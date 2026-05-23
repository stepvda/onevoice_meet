import * as vision from "@mediapipe/tasks-vision";
import {
  ProcessorWrapper,
  VideoTransformer,
  type BackgroundOptions,
} from "@livekit/track-processors";

/**
 * Tighter virtual-background processor.
 *
 * Drop-in replacement for `@livekit/track-processors`'s `VirtualBackground`
 * / `BackgroundBlur`. The stock library uses MediaPipe's *category mask*
 * (binary 0/255 per pixel) and then a 10-px CSS blur on the mask to
 * fake a soft edge — which is exactly why hair and shoulders leak the
 * background through: a thresholded yes/no decision gets smudged
 * across the entire boundary band.
 *
 * Instead we ask MediaPipe for the *confidence mask* — a float in
 * [0, 1] expressing how certain the model is that a pixel is the person
 * — and run that through a steepened sigmoid centered just above 0.5.
 * Two consequences:
 *
 *   1. Tighter edges. The sigmoid slope (`SIGMOID_K`) controls how
 *      narrow the transition band is. Higher = harder edge.
 *
 *   2. Slight inward erosion. Centering the sigmoid at `THRESHOLD`
 *      (slightly > 0.5) shifts the cutoff a touch toward "more
 *      confident person," which eats the 1-2 px halo of low-confidence
 *      pixels where background bleeds in.
 *
 * Animated-background compatibility: `transformer.backgroundImage` is
 * still public and updated the same way `lib/animatedBackgrounds.ts`
 * expects — see [animatedBackgrounds.ts:184].
 */

// Pin to the same MediaPipe version @livekit/track-processors uses so we
// share the cached wasm bundle from cdn.jsdelivr.net (CSP already allows
// that origin in caddy/Caddyfile).
const TASKS_VISION_VERSION = "0.10.9";
const TASKS_VISION_WASM = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${TASKS_VISION_VERSION}/wasm`;
const SELFIE_MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter/float16/latest/selfie_segmenter.tflite";

// Sigmoid that shapes the soft mask. Threshold of 0.42 (below the natural
// midpoint) is deliberate: in low light and on dark hair the model's
// confidence drops to 0.3-0.5 even where the person clearly IS. Pulling
// the cut down to 0.42 keeps those pixels on the person side instead of
// surrendering them to the background. The downside (slightly looser
// edges in well-lit areas) is neutralised by temporal smoothing below,
// which removes the per-frame noise that would otherwise reach through
// the wider boundary band. SIGMOID_K=10 → 5%-to-95% transition spans
// ~0.30 of mask space, soft enough for hair feathering.
const THRESHOLD = 0.42;
const SIGMOID_K = 10;

// Temporal EMA across frames. Smoothed = α·current + (1-α)·previous.
// α=0.55 keeps the mask responsive to head motion (effective time
// constant ~1.8 frames ≈ 60 ms at 30 fps) while killing most of the
// pixel-level jitter. Higher α = snappier but flickerier; lower = laggy.
const TEMPORAL_ALPHA = 0.55;

// ─── Auto-exposure ──────────────────────────────────────────────────
//
// Target average luma for the published frame. 130/255 is slightly above
// middle grey — webcams under-expose faces by default, and slightly-bright
// reads better on a video call than slightly-dark. The compensator only
// kicks in when measured luma is OUTSIDE the deadband around this target.
const AUTO_EXPOSURE_TARGET = 130;
// Deadband — don't adjust if measured luma is already within ±this of the
// target. Stops the filter from pumping back and forth on flickering
// scenes (overhead fluorescent, screen reflections, etc).
const AUTO_EXPOSURE_DEADBAND = 10;
// Slow EMA on the measured luma so we don't react to one bright frame.
// α=0.05 → time constant ~20 frames ≈ 0.7 s at 30 fps. Same on the
// derived brightness/contrast multipliers so the filter ramps smoothly.
const AUTO_EXPOSURE_LUMA_ALPHA = 0.05;
const AUTO_EXPOSURE_FILTER_ALPHA = 0.08;
// Hard limits on the filter so the auto-correct can never make a face
// look like it's been hit by a flashbang or buried in shadow.
const BRIGHTNESS_MIN = 0.8;
const BRIGHTNESS_MAX = 1.5;
const CONTRAST_MIN = 0.95;
const CONTRAST_MAX = 1.3;
// Downsampled resolution we sample luma at. 64×36 = 2,304 pixels, ~0.3 ms
// to compute mean luma. Anything more is wasted accuracy.
const LUMA_SAMPLE_W = 64;
const LUMA_SAMPLE_H = 36;

// ─── Auto-framing ──────────────────────────────────────────────────
//
// Always-on slight zoom-in. We never show pixels outside this crop
// window, so we have headroom to pan without exposing letterbox bars at
// the edges. 1.18 = 18% zoom-in, which lines up with what Meet and
// Teams' "Speaker frame" uses.
const AUTO_FRAMING_ZOOM = 1.18;
// EMA on the centroid. VERY slow — we want the camera to feel like a
// steady-cam operator, not a security camera tracking a fast suspect.
// α=0.04 → time constant ~25 frames ≈ 0.8 s at 30 fps. Translates to
// "you can move your head and the frame doesn't snap; you can shift
// seats and the frame settles to the new position over about a second".
const AUTO_FRAMING_ALPHA = 0.04;
// Below this total person-confidence sum, we assume the person has
// left the frame and freeze the centroid where it was rather than
// chasing a noisy near-zero mask. (256² × 0.005 → 33 pixels' worth of
// person-confidence.)
const AUTO_FRAMING_MIN_CONFIDENCE = 0.005 * 256 * 256;

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/**
 * Walks the confidence mask once, applies the sigmoid, and packs the
 * result into an RGBA8 ImageData (R=G=B=alpha=mask). Returns an
 * ImageBitmap that can be drawn to the output canvas at the input
 * resolution.
 *
 * Doing this on the CPU per frame would normally be slow, but the mask
 * is the model's *native* output resolution (256×256 for selfie
 * segmenter), not the camera resolution. That's ~65 K pixels — well
 * under a millisecond.
 */
async function shapedMaskToBitmap(
  conf: Float32Array,
  w: number,
  h: number,
): Promise<ImageBitmap> {
  const rgba = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < conf.length; i++) {
    // confidenceMasks[0] for the selfie segmenter is *person*-confidence:
    // ~1.0 where the model is sure it's the person, ~0.0 for background.
    // The downstream composite uses high-alpha = person (destination-in
    // keeps the frame where this mask is opaque), so no inversion.
    const x = SIGMOID_K * (conf[i] - THRESHOLD);
    // 1 / (1 + e^-x), clamped to [0, 255] via Uint8ClampedArray.
    const v = 255 / (1 + Math.exp(-x));
    const o = i * 4;
    rgba[o] = v;
    rgba[o + 1] = v;
    rgba[o + 2] = v;
    rgba[o + 3] = v;
  }
  return createImageBitmap(new ImageData(rgba, w, h));
}

class TighterBackgroundTransformer extends VideoTransformer<BackgroundOptions> {
  imageSegmenter?: vision.ImageSegmenter;
  segmentationResults?: vision.ImageSegmenterResult;
  backgroundImage: ImageBitmap | null = null;
  blurRadius?: number;
  options: BackgroundOptions;
  // Rolling EMA of the confidence mask. Lazy-allocated to the model's
  // native mask dimensions on the first frame; reused thereafter so we
  // don't churn 256 KB of memory per frame.
  private prevConfidence: Float32Array | null = null;
  // Auto-exposure state. The luma sampler reuses the same offscreen
  // canvas every frame.
  private lumaCanvas: OffscreenCanvas | null = null;
  private lumaCtx: OffscreenCanvasRenderingContext2D | null = null;
  private smoothedLuma = AUTO_EXPOSURE_TARGET;
  private smoothedBrightness = 1;
  private smoothedContrast = 1;
  // Auto-framing state. Centroid in [0,1] of the camera frame; seeded to
  // dead-center so the first few frames don't snap.
  private smoothedCenterX = 0.5;
  private smoothedCenterY = 0.5;
  // Set to false on the first valid centroid measurement so we don't
  // EMA-blend with the seed value (which would slow convergence).
  private centroidSeeded = false;

  /**
   * Sample mean luma of the current frame, smoothed across frames.
   * Returns the smoothed luma value (0-255).
   *
   * We sample at 64×36 (LUMA_SAMPLE_W × LUMA_SAMPLE_H) because that's
   * cheap (~2K pixels) and the average pulls out the lighting condition
   * just as well as sampling at full res. drawImage() does the
   * downsampling on the GPU, then we readback once.
   */
  private sampleLuma(frame: VideoFrame): number {
    if (!this.lumaCanvas) {
      this.lumaCanvas = new OffscreenCanvas(LUMA_SAMPLE_W, LUMA_SAMPLE_H);
      this.lumaCtx = this.lumaCanvas.getContext("2d", { willReadFrequently: true });
    }
    if (!this.lumaCtx) return this.smoothedLuma;
    this.lumaCtx.drawImage(frame, 0, 0, LUMA_SAMPLE_W, LUMA_SAMPLE_H);
    const data = this.lumaCtx.getImageData(0, 0, LUMA_SAMPLE_W, LUMA_SAMPLE_H).data;
    let sum = 0;
    for (let i = 0; i < data.length; i += 4) {
      // Rec. 601 luma — close enough and cheap.
      sum += 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
    }
    const meanLuma = sum / (data.length / 4);
    const a = AUTO_EXPOSURE_LUMA_ALPHA;
    this.smoothedLuma = a * meanLuma + (1 - a) * this.smoothedLuma;
    return this.smoothedLuma;
  }

  /**
   * From the smoothed luma, derive a smoothed brightness/contrast pair
   * and return a CSS filter string. Returns `"none"` if the scene's
   * already inside the deadband AND the current filter is close to 1,
   * which avoids an expensive filter compositing pass for no gain.
   */
  private exposureFilter(): string {
    const luma = this.smoothedLuma;
    const diff = AUTO_EXPOSURE_TARGET - luma;
    let targetB = 1;
    let targetC = 1;
    if (Math.abs(diff) > AUTO_EXPOSURE_DEADBAND) {
      // Brightness scales proportionally to the gap, clipped.
      targetB = clamp(AUTO_EXPOSURE_TARGET / Math.max(luma, 30), BRIGHTNESS_MIN, BRIGHTNESS_MAX);
      // Dark scenes are flat — bump contrast a touch. Bright scenes are
      // washed out — slight pull. Mapped so |diff|=50 → ±~0.2 contrast.
      targetC = clamp(1 + diff / 250, CONTRAST_MIN, CONTRAST_MAX);
    }
    const a = AUTO_EXPOSURE_FILTER_ALPHA;
    this.smoothedBrightness = a * targetB + (1 - a) * this.smoothedBrightness;
    this.smoothedContrast = a * targetC + (1 - a) * this.smoothedContrast;
    // Skip filter compositing if we're effectively neutral (saves a
    // canvas compositing pass).
    if (Math.abs(this.smoothedBrightness - 1) < 0.02 && Math.abs(this.smoothedContrast - 1) < 0.02) {
      return "none";
    }
    return `brightness(${this.smoothedBrightness.toFixed(3)}) contrast(${this.smoothedContrast.toFixed(3)})`;
  }

  /**
   * Compute the weighted centroid of the person mask in [0,1] of frame
   * coordinates, smoothed across frames. Returns `{x, y}` where both are
   * the SMOOTHED values — callers don't need to remember the previous
   * centroid; we keep it on the instance.
   *
   * If the total person confidence is below `AUTO_FRAMING_MIN_CONFIDENCE`
   * (e.g. nobody on camera), we hold the last smoothed centroid in place
   * instead of chasing noise.
   */
  private updateCentroid(conf: Float32Array, w: number, h: number): { x: number; y: number } {
    // Walk the mask once, accumulating sum(p), sum(p·x), sum(p·y).
    let sumP = 0;
    let sumPX = 0;
    let sumPY = 0;
    let idx = 0;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const p = conf[idx++];
        if (p < 0.3) continue; // skip near-background pixels — much faster than counting them
        sumP += p;
        sumPX += p * x;
        sumPY += p * y;
      }
    }
    if (sumP < AUTO_FRAMING_MIN_CONFIDENCE) {
      // Not enough person detected — coast on previous centroid.
      return { x: this.smoothedCenterX, y: this.smoothedCenterY };
    }
    const measuredX = sumPX / sumP / w;  // normalised to [0,1]
    const measuredY = sumPY / sumP / h;
    if (!this.centroidSeeded) {
      this.smoothedCenterX = measuredX;
      this.smoothedCenterY = measuredY;
      this.centroidSeeded = true;
    } else {
      const a = AUTO_FRAMING_ALPHA;
      this.smoothedCenterX = a * measuredX + (1 - a) * this.smoothedCenterX;
      this.smoothedCenterY = a * measuredY + (1 - a) * this.smoothedCenterY;
    }
    return { x: this.smoothedCenterX, y: this.smoothedCenterY };
  }

  /**
   * Blend the current mask into our rolling EMA buffer and return the
   * smoothed result. The buffer IS the working result — the caller reads
   * it directly. Splitting this out makes the EMA easy to disable later
   * (just return the input `cur`).
   */
  private smoothConfidence(cur: Float32Array): Float32Array {
    if (!this.prevConfidence || this.prevConfidence.length !== cur.length) {
      // First frame (or resolution change): seed prev with the raw mask so
      // we don't render a black smear while the EMA warms up.
      this.prevConfidence = new Float32Array(cur);
      return this.prevConfidence;
    }
    const prev = this.prevConfidence;
    const a = TEMPORAL_ALPHA;
    const b = 1 - a;
    for (let i = 0; i < cur.length; i++) {
      prev[i] = a * cur[i] + b * prev[i];
    }
    return prev;
  }

  constructor(opts: BackgroundOptions) {
    super();
    this.options = opts;
    if (opts.blurRadius) this.blurRadius = opts.blurRadius;
  }

  async init(initOpts: Parameters<VideoTransformer<BackgroundOptions>["init"]>[0]) {
    await super.init(initOpts);

    const fileSet = await vision.FilesetResolver.forVisionTasks(
      this.options.assetPaths?.tasksVisionFileSet ?? TASKS_VISION_WASM,
    );
    this.imageSegmenter = await vision.ImageSegmenter.createFromOptions(fileSet, {
      baseOptions: {
        modelAssetPath: this.options.assetPaths?.modelAssetPath ?? SELFIE_MODEL_URL,
        delegate: "GPU",
        ...this.options.segmenterOptions,
      },
      runningMode: "VIDEO",
      // The key difference from the stock @livekit/track-processors:
      //   stock = outputCategoryMask: true  → binary 0/255
      //   ours  = outputConfidenceMasks: true → float [0, 1]
      outputCategoryMask: false,
      outputConfidenceMasks: true,
    });

    if (this.options.imagePath && !this.backgroundImage) {
      await this.loadBackground(this.options.imagePath).catch((err) =>
        console.error("[tighterBackground] background image load failed:", err),
      );
    }
  }

  async destroy() {
    await super.destroy();
    await this.imageSegmenter?.close();
    this.imageSegmenter = undefined;
    this.backgroundImage = null;
    this.prevConfidence = null;
    this.lumaCanvas = null;
    this.lumaCtx = null;
    this.smoothedLuma = AUTO_EXPOSURE_TARGET;
    this.smoothedBrightness = 1;
    this.smoothedContrast = 1;
    this.smoothedCenterX = 0.5;
    this.smoothedCenterY = 0.5;
    this.centroidSeeded = false;
  }

  async loadBackground(path: string) {
    const img = new Image();
    img.crossOrigin = "Anonymous";
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = (err) => reject(err);
      img.src = path;
    });
    this.backgroundImage = await createImageBitmap(img);
  }

  async update(opts: BackgroundOptions) {
    this.options = opts;
    if (opts.blurRadius) {
      this.blurRadius = opts.blurRadius;
    } else if (opts.imagePath) {
      await this.loadBackground(opts.imagePath);
    }
  }

  async transform(frame: VideoFrame, controller: TransformStreamDefaultController<VideoFrame>) {
    try {
      if (!(frame instanceof VideoFrame)) return;
      if (this.isDisabled || !this.canvas || !this.ctx || !this.inputVideo) {
        controller.enqueue(frame);
        return;
      }

      // segmentForVideo's callback fires synchronously with the result
      // for the SAME frame (VIDEO mode). That's why we can write the
      // result into a member variable and immediately read it below.
      this.imageSegmenter?.segmentForVideo(this.inputVideo, performance.now(), (r) => {
        this.segmentationResults = r;
      });

      if (this.blurRadius) {
        await this.compositeBlur(frame);
      } else {
        await this.compositeVirtualBackground(frame);
      }

      const out = new VideoFrame(this.canvas, { timestamp: frame.timestamp ?? Date.now() });
      controller.enqueue(out);
    } finally {
      frame?.close();
    }
  }

  /**
   * Composite person-over-background. Order matters:
   *   1. draw the shaped soft mask (alpha = personness)
   *   2. `source-in` background image → background is painted only where
   *      the mask is opaque (= where the *background* should appear)
   *   3. `destination-over` camera frame → the person fills the rest
   *
   * Note we're using the mask "inverted" relative to step 1's wording:
   * `shapedMaskToBitmap` puts higher values where the person IS. Then
   * `source-in` paints background only where the mask is opaque. That's
   * the opposite of what we want — fixed by drawing the mask itself
   * INVERTED. See globalCompositeOperation gymnastics below.
   */
  /**
   * Resolve the auto-framing crop window: where in the FRAME we sample
   * pixels, and where in the MASK we sample mask pixels. Both windows
   * are zoom-locked and centered on the smoothed person centroid.
   *
   * Returns the four `drawImage` source-rect parameters for each:
   *   { fx, fy, fw, fh }  → for the frame
   *   { mx, my, mw, mh }  → for the mask bitmap
   *
   * Caller draws each onto the full output canvas. Same crop fraction
   * is used for both so mask and frame stay aligned.
   */
  private framingWindow(frame: VideoFrame, maskW: number, maskH: number) {
    const fw = frame.displayWidth / AUTO_FRAMING_ZOOM;
    const fh = frame.displayHeight / AUTO_FRAMING_ZOOM;
    const fxTarget = this.smoothedCenterX * frame.displayWidth - fw / 2;
    const fyTarget = this.smoothedCenterY * frame.displayHeight - fh / 2;
    const fx = clamp(fxTarget, 0, frame.displayWidth - fw);
    const fy = clamp(fyTarget, 0, frame.displayHeight - fh);
    // Same crop fraction in mask space.
    const mw = maskW / AUTO_FRAMING_ZOOM;
    const mh = maskH / AUTO_FRAMING_ZOOM;
    const mx = (fx / frame.displayWidth) * maskW;
    const my = (fy / frame.displayHeight) * maskH;
    return { fx, fy, fw, fh, mx, my, mw, mh };
  }

  private async compositeVirtualBackground(frame: VideoFrame) {
    if (!this.canvas || !this.ctx) return;
    const mask = this.segmentationResults?.confidenceMasks?.[0];
    if (!mask) return;

    const w = this.canvas.width;
    const h = this.canvas.height;
    const rawConf = mask.getAsFloat32Array();
    // Centroid is computed from the raw (un-EMA'd) confidence — gives a
    // truer position; the EMA on `smoothedCenterX/Y` smooths it instead.
    this.updateCentroid(rawConf, mask.width, mask.height);
    const smoothed = this.smoothConfidence(rawConf);
    const bitmap = await shapedMaskToBitmap(smoothed, mask.width, mask.height);
    const win = this.framingWindow(frame, mask.width, mask.height);
    this.sampleLuma(frame);
    const filter = this.exposureFilter();

    // Step 1: paint the camera frame onto the canvas, full opacity,
    // through the auto-exposure filter, cropped to the framing window.
    this.ctx.save();
    this.ctx.globalCompositeOperation = "copy";
    this.ctx.filter = filter;
    this.ctx.drawImage(frame, win.fx, win.fy, win.fw, win.fh, 0, 0, w, h);
    this.ctx.filter = "none";

    // Step 2: clip to person mask, using the same crop window.
    this.ctx.globalCompositeOperation = "destination-in";
    this.ctx.drawImage(bitmap, win.mx, win.my, win.mw, win.mh, 0, 0, w, h);

    // Step 3: place the background BEHIND the surviving camera pixels.
    // The background is NOT framed-cropped — the virtual background
    // fills the whole output regardless of where the person is.
    this.ctx.globalCompositeOperation = "destination-over";
    if (this.backgroundImage) {
      this.ctx.drawImage(
        this.backgroundImage,
        0, 0, this.backgroundImage.width, this.backgroundImage.height,
        0, 0, w, h,
      );
    } else {
      this.ctx.fillStyle = "#00FF00";
      this.ctx.fillRect(0, 0, w, h);
    }
    this.ctx.restore();
    bitmap.close();
  }

  /** Same idea as compositeVirtualBackground but the "background" is a blurred copy of the frame. */
  private async compositeBlur(frame: VideoFrame) {
    if (!this.canvas || !this.ctx) return;
    const mask = this.segmentationResults?.confidenceMasks?.[0];
    if (!mask) return;

    const w = this.canvas.width;
    const h = this.canvas.height;
    const rawConf = mask.getAsFloat32Array();
    this.updateCentroid(rawConf, mask.width, mask.height);
    const smoothed = this.smoothConfidence(rawConf);
    const bitmap = await shapedMaskToBitmap(smoothed, mask.width, mask.height);
    const win = this.framingWindow(frame, mask.width, mask.height);
    this.sampleLuma(frame);
    const filter = this.exposureFilter();

    this.ctx.save();
    this.ctx.globalCompositeOperation = "copy";
    this.ctx.filter = filter;
    this.ctx.drawImage(frame, win.fx, win.fy, win.fw, win.fh, 0, 0, w, h);
    this.ctx.filter = "none";

    this.ctx.globalCompositeOperation = "destination-in";
    this.ctx.drawImage(bitmap, win.mx, win.my, win.mw, win.mh, 0, 0, w, h);

    // Blur the original frame for the background — uses the same crop
    // so the blurred backdrop tracks the person without showing a
    // hard "frame within a frame" artifact.
    this.ctx.globalCompositeOperation = "destination-over";
    this.ctx.filter = `blur(${this.blurRadius ?? 10}px) ${filter !== "none" ? filter : ""}`.trim();
    this.ctx.drawImage(frame, win.fx, win.fy, win.fw, win.fh, 0, 0, w, h);
    this.ctx.restore();
    bitmap.close();
  }
}

export function TighterBackgroundBlur(blurRadius: number = 10) {
  return new ProcessorWrapper(
    new TighterBackgroundTransformer({ blurRadius }),
    "tighter-background-blur",
  );
}

export function TighterVirtualBackground(imagePath: string) {
  return new ProcessorWrapper(
    new TighterBackgroundTransformer({ imagePath }),
    "tighter-virtual-background",
  );
}

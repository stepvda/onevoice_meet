import * as vision from "@mediapipe/tasks-vision";
import {
  ProcessorWrapper,
  VideoTransformer,
  type BackgroundOptions,
} from "@livekit/track-processors";
import { BackgroundCompositorGL, type EdgeTuning } from "./backgroundCompositorGL";
import { SegmentationWorker } from "./segmentationWorker";

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
 * Where WebGL2 is available, compositing runs on the GPU instead
 * (`backgroundCompositorGL.ts`): a colour guided filter snaps the mask
 * edge onto the real contour in the camera image, and the blur backdrop
 * is built only from background pixels so the person doesn't halo. The
 * sigmoid/canvas path below remains as the fallback.
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
// CommonJS build of the same version, loaded into the segmentation worker
// (see segmentationWorker.ts for why it isn't the npm ESM build).
const TASKS_VISION_BUNDLE = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${TASKS_VISION_VERSION}/vision_bundle.cjs`;
const MODEL_BASE = "https://storage.googleapis.com/mediapipe-models/image_segmenter";

// Segmentation models, best first. The multiclass model (background, hair,
// body skin, face skin, clothes, other) is far more decisive than the
// single-class selfie model: side profiles, a second person and long hair
// come out at ~1.0 instead of the selfie model's flickering 0.3-0.5, and
// people printed on posters behind you are mostly rejected. It costs ~3×
// the inference time (25 vs 9 ms on an idle M1, ~65 ms on a busy one) and
// a one-off 16 MB download (vs 250 KB), so it runs in a worker off the
// video path (segmentationWorker.ts), and the selfie model stays as the
// fallback if it fails to load or can't keep up.
interface SegmentationModel {
  url: string;
  // Index of the confidence mask to read, and whether it is the
  // *background* class (person = 1 − value) rather than the person.
  maskIndex: number;
  isBackground: boolean;
  // Alpha ramp on the guided-filter output (WebGL path): alpha 0 at
  // edgeLow, 1 at edgeHigh. The multiclass model is confident, so its ramp
  // sits above 0.5 to shave off the 1-2 px rim of old background (and the
  // camera's sharpening halo) that otherwise outlines the person against a
  // replacement image. The selfie model's confidence on dark hair in low
  // light drops to 0.3-0.5, so its ramp sits low, for the same reason as
  // THRESHOLD above.
  edgeLow: number;
  edgeHigh: number;
}
const SEGMENTATION_MODELS: SegmentationModel[] = [
  {
    url: `${MODEL_BASE}/selfie_multiclass_256x256/float32/latest/selfie_multiclass_256x256.tflite`,
    maskIndex: 0,
    isBackground: true,
    edgeLow: 0.45,
    edgeHigh: 0.75,
  },
  {
    url: `${MODEL_BASE}/selfie_segmenter/float16/latest/selfie_segmenter.tflite`,
    maskIndex: 0,
    isBackground: false,
    edgeLow: 0.3,
    edgeHigh: 0.6,
  },
];
const SELFIE_MODEL = SEGMENTATION_MODELS[1];

// Adaptive segmentation cadence — only for the in-thread fallback, where
// inference blocks the frame. (The worker sets its own pace: a new frame
// goes to it whenever it's free.) When inference is slow, run the model
// only every 2nd or 3rd frame and reuse the last mask in between: the
// guided filter still re-snaps that mask onto each new frame's contours,
// so small motion between model runs stays tight. The cadence targets
// ~SEGMENT_BUDGET_MS of inference per frame on average (of a 33 ms frame
// at 30 fps), and only moves once the smoothed time is clearly past the
// next step, so it doesn't oscillate around a boundary.
const SEGMENT_TIME_ALPHA = 0.1;
const SEGMENT_BUDGET_MS = 12;
const SEGMENT_EVERY_MAX = 3;
const SEGMENT_HYSTERESIS = 0.6;
const SEGMENT_WARMUP_RUNS = 3;
// If inference is still this slow at the slowest cadence (a GPU well below
// an M1's), the long stalls on model frames start to show as stutter:
// switch to the next, lighter model in SEGMENTATION_MODELS instead. The
// switch is one-way, so it needs that many consecutive slow model runs
// (~3 s at the slowest cadence) — a passing GPU spike, e.g. while the
// meeting grid lays out, mustn't cost the better model for the whole call.
const SEGMENT_DOWNGRADE_MS = 40;
const SEGMENT_DOWNGRADE_RUNS = 30;
// Worker equivalent: below ~10 masks/s the mask trails fast head movement
// by more than the guided filter's reach can make up.
const WORKER_DOWNGRADE_MS = 100;
// A worker that hasn't answered in this long has died (crash, GPU context
// loss) and never will; segmentation moves in-thread.
const WORKER_STALL_MS = 3000;

// Sigmoid that shapes the soft mask. Threshold of 0.42 (below the natural
// midpoint) is deliberate: in low light and on dark hair the model's
// confidence drops to 0.3-0.5 even where the person clearly IS. Pulling
// the cut down to 0.42 keeps those pixels on the person side instead of
// surrendering them to the background. The downside (slightly looser
// edges in well-lit areas) is neutralised by temporal smoothing below,
// which removes the per-frame noise that would otherwise reach through
// the wider boundary band. SIGMOID_K=10 → 5%-to-95% transition spans
// ~0.59 of mask space (±ln(19)/k), soft enough for hair feathering.
// Used only by the 2D fallback; the WebGL path uses the per-model ramp
// in SEGMENTATION_MODELS.
const THRESHOLD = 0.42;
const SIGMOID_K = 10;

// Both segmentation models take 256×256 input. MediaPipe returns masks at the
// resolution of the image it was given, so handing it the full camera
// frame yields a 1280×720 mask that is just a bilinear blow-up of the
// same 256×256 output — 14× more pixels for every CPU pass below, with no
// extra detail. Downscale the frame to the model size up front instead.
const SEGMENTATION_SIZE = 256;

// WebGL path: guided-filter edge refinement (see backgroundCompositorGL.ts).
//   radius  — window radius at guide resolution (≈ 2.7 output px each on
//             720p), so a 9×9 window spans ~24 output px: enough to reach
//             the real contour from where the 256-px mask puts it.
//   eps     — how strongly the edge follows colour changes in the frame.
//             1e-4 is barely sharper; 1e-2 visibly softens hair again.
// The alpha ramp comes from the active model (SegmentationModel.edgeLow/High).
const GUIDED_FILTER_RADIUS = 4;
const GUIDED_FILTER_EPS = 1e-3;

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
// Sample at most every Nth frame, from the small bitmap made for the
// segmentation model: each sample is a GPU→CPU readback, and the luma
// EMA's time constant (~0.7 s) doesn't need 30 Hz input. The per-sample α
// is compounded by the frames elapsed so that time constant is unchanged.
const LUMA_SAMPLE_EVERY = 6;

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
  backgroundImage: ImageBitmap | null = null;
  blurRadius?: number;
  options: BackgroundOptions;
  private edgeTuning: EdgeTuning = this.tuningFor(SEGMENTATION_MODELS[0]);
  // GPU compositor; null when WebGL2 is unavailable or the context was
  // lost, in which case the Canvas2D path below is used.
  private compositor: BackgroundCompositorGL | null = null;
  // Off-thread segmentation; null if the worker couldn't start, in which
  // case `imageSegmenter` runs in-thread instead.
  private worker: SegmentationWorker | null = null;
  private lastMaskFrame = 0;
  // This frame's raw confidence mask, copied out of MediaPipe's callback
  // (its buffers are only valid for the callback's duration).
  private confidence: Float32Array | null = null;
  private confidenceW = 0;
  private confidenceH = 0;
  private model: SegmentationModel = SEGMENTATION_MODELS[0];
  private fileSet?: Awaited<ReturnType<typeof vision.FilesetResolver.forVisionTasks>>;
  private switchingModel = false;
  private lastLumaFrame = -Infinity;
  private frameCount = 0;
  // Adaptive cadence state: run the model every Nth frame.
  private segmentEvery = 1;
  private segmentTimeMs = 0;
  private segmentRuns = 0;
  private slowRuns = 0;
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
  private sampleLuma(source: CanvasImageSource, frames: number): number {
    if (!this.lumaCanvas) {
      this.lumaCanvas = new OffscreenCanvas(LUMA_SAMPLE_W, LUMA_SAMPLE_H);
      this.lumaCtx = this.lumaCanvas.getContext("2d", { willReadFrequently: true });
    }
    if (!this.lumaCtx) return this.smoothedLuma;
    this.lumaCtx.drawImage(source, 0, 0, LUMA_SAMPLE_W, LUMA_SAMPLE_H);
    const data = this.lumaCtx.getImageData(0, 0, LUMA_SAMPLE_W, LUMA_SAMPLE_H).data;
    let sum = 0;
    for (let i = 0; i < data.length; i += 4) {
      // Rec. 601 luma — close enough and cheap.
      sum += 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
    }
    return this.smoothLuma(sum / (data.length / 4), frames);
  }

  /** Fold a mean-luma measurement standing for `frames` frames into the EMA. */
  private smoothLuma(meanLuma: number, frames: number): number {
    const a = 1 - Math.pow(1 - AUTO_EXPOSURE_LUMA_ALPHA, frames);
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
   *
   * `frames` is how many camera frames this mask stands for (the
   * segmentation cadence); α is compounded so the EMA's time constant
   * stays the same when the model skips frames.
   */
  private smoothConfidence(cur: Float32Array, frames: number): Float32Array {
    if (!this.prevConfidence || this.prevConfidence.length !== cur.length) {
      // First frame (or resolution change): seed prev with the raw mask so
      // we don't render a black smear while the EMA warms up.
      this.prevConfidence = new Float32Array(cur);
      return this.prevConfidence;
    }
    const prev = this.prevConfidence;
    const a = 1 - Math.pow(1 - TEMPORAL_ALPHA, frames);
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

    const custom = this.options.assetPaths?.modelAssetPath;
    const candidates = custom ? [{ ...SELFIE_MODEL, url: custom }] : SEGMENTATION_MODELS;
    for (const model of candidates) {
      try {
        this.attachWorker(await this.createWorker(model), model);
        break;
      } catch (err) {
        console.warn(`[tighterBackground] segmentation worker failed (${model.url}):`, err);
      }
    }
    if (!this.worker) {
      this.fileSet = await vision.FilesetResolver.forVisionTasks(this.wasmBase());
      for (const model of candidates) {
        try {
          this.imageSegmenter = await this.createSegmenter(model);
          this.useModel(model);
          break;
        } catch (err) {
          console.warn(`[tighterBackground] segmentation model failed to load (${model.url}):`, err);
        }
      }
    }
    if (!this.worker && !this.imageSegmenter) throw new Error("no segmentation model could be loaded");

    if (this.options.imagePath && !this.backgroundImage) {
      await this.loadBackground(this.options.imagePath).catch((err) =>
        console.error("[tighterBackground] background image load failed:", err),
      );
    }

    if (this.canvas) {
      this.compositor = BackgroundCompositorGL.create(this.canvas.width, this.canvas.height);
    }
  }

  private wasmBase(): string {
    return this.options.assetPaths?.tasksVisionFileSet ?? TASKS_VISION_WASM;
  }

  private createWorker(model: SegmentationModel): Promise<SegmentationWorker> {
    return SegmentationWorker.create({ bundleUrl: TASKS_VISION_BUNDLE, wasmBase: this.wasmBase(), model });
  }

  private attachWorker(worker: SegmentationWorker, model: SegmentationModel) {
    this.worker?.terminate();
    this.worker = worker;
    this.useModel(model);
    worker.onMask = ({ mask, width, height, ms, luma }) => {
      this.confidence = mask;
      this.confidenceW = width;
      this.confidenceH = height;
      this.recordSegmentTime(ms);
      this.updateCentroid(mask, width, height);
      // The mask (and luma) stand for every frame since the previous one.
      const frames = Math.max(1, this.frameCount - this.lastMaskFrame);
      this.smoothConfidence(mask, frames);
      this.smoothLuma(luma, frames);
      this.lastMaskFrame = this.frameCount;
    };
    worker.onError = (err) => console.warn("[tighterBackground] segmentation worker error:", err);
  }

  private tuningFor(model: SegmentationModel): EdgeTuning {
    return {
      radius: GUIDED_FILTER_RADIUS,
      eps: GUIDED_FILTER_EPS,
      edgeLow: model.edgeLow,
      edgeHigh: model.edgeHigh,
    };
  }

  private useModel(model: SegmentationModel) {
    this.model = model;
    this.edgeTuning = this.tuningFor(model);
  }

  private createSegmenter(model: SegmentationModel): Promise<vision.ImageSegmenter> {
    return vision.ImageSegmenter.createFromOptions(this.fileSet!, {
      baseOptions: {
        modelAssetPath: model.url,
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
  }

  /** The worker stopped answering (see WORKER_STALL_MS): segment in-thread. */
  private async abandonWorker() {
    const dead = this.worker;
    if (!dead) return;
    this.worker = null;
    dead.terminate();
    console.warn("[tighterBackground] segmentation worker stopped responding; segmenting in-thread");
    try {
      this.fileSet ??= await vision.FilesetResolver.forVisionTasks(this.wasmBase());
      const seg = await this.createSegmenter(this.model);
      if (this.isDisabled) {
        seg.close();
        return;
      }
      this.imageSegmenter = seg;
      this.segmentRuns = 0;
      this.segmentTimeMs = 0;
      this.slowRuns = 0;
    } catch (err) {
      console.error("[tighterBackground] in-thread segmentation unavailable:", err);
    }
  }

  /** Swap in the next, lighter model (see SEGMENT_DOWNGRADE_MS). */
  private async downgradeModel() {
    const next = SEGMENTATION_MODELS[SEGMENTATION_MODELS.indexOf(this.model) + 1];
    if (!next || this.switchingModel) return;
    this.switchingModel = true;
    console.info(
      `[tighterBackground] inference ${this.segmentTimeMs.toFixed(0)} ms is too slow; switching to ${next.url}`,
    );
    try {
      if (this.worker) {
        const w = await this.createWorker(next);
        if (this.isDisabled || !this.worker) {
          w.terminate();
          return;
        }
        this.attachWorker(w, next);
      } else {
        const seg = await this.createSegmenter(next);
        const old = this.imageSegmenter;
        if (this.isDisabled || !old) {
          seg.close();
          return;
        }
        this.imageSegmenter = seg;
        this.useModel(next);
        old.close();
      }
      this.segmentEvery = 1;
      this.segmentTimeMs = 0;
      this.segmentRuns = 0;
      this.slowRuns = 0;
    } catch (err) {
      console.warn("[tighterBackground] lighter model failed to load; keeping current one:", err);
    } finally {
      this.switchingModel = false;
    }
  }

  async destroy() {
    await super.destroy();
    await this.imageSegmenter?.close();
    this.imageSegmenter = undefined;
    this.worker?.terminate();
    this.worker = null;
    this.lastMaskFrame = 0;
    this.compositor?.dispose();
    this.compositor = null;
    this.backgroundImage = null;
    this.confidence = null;
    this.prevConfidence = null;
    this.frameCount = 0;
    this.segmentEvery = 1;
    this.segmentTimeMs = 0;
    this.segmentRuns = 0;
    this.slowRuns = 0;
    this.lastLumaFrame = -Infinity;
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
    if (!(frame instanceof VideoFrame)) return;
    if (this.isDisabled || !this.canvas || !this.ctx) {
      controller.enqueue(frame);
      return;
    }
    try {
      const frameIndex = this.frameCount++;
      // We segment the camera frames themselves. (Segmenting `inputVideo`
      // instead reads whatever the <video> element currently shows, which
      // can be a frame ahead of or behind this one — on motion the mask
      // then lands a frame off the person.) Scaling them to the model's
      // input size keeps the returned mask at 256×256 rather than camera
      // resolution. With neither a worker nor an in-thread segmenter,
      // segmentation is being moved in-thread after the worker died, and
      // frames keep compositing with the last mask meanwhile.
      if (this.worker) {
        if (this.worker.busyMs > WORKER_STALL_MS) void this.abandonWorker();
        else if (!this.worker.busy) this.worker.segment(frame.clone(), SEGMENTATION_SIZE, performance.now());
      } else if (this.imageSegmenter && (!this.prevConfidence || frameIndex % this.segmentEvery === 0)) {
        const small = await this.modelBitmap(frame);
        try {
          const t0 = performance.now();
          if (this.segment(small)) {
            this.recordSegmentTime(performance.now() - t0);
            // Centroid is computed from the raw (un-EMA'd) confidence — gives a
            // truer position; the EMA on `smoothedCenterX/Y` smooths it instead.
            this.updateCentroid(this.confidence!, this.confidenceW, this.confidenceH);
            this.smoothConfidence(this.confidence!, this.segmentEvery);
          }
          this.maybeSampleLuma(small, frameIndex);
        } finally {
          small.close();
        }
      }
      // No mask yet means nothing to hide the background with: drop the
      // frame rather than publish the room unprocessed.
      const smoothed = this.prevConfidence;
      if (!smoothed) return;
      const mw = this.confidenceW;
      const mh = this.confidenceH;
      const filter = this.exposureFilter();

      const out =
        this.compositeGL(frame, smoothed, mw, mh) ??
        (await this.composite2D(frame, smoothed, mw, mh, filter));
      controller.enqueue(new VideoFrame(out, { timestamp: frame.timestamp ?? Date.now() }));
    } finally {
      frame.close();
    }
  }

  private modelBitmap(frame: VideoFrame): Promise<ImageBitmap> {
    return createImageBitmap(frame, {
      resizeWidth: SEGMENTATION_SIZE,
      resizeHeight: SEGMENTATION_SIZE,
      resizeQuality: "medium",
    });
  }

  private maybeSampleLuma(small: ImageBitmap, frameIndex: number) {
    if (frameIndex - this.lastLumaFrame < LUMA_SAMPLE_EVERY) return;
    this.sampleLuma(small, Math.min(frameIndex - this.lastLumaFrame, 30));
    this.lastLumaFrame = frameIndex;
  }

  /**
   * Run the model and copy the person confidence into `this.confidence`.
   * Returns false if the model produced no mask.
   */
  private segment(image: ImageBitmap): boolean {
    let got = false;
    // VIDEO mode runs synchronously; the masks are only valid inside the
    // callback, so copy the confidence values out.
    this.imageSegmenter!.segmentForVideo(image, performance.now(), (r) => {
      const m = r.confidenceMasks?.[this.model.maskIndex];
      if (!m) return;
      const src = m.getAsFloat32Array();
      if (!this.confidence || this.confidence.length !== src.length) {
        this.confidence = new Float32Array(src.length);
      }
      const dst = this.confidence;
      if (this.model.isBackground) {
        for (let i = 0; i < src.length; i++) dst[i] = 1 - src[i];
      } else {
        dst.set(src);
      }
      this.confidenceW = m.width;
      this.confidenceH = m.height;
      got = true;
    });
    return got;
  }

  /** Smooth the inference time and adjust how often the model runs. */
  private recordSegmentTime(ms: number) {
    // The first runs include GPU shader compilation; they'd seed the
    // average with hundreds of ms and pin the cadence at its slowest.
    if (++this.segmentRuns <= SEGMENT_WARMUP_RUNS) return;
    const a = SEGMENT_TIME_ALPHA;
    this.segmentTimeMs = this.segmentTimeMs ? a * ms + (1 - a) * this.segmentTimeMs : ms;
    let slow: boolean;
    if (this.worker) {
      slow = this.segmentTimeMs > WORKER_DOWNGRADE_MS;
    } else {
      const ideal = this.segmentTimeMs / SEGMENT_BUDGET_MS;
      if (ideal > this.segmentEvery + SEGMENT_HYSTERESIS && this.segmentEvery < SEGMENT_EVERY_MAX) {
        this.segmentEvery++;
      } else if (ideal < this.segmentEvery - SEGMENT_HYSTERESIS && this.segmentEvery > 1) {
        this.segmentEvery--;
      }
      slow = this.segmentEvery === SEGMENT_EVERY_MAX && this.segmentTimeMs > SEGMENT_DOWNGRADE_MS;
    }
    this.slowRuns = slow ? this.slowRuns + 1 : 0;
    if (this.slowRuns >= SEGMENT_DOWNGRADE_RUNS && !this.options.assetPaths?.modelAssetPath) {
      void this.downgradeModel();
    }
  }

  /** GPU path. Returns null (and falls back for good) if WebGL is unusable. */
  private compositeGL(frame: VideoFrame, conf: Float32Array, mw: number, mh: number): OffscreenCanvas | null {
    const gl = this.compositor;
    if (!gl || !this.canvas) return null;
    if (gl.isLost) {
      console.warn("[tighterBackground] WebGL context lost; switching to 2D compositing");
      this.compositor = null;
      return null;
    }
    try {
      if (gl.canvas.width !== this.canvas.width || gl.canvas.height !== this.canvas.height) {
        gl.canvas.width = this.canvas.width;
        gl.canvas.height = this.canvas.height;
      }
      // A 1×1 "mask" makes the mask-space window come out in normalised UV.
      const win = this.framingWindow(frame, 1, 1);
      gl.render({
        frame,
        mask: conf,
        maskWidth: mw,
        maskHeight: mh,
        crop: { x: win.mx, y: win.my, w: win.mw, h: win.mh },
        brightness: this.smoothedBrightness,
        contrast: this.smoothedContrast,
        mode: this.blurRadius ? "blur" : "image",
        blurSigma: this.blurRadius ?? 10,
        background: this.backgroundImage,
        tuning: this.edgeTuning,
      });
      return gl.canvas;
    } catch (err) {
      console.error("[tighterBackground] WebGL compositing failed; switching to 2D:", err);
      gl.dispose();
      this.compositor = null;
      return null;
    }
  }

  /** Canvas2D fallback path. */
  private async composite2D(
    frame: VideoFrame,
    conf: Float32Array,
    mw: number,
    mh: number,
    filter: string,
  ): Promise<OffscreenCanvas> {
    if (this.blurRadius) {
      await this.compositeBlur(frame, conf, mw, mh, filter);
    } else {
      await this.compositeVirtualBackground(frame, conf, mw, mh, filter);
    }
    return this.canvas!;
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

  private async compositeVirtualBackground(
    frame: VideoFrame,
    conf: Float32Array,
    maskW: number,
    maskH: number,
    filter: string,
  ) {
    if (!this.canvas || !this.ctx) return;
    const w = this.canvas.width;
    const h = this.canvas.height;
    const bitmap = await shapedMaskToBitmap(conf, maskW, maskH);
    const win = this.framingWindow(frame, maskW, maskH);

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
  private async compositeBlur(frame: VideoFrame, conf: Float32Array, maskW: number, maskH: number, filter: string) {
    if (!this.canvas || !this.ctx) return;
    const w = this.canvas.width;
    const h = this.canvas.height;
    const bitmap = await shapedMaskToBitmap(conf, maskW, maskH);
    const win = this.framingWindow(frame, maskW, maskH);

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

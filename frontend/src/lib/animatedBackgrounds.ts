/**
 * Animated virtual backgrounds.
 *
 * LiveKit's `BackgroundTransformer` loads a single `ImageBitmap` and reuses
 * it for every composited frame, so the public API can't drive a moving
 * background. The wrapper exposes its `transformer` publicly though — so
 * we can swap `transformer.backgroundImage` on a `requestAnimationFrame`
 * loop and the next call to the transformer's `transform()` picks up the
 * new bitmap.
 *
 * Each preset is a pure draw function that paints frame `t` (seconds) to
 * a canvas of arbitrary size. The same draw functions feed the small
 * thumbnails in the picker dropdown, so what you preview is what you get.
 *
 * All designs here are original.
 */

export interface AnimatedBackgroundDef {
  id: string;
  // Translation key for the display label.
  i18nKey: string;
  draw: (ctx: CanvasRenderingContext2D, t: number, w: number, h: number) => void;
}

/** Tiny deterministic LCG so animations look the same across reloads. */
function rand(seed: number) {
  let s = (seed * 2654435761) >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/** Soft drifting blobs of colour over a dark canvas. */
function drawAurora(ctx: CanvasRenderingContext2D, t: number, w: number, h: number) {
  ctx.fillStyle = "#0b1326";
  ctx.fillRect(0, 0, w, h);
  const blobs = [
    { color: "rgba(99, 102, 241, 0.55)", phase: 0 },
    { color: "rgba(236, 72, 153, 0.45)", phase: 1.7 },
    { color: "rgba(56, 189, 248, 0.45)", phase: 3.4 },
    { color: "rgba(250, 204, 21, 0.35)", phase: 5.1 },
  ];
  ctx.globalCompositeOperation = "screen";
  for (const b of blobs) {
    const px = w * (0.5 + 0.35 * Math.sin(t * 0.5 + b.phase));
    const py = h * (0.5 + 0.35 * Math.cos(t * 0.4 + b.phase * 0.9));
    const r = Math.min(w, h) * (0.45 + 0.05 * Math.sin(t + b.phase));
    const grad = ctx.createRadialGradient(px, py, 0, px, py, r);
    grad.addColorStop(0, b.color);
    grad.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, h);
  }
  ctx.globalCompositeOperation = "source-over";
}

/** Slow upward-drifting bokeh circles. */
function drawBokeh(ctx: CanvasRenderingContext2D, t: number, w: number, h: number) {
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "#1e1b4b");
  grad.addColorStop(1, "#0b1326");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, w, h);
  const rng = rand(7);
  ctx.globalCompositeOperation = "screen";
  for (let i = 0; i < 28; i++) {
    const seedX = rng();
    const seedY = rng();
    const r = (10 + rng() * 70) * (h / 720);
    const speed = 0.06 + rng() * 0.16;
    // wrap around the top with a fade.
    const y = ((seedY * h + h) - ((t * speed * h) % (h + r * 2))) - r;
    const x = seedX * w + Math.sin(t * 0.3 + seedX * 9) * 18;
    const alpha = 0.18 + rng() * 0.35;
    const hue = (200 + seedX * 160) % 360;
    const inner = `hsla(${hue}, 80%, 75%, ${alpha})`;
    const g = ctx.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, inner);
    g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g;
    ctx.fillRect(x - r, y - r, r * 2, r * 2);
  }
  ctx.globalCompositeOperation = "source-over";
}

/** Flowing sine-wave horizon with gradient sky. */
function drawWaves(ctx: CanvasRenderingContext2D, t: number, w: number, h: number) {
  const sky = ctx.createLinearGradient(0, 0, 0, h);
  sky.addColorStop(0, "#0c4a6e");
  sky.addColorStop(0.6, "#0891b2");
  sky.addColorStop(1, "#facc15");
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, w, h);
  const layers = [
    { color: "rgba(8, 47, 73, 0.95)", baseY: 0.78, amp: 18, freq: 0.012, speed: 0.6 },
    { color: "rgba(7, 89, 133, 0.85)", baseY: 0.84, amp: 26, freq: 0.009, speed: 1.0 },
    { color: "rgba(2, 132, 199, 0.75)", baseY: 0.9, amp: 32, freq: 0.007, speed: 1.4 },
  ];
  for (const l of layers) {
    ctx.beginPath();
    ctx.moveTo(0, h);
    for (let x = 0; x <= w; x += 4) {
      const y = h * l.baseY + Math.sin(x * l.freq + t * l.speed) * l.amp;
      ctx.lineTo(x, y);
    }
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fillStyle = l.color;
    ctx.fill();
  }
}

/** Soft particles drifting upward against a gradient sky. */
function drawParticles(ctx: CanvasRenderingContext2D, t: number, w: number, h: number) {
  const sky = ctx.createLinearGradient(0, 0, 0, h);
  sky.addColorStop(0, "#1f2937");
  sky.addColorStop(1, "#0f172a");
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, w, h);
  const rng = rand(42);
  for (let i = 0; i < 80; i++) {
    const sx = rng();
    const sy = rng();
    const sp = 0.04 + rng() * 0.12;
    const r = (1 + rng() * 2.5) * (h / 720);
    const y = ((sy * h + h) - ((t * sp * h) % (h + 40))) - 20;
    const x = sx * w + Math.sin(t * 0.5 + sx * 13) * 12;
    const alpha = 0.4 + rng() * 0.5;
    ctx.fillStyle = `rgba(226, 232, 240, ${alpha})`;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }
}

export const ANIMATED_BACKGROUNDS: AnimatedBackgroundDef[] = [
  { id: "anim:aurora", i18nKey: "background.aurora", draw: drawAurora },
  { id: "anim:bokeh", i18nKey: "background.bokeh", draw: drawBokeh },
  { id: "anim:waves", i18nKey: "background.wavesAnim", draw: drawWaves },
  { id: "anim:particles", i18nKey: "background.particles", draw: drawParticles },
];

export function getAnimatedById(id: string): AnimatedBackgroundDef | null {
  return ANIMATED_BACKGROUNDS.find((b) => b.id === id) ?? null;
}

/** Render one frame to a canvas, then to an ImageBitmap.
 *  Returns null on browsers without `createImageBitmap` (shouldn't happen
 *  in the target set). */
async function renderToBitmap(
  draw: AnimatedBackgroundDef["draw"],
  t: number,
  w: number,
  h: number,
): Promise<ImageBitmap | null> {
  if (typeof OffscreenCanvas !== "undefined") {
    const off = new OffscreenCanvas(w, h);
    const ctx = off.getContext("2d");
    if (!ctx) return null;
    draw(ctx as unknown as CanvasRenderingContext2D, t, w, h);
    return off.transferToImageBitmap();
  }
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d");
  if (!ctx) return null;
  draw(ctx, t, w, h);
  return await createImageBitmap(c);
}

interface AnimationHandle {
  stop: () => void;
}

/**
 * Start updating a processor's background image at ~15fps. The first frame
 * is rendered synchronously so the processor has something to draw on the
 * very next composited frame; subsequent frames replace the previous
 * bitmap (and close it) on a rAF loop.
 */
export function driveAnimatedBackground(
  processor: { transformer: { backgroundImage: ImageBitmap | null } | null | undefined },
  def: AnimatedBackgroundDef,
  width = 1280,
  height = 720,
  fps = 15,
): AnimationHandle {
  let stopped = false;
  let lastUpdate = 0;
  const start = performance.now();

  const tick = () => {
    if (stopped) return;
    const now = performance.now();
    if (now - lastUpdate >= 1000 / fps) {
      lastUpdate = now;
      const t = (now - start) / 1000;
      void renderToBitmap(def.draw, t, width, height).then((bmp) => {
        if (stopped || !bmp) {
          bmp?.close?.();
          return;
        }
        const tr = processor.transformer;
        if (!tr) {
          bmp.close?.();
          return;
        }
        const prev = tr.backgroundImage;
        tr.backgroundImage = bmp;
        prev?.close?.();
      });
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);

  return {
    stop: () => {
      stopped = true;
    },
  };
}

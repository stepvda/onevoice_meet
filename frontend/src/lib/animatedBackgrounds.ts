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

/**
 * Night cityscape — three parallax skyline layers with twinkling windows,
 * a glowing moon with halo, drifting clouds, and an occasional blinking
 * aircraft beacon crossing the sky. The skylines are seeded once and
 * cached on the function so building positions stay put across frames;
 * only the lights and clouds animate.
 */
type Building = { x: number; w: number; h: number; windowSeed: number };
type Skyline = { color: string; baseY: number; buildings: Building[] };
const cityCache = new Map<string, Skyline[]>();
function buildSkylines(w: number, h: number): Skyline[] {
  const key = `${w}x${h}`;
  const hit = cityCache.get(key);
  if (hit) return hit;
  const layers: Skyline[] = [];
  const tiers = [
    { color: "#0f172a", baseY: 0.55, min: 0.18, max: 0.32, width: [40, 90], count: 14 },
    { color: "#0a1426", baseY: 0.7, min: 0.22, max: 0.42, width: [55, 110], count: 11 },
    { color: "#050a15", baseY: 0.85, min: 0.3, max: 0.55, width: [70, 130], count: 9 },
  ];
  const rng = rand(13);
  for (const tier of tiers) {
    const buildings: Building[] = [];
    let x = -tier.width[1] * rng();
    while (x < w + 80) {
      const bw = tier.width[0] + rng() * (tier.width[1] - tier.width[0]);
      const bh = h * (tier.min + rng() * (tier.max - tier.min));
      buildings.push({ x, w: bw, h: bh, windowSeed: Math.floor(rng() * 1e6) });
      x += bw + 2 + rng() * 4;
    }
    layers.push({ color: tier.color, baseY: tier.baseY * h, buildings });
  }
  cityCache.set(key, layers);
  return layers;
}
function drawCityscape(ctx: CanvasRenderingContext2D, t: number, w: number, h: number) {
  // Deep night sky gradient.
  const sky = ctx.createLinearGradient(0, 0, 0, h);
  sky.addColorStop(0, "#0a0e1c");
  sky.addColorStop(0.5, "#162042");
  sky.addColorStop(1, "#2c1a3a");
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, w, h);

  // Stars — fixed positions, only brightness pulses.
  const starRng = rand(91);
  for (let i = 0; i < 50; i++) {
    const sx = starRng() * w;
    const sy = starRng() * h * 0.55;
    const phase = starRng() * Math.PI * 2;
    const a = 0.35 + 0.4 * (0.5 + 0.5 * Math.sin(t * 1.2 + phase));
    ctx.fillStyle = `rgba(255, 250, 230, ${a.toFixed(3)})`;
    ctx.fillRect(sx, sy, 1.4, 1.4);
  }

  // Moon with soft halo, top-right.
  const mx = w * 0.82;
  const my = h * 0.22;
  const mr = Math.min(w, h) * 0.06;
  const halo = ctx.createRadialGradient(mx, my, 0, mx, my, mr * 4);
  halo.addColorStop(0, "rgba(255, 240, 200, 0.55)");
  halo.addColorStop(1, "rgba(255, 240, 200, 0)");
  ctx.fillStyle = halo;
  ctx.fillRect(mx - mr * 4, my - mr * 4, mr * 8, mr * 8);
  ctx.fillStyle = "#fef3c7";
  ctx.beginPath();
  ctx.arc(mx, my, mr, 0, Math.PI * 2);
  ctx.fill();

  // Slow drifting cloud bands across the moon.
  ctx.globalCompositeOperation = "source-over";
  for (let i = 0; i < 3; i++) {
    const cy = h * (0.18 + i * 0.05);
    const cx = ((t * (8 + i * 5)) % (w + 300)) - 200;
    const cr = 60 + i * 30;
    const cg = ctx.createRadialGradient(cx, cy, 0, cx, cy, cr);
    cg.addColorStop(0, `rgba(200, 200, 220, ${0.22 - i * 0.05})`);
    cg.addColorStop(1, "rgba(200, 200, 220, 0)");
    ctx.fillStyle = cg;
    ctx.fillRect(cx - cr, cy - cr, cr * 2, cr * 2);
  }

  // Parallax skylines back-to-front so closer = darker = on top.
  const layers = buildSkylines(w, h);
  for (let li = 0; li < layers.length; li++) {
    const layer = layers[li];
    const drift = ((t * (4 + li * 6)) % w);
    for (const b of layer.buildings) {
      const bx = b.x - drift;
      const by = layer.baseY - b.h;
      // Wrap horizontally.
      for (const offset of [-w, 0, w]) {
        const x = bx + offset;
        if (x + b.w < 0 || x > w) continue;
        ctx.fillStyle = layer.color;
        ctx.fillRect(x, by, b.w, b.h + h);
        // Windows — small grid; brightness twinkles with a per-window phase.
        const wrng = rand(b.windowSeed + li * 7);
        const cols = Math.max(2, Math.floor(b.w / 14));
        const rows = Math.max(2, Math.floor(b.h / 18));
        for (let cy = 0; cy < rows; cy++) {
          for (let cx = 0; cx < cols; cx++) {
            if (wrng() > 0.55) continue;
            const phase = wrng() * 6.28;
            const lit = 0.5 + 0.5 * Math.sin(t * 1.4 + phase);
            // Only the foreground tier lights are bright; back tiers are dimmer.
            const intensity = (li === 2 ? 0.85 : li === 1 ? 0.5 : 0.3) * lit;
            const wx = x + 4 + cx * (b.w - 8) / cols;
            const wy = by + 4 + cy * (b.h - 8) / rows;
            const hue = wrng() < 0.7 ? "255, 220, 140" : "180, 220, 255";
            ctx.fillStyle = `rgba(${hue}, ${(0.1 + intensity * 0.6).toFixed(3)})`;
            ctx.fillRect(wx, wy, Math.max(2, b.w / cols - 4), Math.max(2, b.h / rows - 5));
          }
        }
      }
    }
  }

  // Aircraft beacon: a small red blink that crosses left-to-right every
  // ~25s. Position derived from `t` directly so it loops cleanly.
  const planeT = (t % 25) / 25;
  if (planeT > 0.05 && planeT < 0.95) {
    const px = w * planeT;
    const py = h * (0.12 + Math.sin(planeT * Math.PI) * 0.02);
    const blink = Math.sin(t * 6) > 0 ? 0.95 : 0.2;
    ctx.fillStyle = `rgba(255, 70, 70, ${blink})`;
    ctx.beginPath();
    ctx.arc(px, py, 1.8, 0, Math.PI * 2);
    ctx.fill();
  }
}

/**
 * Cherry blossoms — pastel sky, twin distant mountain silhouettes, and
 * a continuous shower of stylized 5-petal blossom shapes that rotate and
 * sway as they fall. Each petal is drawn as five quadratic-curve lobes
 * around a centre, then alpha-blended over the scene.
 */
type Petal = { sx: number; sy: number; speed: number; size: number; spin: number; phase: number; hue: string };
const petalCache = new Map<string, Petal[]>();
function buildPetals(w: number, h: number): Petal[] {
  const key = `${w}x${h}`;
  const hit = petalCache.get(key);
  if (hit) return hit;
  const rng = rand(57);
  const petals: Petal[] = [];
  for (let i = 0; i < 65; i++) {
    petals.push({
      sx: rng(),
      sy: rng(),
      speed: 0.06 + rng() * 0.14,
      size: (8 + rng() * 14) * (h / 720),
      spin: (rng() - 0.5) * 1.4,
      phase: rng() * 6.28,
      hue: rng() < 0.6 ? "255, 200, 215" : rng() < 0.8 ? "255, 220, 230" : "240, 170, 200",
    });
  }
  petalCache.set(key, petals);
  return petals;
}
function drawCherryBlossoms(ctx: CanvasRenderingContext2D, t: number, w: number, h: number) {
  // Pastel sunset-pink gradient.
  const sky = ctx.createLinearGradient(0, 0, 0, h);
  sky.addColorStop(0, "#fde2e4");
  sky.addColorStop(0.4, "#fcc9d2");
  sky.addColorStop(1, "#ee9ca7");
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, w, h);

  // Distant sun, very soft.
  const sx = w * 0.3;
  const sy = h * 0.32;
  const sr = Math.min(w, h) * 0.18;
  const sunHalo = ctx.createRadialGradient(sx, sy, 0, sx, sy, sr * 2);
  sunHalo.addColorStop(0, "rgba(255, 240, 230, 0.7)");
  sunHalo.addColorStop(1, "rgba(255, 240, 230, 0)");
  ctx.fillStyle = sunHalo;
  ctx.fillRect(sx - sr * 2, sy - sr * 2, sr * 4, sr * 4);

  // Distant mountains, two layers.
  ctx.fillStyle = "rgba(180, 130, 160, 0.55)";
  ctx.beginPath();
  ctx.moveTo(0, h * 0.78);
  for (let x = 0; x <= w; x += 12) {
    const y = h * 0.78 - Math.sin(x * 0.004 + 0.6) * 30 - Math.sin(x * 0.011) * 14;
    ctx.lineTo(x, y);
  }
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fill();

  ctx.fillStyle = "rgba(120, 80, 110, 0.7)";
  ctx.beginPath();
  ctx.moveTo(0, h * 0.86);
  for (let x = 0; x <= w; x += 12) {
    const y = h * 0.86 - Math.sin(x * 0.006 + 1.8) * 26 - Math.sin(x * 0.013) * 10;
    ctx.lineTo(x, y);
  }
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fill();

  // Foreground branch silhouette in the upper-left corner.
  ctx.strokeStyle = "rgba(58, 30, 40, 0.85)";
  ctx.lineWidth = 4;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(-10, h * 0.15);
  ctx.bezierCurveTo(w * 0.2, h * 0.05, w * 0.32, h * 0.22, w * 0.42, h * 0.12);
  ctx.stroke();
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(w * 0.25, h * 0.13);
  ctx.bezierCurveTo(w * 0.27, h * 0.22, w * 0.3, h * 0.2, w * 0.32, h * 0.28);
  ctx.stroke();

  // Falling petals.
  const petals = buildPetals(w, h);
  for (const p of petals) {
    const cycle = h + p.size * 3;
    const y = ((p.sy * h) + ((t * p.speed * h) % cycle)) - p.size;
    const x = p.sx * w + Math.sin(t * 0.8 + p.phase) * 26;
    const rotation = t * p.spin + p.phase;
    ctx.save();
    ctx.translate(x, y % cycle);
    ctx.rotate(rotation);
    ctx.fillStyle = `rgba(${p.hue}, 0.92)`;
    // 5-lobed flower silhouette drawn as overlapping ellipses.
    for (let k = 0; k < 5; k++) {
      const a = (k / 5) * Math.PI * 2;
      const lx = Math.cos(a) * p.size * 0.55;
      const ly = Math.sin(a) * p.size * 0.55;
      ctx.beginPath();
      ctx.ellipse(lx, ly, p.size * 0.55, p.size * 0.36, a + Math.PI / 2, 0, Math.PI * 2);
      ctx.fill();
    }
    // Centre dot for definition.
    ctx.fillStyle = "rgba(220, 110, 140, 0.7)";
    ctx.beginPath();
    ctx.arc(0, 0, p.size * 0.18, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }
}

/**
 * Fireflies in a forest. Layered tree silhouettes with rolling mist
 * between layers and ~40 fireflies each tracing slow Lissajous-like
 * curves. Brightness pulses per firefly so the swarm twinkles. Trees
 * are cached after first build for the same reason as the city.
 */
type Tree = { x: number; trunkW: number; trunkH: number; canopyR: number; seed: number };
type TreeLayer = { color: string; baseY: number; trees: Tree[] };
const forestCache = new Map<string, TreeLayer[]>();
function buildForest(w: number, h: number): TreeLayer[] {
  const key = `${w}x${h}`;
  const hit = forestCache.get(key);
  if (hit) return hit;
  const tiers = [
    { color: "#0c1f1d", baseY: 0.62, count: 8, scale: 0.6 },
    { color: "#08171a", baseY: 0.78, count: 7, scale: 0.85 },
    { color: "#040a0c", baseY: 0.95, count: 5, scale: 1.2 },
  ];
  const rng = rand(31);
  const layers: TreeLayer[] = [];
  for (const tier of tiers) {
    const trees: Tree[] = [];
    for (let i = 0; i < tier.count; i++) {
      const x = (i + rng() * 0.8) * (w / tier.count) - w * 0.02;
      trees.push({
        x,
        trunkW: 6 + rng() * 10,
        trunkH: (80 + rng() * 100) * tier.scale,
        canopyR: (40 + rng() * 50) * tier.scale,
        seed: Math.floor(rng() * 1e6),
      });
    }
    layers.push({ color: tier.color, baseY: tier.baseY * h, trees });
  }
  forestCache.set(key, layers);
  return layers;
}
function drawFireflies(ctx: CanvasRenderingContext2D, t: number, w: number, h: number) {
  // Twilight forest gradient — deep teal up top, dark olive at the ground.
  const sky = ctx.createLinearGradient(0, 0, 0, h);
  sky.addColorStop(0, "#0e2e2e");
  sky.addColorStop(0.55, "#143a30");
  sky.addColorStop(1, "#0a1c14");
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, w, h);

  // Faint moon glow upper-right (just to give the eye a focal point).
  const mhx = w * 0.78;
  const mhy = h * 0.2;
  const mhr = Math.min(w, h) * 0.32;
  const moonGlow = ctx.createRadialGradient(mhx, mhy, 0, mhx, mhy, mhr);
  moonGlow.addColorStop(0, "rgba(180, 220, 200, 0.22)");
  moonGlow.addColorStop(1, "rgba(180, 220, 200, 0)");
  ctx.fillStyle = moonGlow;
  ctx.fillRect(0, 0, w, h);

  // Tree silhouettes per layer.
  const layers = buildForest(w, h);
  for (let li = 0; li < layers.length; li++) {
    const layer = layers[li];
    for (const tree of layer.trees) {
      const ty = layer.baseY;
      // Trunk.
      ctx.fillStyle = layer.color;
      ctx.fillRect(tree.x - tree.trunkW / 2, ty - tree.trunkH, tree.trunkW, tree.trunkH);
      // Canopy: a few overlapping ovals so it looks bushy, not perfectly round.
      const cRng = rand(tree.seed);
      for (let k = 0; k < 5; k++) {
        const ox = (cRng() - 0.5) * tree.canopyR * 0.8;
        const oy = -tree.trunkH + (cRng() - 0.5) * tree.canopyR * 0.4;
        const r = tree.canopyR * (0.7 + cRng() * 0.4);
        ctx.beginPath();
        ctx.ellipse(tree.x + ox, ty + oy, r, r * 0.78, 0, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    // Mist band BETWEEN layers (drawn after the layer's trees so it veils
    // them slightly from view).
    if (li < layers.length - 1) {
      const mist = ctx.createLinearGradient(0, layer.baseY - 40, 0, layer.baseY + 40);
      mist.addColorStop(0, "rgba(180, 200, 200, 0)");
      mist.addColorStop(0.5, "rgba(180, 200, 200, 0.12)");
      mist.addColorStop(1, "rgba(180, 200, 200, 0)");
      ctx.fillStyle = mist;
      ctx.fillRect(0, layer.baseY - 40, w, 80);
    }
  }

  // Fireflies — 40 of them, each following a slow Lissajous curve with
  // a per-firefly brightness pulse. Drawn with two passes: outer halo
  // (radial gradient) and inner hot core (solid circle).
  const flyRng = rand(73);
  ctx.globalCompositeOperation = "lighter";
  for (let i = 0; i < 40; i++) {
    const ax = 0.15 + flyRng() * 0.7;
    const ay = 0.25 + flyRng() * 0.55;
    const rx = 0.04 + flyRng() * 0.12;
    const ry = 0.05 + flyRng() * 0.1;
    const sx = 0.5 + flyRng() * 1.5;
    const sy = 0.6 + flyRng() * 1.7;
    const phase = flyRng() * 6.28;
    const pulsePhase = flyRng() * 6.28;
    const x = (ax + Math.sin(t * sx * 0.2 + phase) * rx) * w;
    const y = (ay + Math.cos(t * sy * 0.25 + phase * 1.3) * ry) * h;
    const pulse = 0.45 + 0.55 * (0.5 + 0.5 * Math.sin(t * 2.2 + pulsePhase));
    const haloR = 18 * pulse * (h / 720);
    const halo = ctx.createRadialGradient(x, y, 0, x, y, haloR);
    halo.addColorStop(0, `rgba(255, 220, 140, ${0.85 * pulse})`);
    halo.addColorStop(0.4, `rgba(255, 200, 120, ${0.35 * pulse})`);
    halo.addColorStop(1, "rgba(255, 200, 120, 0)");
    ctx.fillStyle = halo;
    ctx.fillRect(x - haloR, y - haloR, haloR * 2, haloR * 2);
    // Hot core.
    ctx.fillStyle = `rgba(255, 255, 220, ${pulse})`;
    ctx.beginPath();
    ctx.arc(x, y, 1.6 * (h / 720), 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalCompositeOperation = "source-over";
}

export const ANIMATED_BACKGROUNDS: AnimatedBackgroundDef[] = [
  { id: "anim:aurora", i18nKey: "background.aurora", draw: drawAurora },
  { id: "anim:bokeh", i18nKey: "background.bokeh", draw: drawBokeh },
  { id: "anim:waves", i18nKey: "background.wavesAnim", draw: drawWaves },
  { id: "anim:particles", i18nKey: "background.particles", draw: drawParticles },
  { id: "anim:cityscape", i18nKey: "background.cityscape", draw: drawCityscape },
  { id: "anim:cherryBlossoms", i18nKey: "background.cherryBlossoms", draw: drawCherryBlossoms },
  { id: "anim:fireflies", i18nKey: "background.fireflies", draw: drawFireflies },
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

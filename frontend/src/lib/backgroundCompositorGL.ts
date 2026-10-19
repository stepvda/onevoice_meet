/**
 * WebGL2 compositor for the virtual-background / blur processor.
 *
 * Takes the camera frame plus MediaPipe's low-resolution person-confidence
 * mask and produces the final composited frame on the GPU. Three techniques
 * do the heavy lifting for edge quality:
 *
 *   1. Colour guided filter (He, Sun & Tang, "Guided Image Filtering",
 *      ECCV 2010 / TPAMI 2013; the "fast" subsampled variant from He & Sun
 *      2015). The 256×256 mask is only a rough silhouette once stretched
 *      over a 720p frame — its edge can sit several pixels off the real
 *      contour. The guided filter fits, in every small window, a linear
 *      model `mask ≈ a·RGB + b` against the camera image and then
 *      evaluates that model at full resolution. Where the window straddles
 *      a real colour edge, the mask edge snaps onto it; in flat regions the
 *      filter just smooths. Coefficients are solved at a reduced "guide"
 *      resolution and bilinearly upsampled, which is what keeps it cheap.
 *
 *   2. Person-excluding background blur. Every blur tap is weighted by
 *      (1 − person), accumulated premultiplied and renormalised at the
 *      end, so the person's own colours never smear outwards into the
 *      blurred backdrop. That smear is what produces the bright/dark halo
 *      around the silhouette with a plain whole-frame blur. Clamped
 *      sampling also removes the dark vignette a canvas `blur()` filter
 *      produces at the frame borders.
 *
 *   3. Edge colour decontamination. A pixel on the contour is a mix of
 *      person and old background (I = α·F + (1−α)·B). Pasted over a new
 *      background as-is, it keeps a rim of the old one (a blue fringe on
 *      hair in front of a blue wall). The person-excluding blur above is
 *      a local estimate of B, so the compositor solves for F and blends
 *      that instead.
 *
 * Conventions: every texture stores image row 0 (the top of the picture) at
 * v = 0, and every offscreen pass renders row 0 at gl_FragCoord.y = 0, so
 * intermediate textures share that orientation. Only the final pass to the
 * canvas flips, because the canvas presents gl y = 0 at the bottom.
 */

export type CompositeMode = "blur" | "image";

export interface CompositeParams {
  frame: VideoFrame;
  /** Person confidence in [0, 1], row-major, top row first. */
  mask: Float32Array;
  maskWidth: number;
  maskHeight: number;
  /** Source window in normalised frame UV (auto-framing crop). */
  crop: { x: number; y: number; w: number; h: number };
  brightness: number;
  contrast: number;
  mode: CompositeMode;
  /** Gaussian sigma of the background blur, in output pixels. */
  blurSigma: number;
  background: ImageBitmap | null;
  tuning: EdgeTuning;
}

export interface EdgeTuning {
  /** Guided-filter window radius, in guide-resolution pixels. */
  radius: number;
  /** Guided-filter regulariser. Smaller = edges follow the image harder. */
  eps: number;
  /** Refined-mask ramp: alpha 0 at `edgeLow`, 1 at `edgeHigh`. */
  edgeLow: number;
  edgeHigh: number;
}

// Guide resolution: long side of the frame is scaled to this many pixels
// for the guided-filter statistics. 480 keeps the window (2r+1 at this
// scale) around 20-25 output pixels on a 720p feed — wide enough to cover
// the mask's misplacement, narrow enough to keep fine structure.
const GUIDE_LONG_SIDE = 480;
// Background blur runs at a quarter of the output resolution; the blur
// itself hides the downsampling.
const BLUR_DOWNSCALE = 4;
// Minimum weight per blur tap. Inside large person regions every tap is
// "person", so the weighted average would be 0/0; this floor degrades it
// to a plain blur there instead (where it is hidden by the person anyway),
// while near real background the floor is ~500× smaller than a background
// tap and has no visible effect.
const BLUR_WEIGHT_FLOOR = 0.002;
// With an image background the person-excluding blur only serves as the
// local background estimate for decontamination, so it stays tight
// (sigma in output pixels).
const DECONTAM_SIGMA = 6;
// Lower bound on α when solving I = α·F + (1−α)·B for F: below it the
// pixel is mostly background and dividing by α would amplify noise.
const DECONTAM_MIN_ALPHA = 0.2;

const VERT = `#version 300 es
uniform bool uFlipY;
out vec2 vUv;
void main() {
  // One oversized triangle covering the viewport; no vertex buffers needed.
  vec2 p = vec2(gl_VertexID == 1 ? 3.0 : -1.0, gl_VertexID == 2 ? 3.0 : -1.0);
  vUv = p * 0.5 + 0.5;
  if (uFlipY) vUv.y = 1.0 - vUv.y;
  gl_Position = vec4(p, 0.0, 1.0);
}`;

// Guide statistics, one pass, four render targets:
//   o0 = (I.rgb, p)       o1 = (I.rgb * p, 0)
//   o2 = (rr, rg, rb, gg) o3 = (gb, bb, 0, 0)
// I is a 2×2-tap average of the frame so the downsample doesn't alias.
const STATS_FRAG = `#version 300 es
precision highp float;
uniform sampler2D uFrame;
uniform sampler2D uMask;
uniform vec2 uTexel;
in vec2 vUv;
layout(location = 0) out vec4 o0;
layout(location = 1) out vec4 o1;
layout(location = 2) out vec4 o2;
layout(location = 3) out vec4 o3;
void main() {
  vec2 d = uTexel * 0.25;
  vec3 I = 0.25 * (texture(uFrame, vUv + vec2(-d.x, -d.y)).rgb
                 + texture(uFrame, vUv + vec2( d.x, -d.y)).rgb
                 + texture(uFrame, vUv + vec2(-d.x,  d.y)).rgb
                 + texture(uFrame, vUv + vec2( d.x,  d.y)).rgb);
  float p = texture(uMask, vUv).r;
  o0 = vec4(I, p);
  o1 = vec4(I * p, 0.0);
  o2 = vec4(I.r * I.r, I.r * I.g, I.r * I.b, I.g * I.g);
  o3 = vec4(I.g * I.b, I.b * I.b, 0.0, 0.0);
}`;

// Separable box filter over N textures at once (clamp-to-edge).
function boxFrag(n: number): string {
  const decl = Array.from({ length: n }, (_, i) =>
    `uniform highp sampler2D uT${i};\nlayout(location = ${i}) out vec4 o${i};`).join("\n");
  const acc = Array.from({ length: n }, (_, i) => `vec4 s${i} = vec4(0.0);`).join("\n  ");
  const add = Array.from({ length: n }, (_, i) => `s${i} += texelFetch(uT${i}, q, 0);`).join("\n    ");
  const out = Array.from({ length: n }, (_, i) => `o${i} = s${i} * inv;`).join("\n  ");
  return `#version 300 es
precision highp float;
${decl}
uniform ivec2 uDir;
uniform int uRadius;
void main() {
  ivec2 c = ivec2(gl_FragCoord.xy);
  ivec2 hi = textureSize(uT0, 0) - 1;
  ${acc}
  for (int i = -uRadius; i <= uRadius; ++i) {
    ivec2 q = clamp(c + uDir * i, ivec2(0), hi);
    ${add}
  }
  float inv = 1.0 / float(2 * uRadius + 1);
  ${out}
}`;
}

// Per-window linear coefficients: a = (Σ + εI)⁻¹ cov(I, p), b = p̄ − a·Ī.
const COEF_FRAG = `#version 300 es
precision highp float;
uniform highp sampler2D uM0;
uniform highp sampler2D uM1;
uniform highp sampler2D uM2;
uniform highp sampler2D uM3;
uniform float uEps;
out vec4 o;
void main() {
  ivec2 c = ivec2(gl_FragCoord.xy);
  vec4 m0 = texelFetch(uM0, c, 0);
  vec3 mI = m0.rgb;
  float mp = m0.a;
  vec3 cov = texelFetch(uM1, c, 0).rgb - mI * mp;
  vec4 m2 = texelFetch(uM2, c, 0);
  vec4 m3 = texelFetch(uM3, c, 0);
  float rr = m2.x - mI.r * mI.r + uEps;
  float rg = m2.y - mI.r * mI.g;
  float rb = m2.z - mI.r * mI.b;
  float gg = m2.w - mI.g * mI.g + uEps;
  float gb = m3.x - mI.g * mI.b;
  float bb = m3.y - mI.b * mI.b + uEps;
  mat3 sigma = mat3(rr, rg, rb, rg, gg, gb, rb, gb, bb);
  vec3 a = inverse(sigma) * cov;
  o = vec4(a, mp - dot(a, mI));
}`;

// Shared helpers for the passes that evaluate the refined mask.
const REFINE_GLSL = `
uniform sampler2D uFrame;
uniform sampler2D uCoef;
uniform vec2 uEdge;
float refinedAlpha(vec2 uv, vec3 I) {
  vec4 ab = texture(uCoef, uv);
  float q = dot(ab.rgb, I) + ab.a;
  return smoothstep(uEdge.x, uEdge.y, q);
}`;

// Background-blur source: premultiplied (rgb·w, w) with w = 1 − person.
const BLUR_SRC_FRAG = `#version 300 es
precision highp float;
${REFINE_GLSL}
uniform vec2 uTexel;
uniform float uFloor;
in vec2 vUv;
out vec4 o;
void main() {
  vec2 d = uTexel * 0.25;
  vec3 I = 0.25 * (texture(uFrame, vUv + vec2(-d.x, -d.y)).rgb
                 + texture(uFrame, vUv + vec2( d.x, -d.y)).rgb
                 + texture(uFrame, vUv + vec2(-d.x,  d.y)).rgb
                 + texture(uFrame, vUv + vec2( d.x,  d.y)).rgb);
  // Err towards excluding: anything the refined mask is even half sure of
  // counts as person for the purpose of feeding the background blur.
  vec4 ab = texture(uCoef, vUv);
  float person = smoothstep(uEdge.x - 0.2, uEdge.x + 0.1, dot(ab.rgb, I) + ab.a);
  float w = max(1.0 - person, uFloor);
  o = vec4(I * w, w);
}`;

// Separable Gaussian on premultiplied RGBA (clamp-to-edge, so no vignette).
const GAUSS_FRAG = `#version 300 es
precision highp float;
uniform highp sampler2D uT0;
uniform ivec2 uDir;
uniform float uSigma;
out vec4 o;
void main() {
  ivec2 c = ivec2(gl_FragCoord.xy);
  ivec2 hi = textureSize(uT0, 0) - 1;
  int r = int(min(ceil(uSigma * 3.0), 32.0));
  float k = -0.5 / (uSigma * uSigma);
  vec4 s = vec4(0.0);
  float wsum = 0.0;
  for (int i = -r; i <= r; ++i) {
    float w = exp(float(i * i) * k);
    s += w * texelFetch(uT0, clamp(c + uDir * i, ivec2(0), hi), 0);
    wsum += w;
  }
  o = s / wsum;
}`;

const COMPOSITE_FRAG = `#version 300 es
precision highp float;
${REFINE_GLSL}
uniform sampler2D uBgBlur;  // premultiplied person-excluding blur
uniform sampler2D uBgImage;
uniform bool uBlur;
uniform bool uHasBg;
uniform float uMinAlpha;
uniform vec4 uCrop;      // x, y, w, h in frame UV
uniform vec4 uBgCover;   // UV offset.xy, scale.zw for "cover" fit
uniform vec2 uExposure;  // brightness, contrast
in vec2 vUv;
out vec4 o;
vec3 expose(vec3 c) {
  // Same semantics (and order) as CSS "brightness(b) contrast(c)".
  c = clamp(c * uExposure.x, 0.0, 1.0);
  return clamp((c - 0.5) * uExposure.y + 0.5, 0.0, 1.0);
}
void main() {
  vec2 fuv = uCrop.xy + vUv * uCrop.zw;
  vec3 I = texture(uFrame, fuv).rgb;
  float alpha = refinedAlpha(fuv, I);
  vec4 b = texture(uBgBlur, fuv);
  vec3 localBg = b.rgb / max(b.a, 1e-4);
  // Decontaminate the contour: remove the old background's share.
  vec3 F = I;
  if (alpha < 1.0) {
    F = clamp((I - (1.0 - alpha) * localBg) / max(alpha, uMinAlpha), 0.0, 1.0);
  }
  vec3 bg;
  if (uBlur) {
    bg = expose(localBg);
  } else if (uHasBg) {
    bg = texture(uBgImage, uBgCover.xy + vUv * uBgCover.zw).rgb;
  } else {
    bg = vec3(0.0, 1.0, 0.0);
  }
  o = vec4(mix(bg, expose(F), alpha), 1.0);
}`;

interface Target {
  fb: WebGLFramebuffer;
  tex: WebGLTexture[];
  w: number;
  h: number;
}

interface Program {
  prog: WebGLProgram;
  loc: Map<string, WebGLUniformLocation | null>;
}

export class BackgroundCompositorGL {
  readonly canvas: OffscreenCanvas;
  private gl: WebGL2RenderingContext;
  private vao: WebGLVertexArrayObject;
  private progs: Record<"stats" | "box4" | "box1" | "coef" | "blurSrc" | "gauss" | "composite", Program>;
  private frameTex: WebGLTexture;
  private maskTex: WebGLTexture;
  private bgTex: WebGLTexture;
  private bgSource: ImageBitmap | null = null;
  private bgSize = { w: 0, h: 0 };
  // Size-dependent render targets, rebuilt when the frame size changes.
  private sized: {
    fw: number; fh: number;
    stats: Target; statsTmp: Target; coef: Target; coefTmp: Target;
    blurSrc: Target; blurTmp: Target; blur: Target;
  } | null = null;
  private lost = false;

  /** Returns null when WebGL2 or float render targets are unavailable. */
  static create(width: number, height: number): BackgroundCompositorGL | null {
    try {
      const canvas = new OffscreenCanvas(width, height);
      const gl = canvas.getContext("webgl2", {
        alpha: false,
        antialias: false,
        depth: false,
        stencil: false,
        premultipliedAlpha: false,
        preserveDrawingBuffer: false,
        powerPreference: "high-performance",
      });
      if (!gl || !gl.getExtension("EXT_color_buffer_float")) return null;
      return new BackgroundCompositorGL(canvas, gl);
    } catch (err) {
      console.warn("[backgroundCompositorGL] unavailable, using 2D fallback:", err);
      return null;
    }
  }

  private constructor(canvas: OffscreenCanvas, gl: WebGL2RenderingContext) {
    this.canvas = canvas;
    this.gl = gl;
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      this.lost = true;
    });
    this.vao = gl.createVertexArray()!;
    this.progs = {
      stats: this.program(STATS_FRAG),
      box4: this.program(boxFrag(4)),
      box1: this.program(boxFrag(1)),
      coef: this.program(COEF_FRAG),
      blurSrc: this.program(BLUR_SRC_FRAG),
      gauss: this.program(GAUSS_FRAG),
      composite: this.program(COMPOSITE_FRAG),
    };
    this.frameTex = this.texture(gl.LINEAR);
    this.maskTex = this.texture(gl.LINEAR);
    this.bgTex = this.texture(gl.LINEAR);
  }

  get isLost(): boolean {
    return this.lost || this.gl.isContextLost();
  }

  render(p: CompositeParams): void {
    const gl = this.gl;
    const fw = p.frame.displayWidth;
    const fh = p.frame.displayHeight;
    this.ensureSized(fw, fh);
    const s = this.sized!;

    gl.bindVertexArray(this.vao);
    gl.disable(gl.BLEND);
    gl.disable(gl.DEPTH_TEST);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);

    gl.bindTexture(gl.TEXTURE_2D, this.frameTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, p.frame);

    gl.bindTexture(gl.TEXTURE_2D, this.maskTex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16F, p.maskWidth, p.maskHeight, 0, gl.RED, gl.FLOAT, p.mask);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);

    // 1. Guide statistics → box filter (H then V).
    const st = s.stats;
    this.use("stats", st);
    this.bindTex(0, this.frameTex, "uFrame", this.progs.stats);
    this.bindTex(1, this.maskTex, "uMask", this.progs.stats);
    gl.uniform2f(this.loc(this.progs.stats, "uTexel"), 1 / st.w, 1 / st.h);
    this.draw();
    const radius = Math.max(1, Math.round(p.tuning.radius));
    this.box("box4", st, s.statsTmp, [1, 0], radius);
    this.box("box4", s.statsTmp, st, [0, 1], radius);

    // 2. Linear coefficients (a, b) → box filter → mean coefficients.
    this.use("coef", s.coef);
    for (let i = 0; i < 4; i++) this.bindTex(i, st.tex[i], `uM${i}`, this.progs.coef);
    gl.uniform1f(this.loc(this.progs.coef, "uEps"), p.tuning.eps);
    this.draw();
    this.box("box1", s.coef, s.coefTmp, [1, 0], radius);
    this.box("box1", s.coefTmp, s.coef, [0, 1], radius);

    // 3. Person-excluding blur: the backdrop in blur mode, and the local
    // background estimate for edge decontamination in both modes.
    const blur = p.mode === "blur";
    this.use("blurSrc", s.blurSrc);
    this.bindRefine(this.progs.blurSrc, p.tuning);
    gl.uniform2f(this.loc(this.progs.blurSrc, "uTexel"), 1 / s.blurSrc.w, 1 / s.blurSrc.h);
    gl.uniform1f(this.loc(this.progs.blurSrc, "uFloor"), BLUR_WEIGHT_FLOOR);
    this.draw();
    // Sigma is specified in output pixels; convert to blur-target pixels
    // and to frame pixels under the auto-framing crop (the backdrop is
    // sampled through the same crop as the person).
    const outSigma = blur ? p.blurSigma : DECONTAM_SIGMA;
    const sigma = Math.max(0.5, (outSigma * p.crop.w * s.blurSrc.w) / this.canvas.width);
    this.gauss(s.blurSrc, s.blurTmp, [1, 0], sigma);
    this.gauss(s.blurTmp, s.blur, [0, 1], sigma);
    if (!blur && p.background && p.background !== this.bgSource) {
      gl.bindTexture(gl.TEXTURE_2D, this.bgTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, p.background);
      this.bgSource = p.background;
      this.bgSize = { w: p.background.width, h: p.background.height };
    }

    // 4. Composite to the canvas.
    const c = this.progs.composite;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.useProgram(c.prog);
    gl.uniform1i(this.loc(c, "uFlipY"), 1);
    this.bindRefine(c, p.tuning);
    this.bindTex(2, s.blur.tex[0], "uBgBlur", c);
    this.bindTex(3, this.bgTex, "uBgImage", c);
    gl.uniform1f(this.loc(c, "uMinAlpha"), DECONTAM_MIN_ALPHA);
    gl.uniform1i(this.loc(c, "uBlur"), blur ? 1 : 0);
    gl.uniform1i(this.loc(c, "uHasBg"), !blur && p.background ? 1 : 0);
    gl.uniform4f(this.loc(c, "uCrop"), p.crop.x, p.crop.y, p.crop.w, p.crop.h);
    const cover = this.coverFit();
    gl.uniform4f(this.loc(c, "uBgCover"), cover[0], cover[1], cover[2], cover[3]);
    gl.uniform2f(this.loc(c, "uExposure"), p.brightness, p.contrast);
    this.draw();
  }

  dispose(): void {
    const gl = this.gl;
    this.freeSized();
    for (const t of [this.frameTex, this.maskTex, this.bgTex]) gl.deleteTexture(t);
    for (const p of Object.values(this.progs)) gl.deleteProgram(p.prog);
    gl.deleteVertexArray(this.vao);
    this.bgSource = null;
    // Release the context now rather than waiting for GC.
    gl.getExtension("WEBGL_lose_context")?.loseContext();
  }

  // ── helpers ────────────────────────────────────────────────────────

  /** "cover" fit of the background image onto the output aspect ratio. */
  private coverFit(): [number, number, number, number] {
    const { w: bw, h: bh } = this.bgSize;
    if (!bw || !bh) return [0, 0, 1, 1];
    const outAspect = this.canvas.width / this.canvas.height;
    const bgAspect = bw / bh;
    if (bgAspect > outAspect) {
      const sx = outAspect / bgAspect;
      return [(1 - sx) / 2, 0, sx, 1];
    }
    const sy = bgAspect / outAspect;
    return [0, (1 - sy) / 2, 1, sy];
  }

  private bindRefine(p: Program, t: EdgeTuning): void {
    this.bindTex(0, this.frameTex, "uFrame", p);
    this.bindTex(1, this.sized!.coef.tex[0], "uCoef", p);
    this.gl.uniform2f(this.loc(p, "uEdge"), t.edgeLow, t.edgeHigh);
  }

  private box(name: "box4" | "box1", src: Target, dst: Target, dir: [number, number], radius: number): void {
    const p = this.progs[name];
    this.use(name, dst);
    src.tex.forEach((t, i) => this.bindTex(i, t, `uT${i}`, p));
    this.gl.uniform2i(this.loc(p, "uDir"), dir[0], dir[1]);
    this.gl.uniform1i(this.loc(p, "uRadius"), radius);
    this.draw();
  }

  private gauss(src: Target, dst: Target, dir: [number, number], sigma: number): void {
    const p = this.progs.gauss;
    this.use("gauss", dst);
    this.bindTex(0, src.tex[0], "uT0", p);
    this.gl.uniform2i(this.loc(p, "uDir"), dir[0], dir[1]);
    this.gl.uniform1f(this.loc(p, "uSigma"), sigma);
    this.draw();
  }

  private use(name: keyof BackgroundCompositorGL["progs"], target: Target): void {
    const gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, target.fb);
    gl.viewport(0, 0, target.w, target.h);
    gl.useProgram(this.progs[name].prog);
    gl.uniform1i(this.loc(this.progs[name], "uFlipY"), 0);
  }

  private draw(): void {
    this.gl.drawArrays(this.gl.TRIANGLES, 0, 3);
  }

  private bindTex(unit: number, tex: WebGLTexture, name: string, p: Program): void {
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.uniform1i(this.loc(p, name), unit);
  }

  private loc(p: Program, name: string): WebGLUniformLocation | null {
    let l = p.loc.get(name);
    if (l === undefined) {
      l = this.gl.getUniformLocation(p.prog, name);
      p.loc.set(name, l);
    }
    return l;
  }

  private program(frag: string): Program {
    const gl = this.gl;
    const compile = (type: number, src: string) => {
      const sh = gl.createShader(type)!;
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        throw new Error(`shader compile failed: ${gl.getShaderInfoLog(sh)}`);
      }
      return sh;
    };
    const prog = gl.createProgram()!;
    const vs = compile(gl.VERTEX_SHADER, VERT);
    const fs = compile(gl.FRAGMENT_SHADER, frag);
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error(`program link failed: ${gl.getProgramInfoLog(prog)}`);
    }
    return { prog, loc: new Map() };
  }

  private texture(filter: number): WebGLTexture {
    const gl = this.gl;
    const t = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  private target(w: number, h: number, count: number, internal: number, filter: number): Target {
    const gl = this.gl;
    const fb = gl.createFramebuffer()!;
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    const tex: WebGLTexture[] = [];
    const bufs: number[] = [];
    for (let i = 0; i < count; i++) {
      const t = this.texture(filter);
      gl.texStorage2D(gl.TEXTURE_2D, 1, internal, w, h);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0 + i, gl.TEXTURE_2D, t, 0);
      tex.push(t);
      bufs.push(gl.COLOR_ATTACHMENT0 + i);
    }
    gl.drawBuffers(bufs);
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) {
      throw new Error("incomplete framebuffer");
    }
    return { fb, tex, w, h };
  }

  private ensureSized(fw: number, fh: number): void {
    if (this.sized && this.sized.fw === fw && this.sized.fh === fh) return;
    this.freeSized();
    const gl = this.gl;
    const g = GUIDE_LONG_SIDE / Math.max(fw, fh);
    const gw = Math.max(16, Math.round(fw * Math.min(1, g)));
    const gh = Math.max(16, Math.round(fh * Math.min(1, g)));
    const bw = Math.max(16, Math.round(fw / BLUR_DOWNSCALE));
    const bh = Math.max(16, Math.round(fh / BLUR_DOWNSCALE));
    // Statistics need fp32: var(I) = E[I²] − E[I]² cancels catastrophically
    // in fp16. They're only ever texelFetch'd, so fp32 needn't be filterable.
    // The coefficients are well-conditioned and must be bilinearly sampled
    // at full resolution, so they're fp16 (filterable in core WebGL2).
    this.sized = {
      fw, fh,
      stats: this.target(gw, gh, 4, gl.RGBA32F, gl.NEAREST),
      statsTmp: this.target(gw, gh, 4, gl.RGBA32F, gl.NEAREST),
      coef: this.target(gw, gh, 1, gl.RGBA16F, gl.LINEAR),
      coefTmp: this.target(gw, gh, 1, gl.RGBA16F, gl.LINEAR),
      blurSrc: this.target(bw, bh, 1, gl.RGBA16F, gl.LINEAR),
      blurTmp: this.target(bw, bh, 1, gl.RGBA16F, gl.LINEAR),
      blur: this.target(bw, bh, 1, gl.RGBA16F, gl.LINEAR),
    };
  }

  private freeSized(): void {
    if (!this.sized) return;
    const gl = this.gl;
    for (const [k, v] of Object.entries(this.sized)) {
      if (k === "fw" || k === "fh") continue;
      const t = v as Target;
      t.tex.forEach((x) => gl.deleteTexture(x));
      gl.deleteFramebuffer(t.fb);
    }
    this.sized = null;
  }
}

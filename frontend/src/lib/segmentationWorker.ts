/**
 * Runs MediaPipe segmentation in a Web Worker, off the video pipeline.
 *
 * In-thread, `segmentForVideo` blocks the main thread for the whole
 * inference plus the GPU→CPU mask readback: 25-45 ms on every model frame
 * with the multiclass model on an M1, which LiveKit and the meeting UI
 * share. Here the transform only posts a clone of the frame and carries on
 * compositing every frame with the latest mask it has; masks arrive at
 * whatever rate the model sustains (~15/s for multiclass on a busy M1 —
 * inference is slower from a worker, but off the critical path), and the
 * guided filter re-snaps each one onto the current frame's contours.
 *
 * Loading: tasks-vision fetches its wasm loader with `importScripts`, which
 * only exists in classic workers (Vite serves module workers in dev). The
 * worker therefore starts from a blob: URL and pulls in the CommonJS
 * bundle from jsdelivr. jsdelivr serves `.cjs` as `application/node` with
 * `nosniff`, which `importScripts` refuses, so the bundle text is fetched
 * and re-wrapped in a JavaScript blob. All of it stays inside the CSP the
 * SPA already has (worker-src/script-src blob:, cdn.jsdelivr.net in
 * script-src and connect-src) — see caddy/Caddyfile.
 */

export interface WorkerModel {
  url: string;
  maskIndex: number;
  isBackground: boolean;
}

export interface WorkerMask {
  /** Person confidence in [0, 1], row-major, top row first. */
  mask: Float32Array;
  width: number;
  height: number;
  /** Inference + readback time inside the worker. */
  ms: number;
  /** Mean Rec. 601 luma (0-255) of the segmented frame, for auto-exposure. */
  luma: number;
}

// Plain ES2017 that runs as a classic worker; kept as a string so it needs
// no bundler support and can be started from a blob: URL.
const WORKER_SRC = `
"use strict";
let segmenter = null;
let model = null;

// When reading a mask back on a Mac, tasks-vision tells iPads from desktops
// with \`"ontouchend" in self.document\`, which throws in a worker (no
// document). An empty stand-in answers "not a touch device". Its other
// document uses sit behind importScripts/OffscreenCanvas checks that a
// Chromium worker passes, so they never see it.
if (typeof self.document === "undefined") self.document = {};

async function init(msg) {
  const res = await fetch(msg.bundleUrl);
  if (!res.ok) throw new Error("tasks-vision bundle: HTTP " + res.status);
  const code = "var module = { exports: {} }; var exports = module.exports;\\n" +
    (await res.text()) + "\\nself.__vision = module.exports;";
  const url = URL.createObjectURL(new Blob([code], { type: "text/javascript" }));
  try {
    importScripts(url);
  } finally {
    URL.revokeObjectURL(url);
  }
  const vision = self.__vision;
  const fileSet = await vision.FilesetResolver.forVisionTasks(msg.wasmBase);
  segmenter = await vision.ImageSegmenter.createFromOptions(fileSet, {
    baseOptions: { modelAssetPath: msg.model.url, delegate: "GPU" },
    runningMode: "VIDEO",
    outputCategoryMask: false,
    outputConfidenceMasks: true,
  });
  model = msg.model;
}

let pixelCtx = null;

async function segment(msg) {
  // Scale the frame to the model's input size on the GPU, then hand
  // MediaPipe plain CPU pixels. Measured on a busy M1: a GPU-backed
  // ImageBitmap straight into segmentForVideo, or drawing the full-size
  // frame onto the CPU canvas, each cost ~3-4 fps end to end (GPU upload
  // contention / a 720p readback); this order reads back only 256×256.
  let small;
  try {
    small = await createImageBitmap(msg.frame, {
      resizeWidth: msg.size, resizeHeight: msg.size, resizeQuality: "medium",
    });
  } finally {
    msg.frame.close();
  }
  if (!pixelCtx || pixelCtx.canvas.width !== msg.size) {
    pixelCtx = new OffscreenCanvas(msg.size, msg.size).getContext("2d", { willReadFrequently: true });
  }
  try {
    pixelCtx.drawImage(small, 0, 0);
  } finally {
    small.close();
  }
  const pixels = pixelCtx.getImageData(0, 0, msg.size, msg.size);

  let out = null, width = 0, height = 0;
  const t0 = performance.now();
  segmenter.segmentForVideo(pixels, msg.timestamp, (r) => {
    const m = r.confidenceMasks && r.confidenceMasks[model.maskIndex];
    if (!m) return;
    const src = m.getAsFloat32Array();
    out = new Float32Array(src.length);
    if (model.isBackground) {
      for (let i = 0; i < src.length; i++) out[i] = 1 - src[i];
    } else {
      out.set(src);
    }
    width = m.width;
    height = m.height;
  });
  const ms = performance.now() - t0;
  if (!out) {
    self.postMessage({ type: "nomask" });
    return;
  }
  // Mean Rec. 601 luma for auto-exposure, from every 4th pixel.
  const d = pixels.data;
  let sum = 0;
  for (let i = 0; i < d.length; i += 16) sum += 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
  const luma = sum / (d.length / 16);
  self.postMessage({ type: "mask", mask: out, width, height, ms, luma }, [out.buffer]);
}

self.onmessage = async (e) => {
  const msg = e.data;
  try {
    if (msg.type === "init") {
      await init(msg);
      self.postMessage({ type: "ready" });
    } else if (msg.type === "segment") {
      await segment(msg);
    }
  } catch (err) {
    self.postMessage({ type: "error", error: String((err && err.stack) || err) });
  }
};
`;

// Generous: covers the 16 MB multiclass download on a slow connection.
const INIT_TIMEOUT_MS = 60_000;

export class SegmentationWorker {
  /** True while a frame is with the worker; the caller skips new ones. */
  busy = false;
  onMask: ((m: WorkerMask) => void) | null = null;
  onError: ((err: string) => void) | null = null;
  readonly model: WorkerModel;
  private worker: Worker;
  private sentAt = 0;

  private constructor(worker: Worker, model: WorkerModel) {
    this.worker = worker;
    this.model = model;
    worker.onmessage = (e: MessageEvent) => {
      const msg = e.data;
      if (msg.type === "mask") {
        this.busy = false;
        this.onMask?.({ mask: msg.mask, width: msg.width, height: msg.height, ms: msg.ms, luma: msg.luma });
      } else if (msg.type === "nomask") {
        this.busy = false;
      } else if (msg.type === "error") {
        this.busy = false;
        this.onError?.(msg.error);
      }
    };
  }

  /** Starts a worker and loads `model` in it; rejects if anything fails. */
  static create(opts: { bundleUrl: string; wasmBase: string; model: WorkerModel }): Promise<SegmentationWorker> {
    const src = URL.createObjectURL(new Blob([WORKER_SRC], { type: "text/javascript" }));
    let worker: Worker;
    try {
      worker = new Worker(src);
    } finally {
      URL.revokeObjectURL(src);
    }
    return new Promise((resolve, reject) => {
      const fail = (why: string) => {
        clearTimeout(timer);
        worker.terminate();
        reject(new Error(why));
      };
      const timer = setTimeout(() => fail("segmentation worker init timed out"), INIT_TIMEOUT_MS);
      worker.onerror = (e) => fail(`segmentation worker failed: ${e.message}`);
      worker.onmessage = (e: MessageEvent) => {
        if (e.data.type === "ready") {
          clearTimeout(timer);
          worker.onerror = null;
          resolve(new SegmentationWorker(worker, opts.model));
        } else if (e.data.type === "error") {
          fail(e.data.error);
        }
      };
      worker.postMessage({ type: "init", bundleUrl: opts.bundleUrl, wasmBase: opts.wasmBase, model: opts.model });
    });
  }

  /**
   * Hands `frame` (transferred, then closed by the worker) to the model,
   * scaled to `size`×`size`. Pass a clone if the caller still needs it.
   */
  segment(frame: VideoFrame, size: number, timestamp: number): void {
    this.busy = true;
    this.sentAt = performance.now();
    this.worker.postMessage({ type: "segment", frame, size, timestamp }, [frame]);
  }

  /** How long the current frame has been with the worker (0 if idle). */
  get busyMs(): number {
    return this.busy ? performance.now() - this.sentAt : 0;
  }

  terminate(): void {
    this.onMask = null;
    this.onError = null;
    this.worker.terminate();
  }
}

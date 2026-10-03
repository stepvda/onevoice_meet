import type { WhiteboardShapeDTO } from "../api";

export interface StrokeLike {
  points: Array<{ x: number; y: number }>;
  color: string;
  width: number;
}

export interface RenderOptions {
  width: number;
  height: number;
  background?: string;
}

function normaliseStroke(raw: Record<string, unknown>): StrokeLike | null {
  const points = raw.points as StrokeLike["points"] | undefined;
  if (!Array.isArray(points) || points.length === 0) return null;
  return {
    points,
    color: (raw.color as string) ?? "#fbbf24",
    width: (raw.width as number) ?? 3,
  };
}

/** Render strokes and shapes to a canvas context. Coordinates are normalised
 * to [0,1] of the source canvas, so this scales to any output size. */
export function renderBoard(
  ctx: CanvasRenderingContext2D,
  strokes: Array<Record<string, unknown>>,
  shapes: WhiteboardShapeDTO[],
  opts: RenderOptions,
): void {
  const { width: w, height: h } = opts;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = opts.background ?? "#0b1220";
  ctx.fillRect(0, 0, w, h);

  for (const raw of strokes) {
    if (raw.type && raw.type !== "stroke") continue;
    const s = normaliseStroke(raw);
    if (!s) continue;
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(s.points[0].x * w, s.points[0].y * h);
    for (let i = 1; i < s.points.length; i++) {
      ctx.lineTo(s.points[i].x * w, s.points[i].y * h);
    }
    ctx.stroke();
  }

  for (const sh of shapes) {
    const px = sh.x * w;
    const py = sh.y * h;
    const pw = sh.w * w;
    const ph = sh.h * h;
    ctx.strokeStyle = sh.color;
    ctx.lineWidth = sh.stroke_width;
    if (sh.kind === "rect") {
      ctx.strokeRect(px, py, pw, ph);
    } else if (sh.kind === "ellipse") {
      ctx.beginPath();
      ctx.ellipse(px + pw / 2, py + ph / 2, Math.abs(pw / 2), Math.abs(ph / 2), 0, 0, Math.PI * 2);
      ctx.stroke();
    } else if (sh.kind === "text") {
      const fs = (sh.font_size ?? 20) * (h / 720);
      ctx.fillStyle = sh.color;
      ctx.font = `${Math.max(8, fs)}px system-ui, sans-serif`;
      ctx.textBaseline = "top";
      let y = py;
      for (const line of (sh.text ?? "").split("\n")) {
        ctx.fillText(line, px, y);
        y += fs * 1.2;
      }
    }
  }
}

/** Build a 1920×1080 PNG snapshot without the selection outline. */
export async function buildSnapshot(
  strokes: Array<Record<string, unknown>>,
  shapes: WhiteboardShapeDTO[],
  background = "#0b1220",
): Promise<Blob> {
  const canvas = document.createElement("canvas");
  canvas.width = 1920;
  canvas.height = 1080;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas unavailable");
  renderBoard(ctx, strokes, shapes, { width: 1920, height: 1080, background });
  return await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("toBlob failed"))), "image/png");
  });
}

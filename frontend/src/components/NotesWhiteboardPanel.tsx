import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";
import { useRoomContext } from "@livekit/components-react";
import { RoomEvent } from "livekit-client";
import {
  Brush,
  Circle as CircleIcon,
  Download,
  Eraser,
  FileText,
  MousePointer2,
  PenTool,
  Square as SquareIcon,
  Type,
  X,
} from "lucide-react";
import jsPDF from "jspdf";
import { api, type WhiteboardShapeDTO } from "../lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Tab to switch to next time the panel opens. */
  initialTab?: Tab | null;
  onConsumeInitialTab?: () => void;
}

type Tab = "notes" | "board";

export const NOTES_TOPIC = "meet-notes";
export const BOARD_TOPIC = "meet-board";

const TEXT_ENC = new TextEncoder();
const TEXT_DEC = new TextDecoder();

// Persisted panel width. Stored separately for notes/board because the
// whiteboard usually wants more room than the notes textarea.
const WIDTH_STORAGE_KEY = "meet-notes-panel-width";
const DEFAULT_WIDTH = 384;
const MIN_WIDTH = 280;
const MAX_WIDTH = 1100;

// Stroke and shape packet shapes used over the LiveKit data channel.
interface StrokePacket {
  v: 1;
  type: "stroke";
  points: Array<{ x: number; y: number }>;
  color: string;
  width: number;
}
interface ClearPacket {
  v: 1;
  type: "clear";
}
interface ShapePacket {
  v: 1;
  type: "shape";
  shape: WhiteboardShapeDTO;
}
interface ShapeDeletePacket {
  v: 1;
  type: "shape-delete";
  id: string;
}
type BoardPacket = StrokePacket | ClearPacket | ShapePacket | ShapeDeletePacket;


/**
 * Combined collaborative notes + whiteboard side panel.
 *
 * The panel has a draggable splitter on its left edge so the host can grow
 * the whiteboard to the size they want — the underlying canvas resizes
 * (the drawings don't scale).
 */
export default function NotesWhiteboardPanel({ open, onClose, initialTab, onConsumeInitialTab }: Props) {
  const { t } = useTranslation();
  const { roomName = "" } = useParams();
  const room = useRoomContext();
  const [tab, setTab] = useState<Tab>("notes");
  const [widthPx, setWidthPx] = useState<number>(() => {
    if (typeof window === "undefined") return DEFAULT_WIDTH;
    const v = parseInt(window.localStorage.getItem(WIDTH_STORAGE_KEY) ?? "", 10);
    return Number.isFinite(v) && v >= MIN_WIDTH && v <= MAX_WIDTH ? v : DEFAULT_WIDTH;
  });

  useEffect(() => {
    if (open && initialTab) {
      setTab(initialTab);
      onConsumeInitialTab?.();
    }
  }, [open, initialTab, onConsumeInitialTab]);

  // Persist width across reloads.
  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(WIDTH_STORAGE_KEY, String(widthPx));
  }, [widthPx]);

  if (!open) return null;
  return (
    <aside
      data-testid="notes-board-panel"
      role="complementary"
      aria-label={t("notes.title", { defaultValue: "Notes & whiteboard" })}
      className="h-full flex-shrink-0 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col relative"
      style={{ width: `${widthPx}px` }}
    >
      <PanelResizer
        widthPx={widthPx}
        onChange={(w) => setWidthPx(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, w)))}
      />
      <header className="flex items-center justify-between px-4 py-3 border-b border-primary-700">
        <h2 className="text-sm font-semibold text-slate-100">
          {t("notes.title", { defaultValue: "Notes & whiteboard" })}
        </h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("notes.close", { defaultValue: "Close notes" })}
          className="p-1 rounded hover:bg-primary-700 text-slate-300"
        >
          <X size={18} />
        </button>
      </header>
      <div className="flex border-b border-primary-700 text-sm">
        <button
          type="button"
          onClick={() => setTab("notes")}
          aria-pressed={tab === "notes" ? "true" : "false"}
          className={[
            "flex-1 py-2 inline-flex items-center justify-center gap-1.5",
            tab === "notes" ? "text-accent-400 border-b-2 border-accent-500" : "text-slate-400 hover:text-slate-200",
          ].join(" ")}
        >
          <FileText size={14} /> {t("notes.tabNotes", { defaultValue: "Notes" })}
        </button>
        <button
          type="button"
          onClick={() => setTab("board")}
          aria-pressed={tab === "board" ? "true" : "false"}
          className={[
            "flex-1 py-2 inline-flex items-center justify-center gap-1.5",
            tab === "board" ? "text-accent-400 border-b-2 border-accent-500" : "text-slate-400 hover:text-slate-200",
          ].join(" ")}
        >
          <Brush size={14} /> {t("notes.tabBoard", { defaultValue: "Whiteboard" })}
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-hidden">
        {tab === "notes" ? <NotesTab room={room} roomName={roomName} /> : <Whiteboard room={room} roomName={roomName} />}
      </div>
    </aside>
  );
}


/** Thin invisible drag handle on the panel's left edge. */
function PanelResizer({ widthPx, onChange }: { widthPx: number; onChange: (w: number) => void }) {
  const startRef = useRef<{ clientX: number; startWidth: number } | null>(null);

  function onPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    e.preventDefault();
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    startRef.current = { clientX: e.clientX, startWidth: widthPx };
  }
  function onPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    if (!startRef.current) return;
    // Dragging LEFT grows the panel (we're on the left edge); RIGHT shrinks.
    const dx = startRef.current.clientX - e.clientX;
    onChange(startRef.current.startWidth + dx);
  }
  function onPointerUp(e: React.PointerEvent<HTMLDivElement>) {
    if (!startRef.current) return;
    (e.target as HTMLElement).releasePointerCapture(e.pointerId);
    startRef.current = null;
  }

  return (
    <div
      data-testid="notes-panel-resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize panel"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      className="absolute top-0 left-0 h-full w-2 -ml-1 z-20 cursor-col-resize hover:bg-accent-500/30"
    />
  );
}


function NotesTab({ room, roomName }: { room: ReturnType<typeof useRoomContext>; roomName: string }) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [loaded, setLoaded] = useState(false);
  const skipNextLocalSave = useRef(false);

  useEffect(() => {
    let cancelled = false;
    api
      .getNotes(roomName)
      .then((r) => {
        if (cancelled) return;
        skipNextLocalSave.current = true;
        setText(r.notes ?? "");
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
    return () => {
      cancelled = true;
    };
  }, [roomName]);

  useEffect(() => {
    const onData = (
      _payload: Uint8Array,
      _participant: unknown,
      _kind: unknown,
      topic?: string,
    ) => {
      if (topic !== NOTES_TOPIC) return;
      api
        .getNotes(roomName)
        .then((r) => {
          skipNextLocalSave.current = true;
          setText(r.notes ?? "");
        })
        .catch(() => undefined);
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, roomName]);

  useEffect(() => {
    if (!loaded) return;
    if (skipNextLocalSave.current) {
      skipNextLocalSave.current = false;
      return;
    }
    const id = window.setTimeout(async () => {
      try {
        await api.putNotes(roomName, text);
        await room.localParticipant.publishData(TEXT_ENC.encode("ping"), {
          reliable: true,
          topic: NOTES_TOPIC,
        });
      } catch {
        /* offline / room closed — keep local text */
      }
    }, 600);
    return () => window.clearTimeout(id);
  }, [text, loaded, roomName, room]);

  function exportPdf() {
    const doc = new jsPDF({ unit: "pt", format: "a4" });
    doc.setFont("helvetica", "normal");
    doc.setFontSize(11);
    const margin = 48;
    const pageWidth = doc.internal.pageSize.getWidth();
    const pageHeight = doc.internal.pageSize.getHeight();
    const lines = doc.splitTextToSize(text || t("notes.placeholder", { defaultValue: "(empty)" }), pageWidth - 2 * margin);
    let y = margin;
    doc.setFontSize(14);
    doc.text(t("notes.tabNotes", { defaultValue: "Notes" }), margin, y);
    y += 24;
    doc.setFontSize(11);
    for (const line of lines as string[]) {
      if (y > pageHeight - margin) {
        doc.addPage();
        y = margin;
      }
      doc.text(line, margin, y);
      y += 14;
    }
    doc.save(`notes-${roomName}.pdf`);
  }

  return (
    <div className="h-full p-3 flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-400">
          {t("notes.hint", { defaultValue: "Everyone in the room can edit. Saved automatically." })}
        </p>
        <button
          type="button"
          onClick={exportPdf}
          data-testid="notes-export-pdf"
          title={t("notes.exportPdf", { defaultValue: "Export notes as PDF" })}
          aria-label={t("notes.exportPdf", { defaultValue: "Export notes as PDF" })}
          className="inline-flex items-center gap-1 px-2 py-1 rounded bg-primary-800 hover:bg-primary-700 text-xs text-slate-200"
        >
          <Download size={12} /> PDF
        </button>
      </div>
      <textarea
        data-testid="notes-textarea"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={t("notes.placeholder", { defaultValue: "Type shared notes here…" })}
        className="flex-1 w-full px-3 py-2 rounded-lg bg-primary-800 text-slate-100 border border-primary-700 text-sm resize-none font-mono"
      />
    </div>
  );
}


// ─── Whiteboard ───────────────────────────────────────────────────────


type Tool = "pen" | "rect" | "ellipse" | "text" | "select";

interface Stroke {
  points: Array<{ x: number; y: number }>;
  color: string;
  width: number;
}

interface Preview {
  kind: "rect" | "ellipse";
  start: { x: number; y: number };
  end: { x: number; y: number };
}

/** Resize handle size in CSS pixels. */
const HANDLE_PX = 12;

// The whiteboard has a FIXED CSS size so resizing the panel doesn't stretch
// the drawings. When the panel is narrower than the canvas, the surrounding
// container scrolls; wider, the canvas just shows with empty space to the
// right / below.
const BOARD_CSS_W = 1280;
const BOARD_CSS_H = 720;

function newId(): string {
  // Good-enough ULID-ish id. The backend regex accepts letters / digits /
  // hyphens / dots / colons; UUID v4 fits comfortably.
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `s-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}


function Whiteboard({ room, roomName }: { room: ReturnType<typeof useRoomContext>; roomName: string }) {
  const { t } = useTranslation();
  const stageRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  const [tool, setTool] = useState<Tool>("pen");
  const [color, setColor] = useState("#fbbf24");
  const [strokeWidth, setStrokeWidth] = useState(3);
  const [fontSize, setFontSize] = useState(22);

  // Authoritative in-memory state. Refs so the pointer handlers (defined
  // once per render) can read latest values without going through React.
  const strokesRef = useRef<Stroke[]>([]);
  const shapesRef = useRef<Map<string, WhiteboardShapeDTO>>(new Map());
  // Insertion order for z-ordering; Maps preserve insertion order natively.
  const sizeRef = useRef<{ wPx: number; hPx: number; cssW: number; cssH: number }>({ wPx: 1, hPx: 1, cssW: 1, cssH: 1 });

  // Transient state for in-progress operations.
  const drawingRef = useRef<{ active: boolean; points: Array<{ x: number; y: number }> }>({ active: false, points: [] });
  const previewRef = useRef<Preview | null>(null);
  const dragRef = useRef<
    | null
    | {
        mode: "move" | "resize";
        shapeId: string;
        startPtr: { x: number; y: number };
        original: WhiteboardShapeDTO;
      }
  >(null);

  const [selectedShapeId, setSelectedShapeId] = useState<string | null>(null);
  // editing → which text shape currently has focus in the overlay textarea.
  const [editingTextId, setEditingTextId] = useState<string | null>(null);
  // Trigger UI redraws for selection state changes.
  const [, setTick] = useState(0);
  const forceRedraw = useCallback(() => setTick((n) => (n + 1) | 0), []);

  // ────── Drawing ────────────────────────────────────────────────────

  function getCtx(): CanvasRenderingContext2D | null {
    return canvasRef.current?.getContext("2d") ?? null;
  }

  function drawStroke(ctx: CanvasRenderingContext2D, s: Stroke) {
    if (s.points.length === 0) return;
    const { wPx, hPx } = sizeRef.current;
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(s.points[0].x * wPx, s.points[0].y * hPx);
    for (let i = 1; i < s.points.length; i++) {
      ctx.lineTo(s.points[i].x * wPx, s.points[i].y * hPx);
    }
    ctx.stroke();
  }

  function drawShape(ctx: CanvasRenderingContext2D, sh: WhiteboardShapeDTO) {
    const { wPx, hPx } = sizeRef.current;
    const px = sh.x * wPx;
    const py = sh.y * hPx;
    const pw = sh.w * wPx;
    const ph = sh.h * hPx;
    ctx.strokeStyle = sh.color;
    ctx.lineWidth = sh.stroke_width;
    if (sh.kind === "rect") {
      ctx.strokeRect(px, py, pw, ph);
    } else if (sh.kind === "ellipse") {
      ctx.beginPath();
      ctx.ellipse(px + pw / 2, py + ph / 2, Math.abs(pw / 2), Math.abs(ph / 2), 0, 0, Math.PI * 2);
      ctx.stroke();
    } else if (sh.kind === "text") {
      const fs = (sh.font_size ?? 20) * (hPx / 720);
      ctx.fillStyle = sh.color;
      ctx.font = `${Math.max(8, fs)}px system-ui, sans-serif`;
      ctx.textBaseline = "top";
      const lines = (sh.text ?? "").split("\n");
      let y = py;
      for (const line of lines) {
        ctx.fillText(line, px, y);
        y += fs * 1.2;
      }
    }
  }

  function drawSelection(ctx: CanvasRenderingContext2D) {
    if (!selectedShapeId) return;
    const sh = shapesRef.current.get(selectedShapeId);
    if (!sh) return;
    const { wPx, hPx } = sizeRef.current;
    const px = sh.x * wPx;
    const py = sh.y * hPx;
    const pw = sh.w * wPx;
    const ph = sh.h * hPx;
    ctx.strokeStyle = "#60a5fa";
    ctx.lineWidth = 1;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(px - 2, py - 2, pw + 4, ph + 4);
    ctx.setLineDash([]);
    // Bottom-right resize handle.
    const handleSize = HANDLE_PX * (hPx / sizeRef.current.cssH);
    ctx.fillStyle = "#60a5fa";
    ctx.fillRect(px + pw - handleSize / 2, py + ph - handleSize / 2, handleSize, handleSize);
  }

  function drawPreview(ctx: CanvasRenderingContext2D) {
    const p = previewRef.current;
    if (!p) return;
    const { wPx, hPx } = sizeRef.current;
    const x = Math.min(p.start.x, p.end.x) * wPx;
    const y = Math.min(p.start.y, p.end.y) * hPx;
    const w = Math.abs(p.end.x - p.start.x) * wPx;
    const h = Math.abs(p.end.y - p.start.y) * hPx;
    ctx.strokeStyle = color;
    ctx.lineWidth = strokeWidth;
    ctx.setLineDash([4, 3]);
    if (p.kind === "rect") {
      ctx.strokeRect(x, y, w, h);
    } else {
      ctx.beginPath();
      ctx.ellipse(x + w / 2, y + h / 2, Math.abs(w / 2), Math.abs(h / 2), 0, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  function redrawAll() {
    const ctx = getCtx();
    if (!ctx) return;
    const { wPx, hPx } = sizeRef.current;
    ctx.clearRect(0, 0, wPx, hPx);
    for (const s of strokesRef.current) drawStroke(ctx, s);
    // ALWAYS draw every shape on the canvas, including the one currently
    // being edited. The text overlay textarea (TextOverlay) has an opaque
    // background so it cleanly covers the canvas text underneath while
    // editing; when the textarea unmounts on blur, the canvas already has
    // the latest committed text drawn — no race to handle.
    for (const sh of shapesRef.current.values()) {
      drawShape(ctx, sh);
    }
    drawPreview(ctx);
    drawSelection(ctx);
  }

  // Re-render the canvas whenever the selection / edit state changes,
  // because both `drawSelection` and the skip-while-editing check above
  // read those values via closure and otherwise wouldn't refresh.
  useEffect(() => {
    redrawAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedShapeId, editingTextId]);

  // When the user picks a text shape (via select tool or by creating one),
  // sync the font-size input in the toolbar to that shape so they can see
  // and tweak it without surprises.
  useEffect(() => {
    const id = editingTextId ?? selectedShapeId;
    if (!id) return;
    const sh = shapesRef.current.get(id);
    if (sh && sh.kind === "text" && sh.font_size != null) {
      setFontSize(sh.font_size);
    }
  }, [selectedShapeId, editingTextId]);

  /** Update the font-size input. If a text shape is currently selected or
   *  being edited, push the new size into that shape too. */
  function changeFontSize(raw: number) {
    const clamped = Math.max(10, Math.min(96, Number.isFinite(raw) ? raw : 22));
    setFontSize(clamped);
    const id = editingTextId ?? selectedShapeId;
    if (!id) return;
    const sh = shapesRef.current.get(id);
    if (!sh || sh.kind !== "text") return;
    sh.font_size = clamped;
    shapesRef.current.set(id, sh);
    redrawAll();
    forceRedraw();
    void broadcastPacket({ v: 1, type: "shape", shape: { ...sh } });
  }

  // ────── Mount: fetch persisted strokes + shapes ────────────────────

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.getWhiteboardStrokes(roomName), api.listWhiteboardShapes(roomName)])
      .then(([packets, shapes]) => {
        if (cancelled) return;
        const sts: Stroke[] = [];
        for (const p of packets) {
          const o = p as unknown as BoardPacket;
          if (o?.v !== 1) continue;
          if (o.type === "clear") {
            sts.length = 0;
          } else if (o.type === "stroke" && o.points) {
            sts.push({ points: o.points, color: o.color || "#fff", width: o.width || 3 });
          }
        }
        strokesRef.current = sts;
        const m = new Map<string, WhiteboardShapeDTO>();
        for (const sh of shapes) m.set(sh.id, sh);
        shapesRef.current = m;
        redrawAll();
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [roomName]);

  // ────── Remote updates over the data channel ───────────────────────

  useEffect(() => {
    const onData = (
      payload: Uint8Array,
      _participant: unknown,
      _kind: unknown,
      topic?: string,
    ) => {
      if (topic !== BOARD_TOPIC) return;
      try {
        const obj = JSON.parse(TEXT_DEC.decode(payload)) as BoardPacket;
        if (obj?.v !== 1) return;
        if (obj.type === "stroke" && obj.points) {
          strokesRef.current.push({ points: obj.points, color: obj.color || "#fff", width: obj.width || 3 });
          redrawAll();
        } else if (obj.type === "clear") {
          strokesRef.current = [];
          shapesRef.current.clear();
          setSelectedShapeId(null);
          setEditingTextId(null);
          redrawAll();
        } else if (obj.type === "shape") {
          shapesRef.current.set(obj.shape.id, obj.shape);
          redrawAll();
        } else if (obj.type === "shape-delete") {
          shapesRef.current.delete(obj.id);
          if (selectedShapeId === obj.id) setSelectedShapeId(null);
          if (editingTextId === obj.id) setEditingTextId(null);
          redrawAll();
        }
      } catch {
        /* ignore malformed */
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, selectedShapeId, editingTextId]);

  // ────── Resize the canvas to fit its CSS box at HiDPI ──────────────

  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const ro = new ResizeObserver(() => {
      const rect = c.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      c.width = Math.max(1, Math.floor(rect.width * dpr));
      c.height = Math.max(1, Math.floor(rect.height * dpr));
      sizeRef.current = { wPx: c.width, hPx: c.height, cssW: rect.width, cssH: rect.height };
      const ctx = c.getContext("2d");
      ctx?.setTransform(1, 0, 0, 1, 0, 0);
      redrawAll();
    });
    ro.observe(c);
    return () => ro.disconnect();
  }, []);

  // ────── Broadcast helper ───────────────────────────────────────────

  async function broadcastPacket(packet: BoardPacket) {
    // Persist first so late joiners replay; data channel is best-effort.
    try {
      if (packet.type === "stroke") {
        await api.postWhiteboardStroke(roomName, packet as unknown as Record<string, unknown>);
      } else if (packet.type === "clear") {
        await api.clearWhiteboardStrokes(roomName);
      } else if (packet.type === "shape") {
        await api.upsertWhiteboardShape(roomName, packet.shape);
      } else if (packet.type === "shape-delete") {
        await api.deleteWhiteboardShape(roomName, packet.id);
      }
    } catch {
      /* persistence failure leaves the live broadcast intact */
    }
    try {
      await room.localParticipant.publishData(
        TEXT_ENC.encode(JSON.stringify(packet)),
        { reliable: true, topic: BOARD_TOPIC },
      );
    } catch {
      /* ignore */
    }
  }

  // ────── Hit testing ────────────────────────────────────────────────

  function pointToNormalised(e: React.PointerEvent<HTMLCanvasElement>): { x: number; y: number } {
    const rect = canvasRef.current!.getBoundingClientRect();
    return {
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height,
    };
  }

  function hitTestShape(nx: number, ny: number): WhiteboardShapeDTO | null {
    // Iterate in reverse insertion order so the topmost shape wins.
    const arr = [...shapesRef.current.values()];
    for (let i = arr.length - 1; i >= 0; i--) {
      const sh = arr[i];
      if (nx >= sh.x && nx <= sh.x + sh.w && ny >= sh.y && ny <= sh.y + sh.h) {
        return sh;
      }
    }
    return null;
  }

  function hitTestHandle(sh: WhiteboardShapeDTO, nx: number, ny: number): boolean {
    const { cssW, cssH } = sizeRef.current;
    const handleX = sh.x + sh.w;
    const handleY = sh.y + sh.h;
    const halfNX = HANDLE_PX / cssW;
    const halfNY = HANDLE_PX / cssH;
    return nx >= handleX - halfNX && nx <= handleX + halfNX && ny >= handleY - halfNY && ny <= handleY + halfNY;
  }

  // ────── Pointer dispatch ───────────────────────────────────────────

  async function onDown(e: React.PointerEvent<HTMLCanvasElement>) {
    const target = e.target as HTMLCanvasElement;
    target.setPointerCapture(e.pointerId);
    const p = pointToNormalised(e);

    // If a text shape is currently being edited, this canvas click is the
    // event that's blurring the textarea (and so committing the text).
    // Don't ALSO interpret it as a new tool action — otherwise a click
    // outside the box creates a new empty text shape (in text mode) or
    // selects an unrelated shape, both of which make the user think their
    // text vanished. Closure captures the pre-blur value of editingTextId,
    // so this branch fires for exactly the "click while editing" case.
    if (editingTextId) {
      return;
    }

    if (tool === "pen") {
      drawingRef.current = { active: true, points: [p] };
      return;
    }

    if (tool === "rect" || tool === "ellipse") {
      previewRef.current = { kind: tool, start: p, end: p };
      redrawAll();
      return;
    }

    if (tool === "text") {
      // Default text-box size: 200×60 CSS px → normalise.
      const { cssW, cssH } = sizeRef.current;
      const w = Math.min(0.6, 200 / cssW);
      const h = Math.min(0.4, 60 / cssH);
      const shape: WhiteboardShapeDTO = {
        id: newId(),
        kind: "text",
        x: Math.max(0, Math.min(1 - w, p.x)),
        y: Math.max(0, Math.min(1 - h, p.y)),
        w,
        h,
        color,
        stroke_width: strokeWidth,
        text: "",
        font_size: fontSize,
      };
      shapesRef.current.set(shape.id, shape);
      setSelectedShapeId(shape.id);
      setEditingTextId(shape.id);
      await broadcastPacket({ v: 1, type: "shape", shape });
      redrawAll();
      return;
    }

    if (tool === "select") {
      // Resize handle on the currently-selected shape takes priority.
      if (selectedShapeId) {
        const sel = shapesRef.current.get(selectedShapeId);
        if (sel && hitTestHandle(sel, p.x, p.y)) {
          dragRef.current = {
            mode: "resize",
            shapeId: sel.id,
            startPtr: p,
            original: { ...sel },
          };
          return;
        }
      }
      const hit = hitTestShape(p.x, p.y);
      if (hit) {
        setSelectedShapeId(hit.id);
        dragRef.current = {
          mode: "move",
          shapeId: hit.id,
          startPtr: p,
          original: { ...hit },
        };
        redrawAll();
      } else {
        setSelectedShapeId(null);
        setEditingTextId(null);
        redrawAll();
      }
      return;
    }
  }

  function onMove(e: React.PointerEvent<HTMLCanvasElement>) {
    const p = pointToNormalised(e);

    if (tool === "pen") {
      if (!drawingRef.current.active) return;
      const ctx = getCtx();
      if (!ctx) return;
      const pts = drawingRef.current.points;
      pts.push(p);
      // Incremental draw of just the new segment.
      drawStroke(ctx, { points: pts.slice(-2), color, width: strokeWidth });
      return;
    }

    if (tool === "rect" || tool === "ellipse") {
      if (!previewRef.current) return;
      previewRef.current = { ...previewRef.current, end: p };
      redrawAll();
      return;
    }

    if (tool === "select" && dragRef.current) {
      const drag = dragRef.current;
      const sh = shapesRef.current.get(drag.shapeId);
      if (!sh) return;
      if (drag.mode === "move") {
        const dx = p.x - drag.startPtr.x;
        const dy = p.y - drag.startPtr.y;
        sh.x = Math.max(0, Math.min(1 - sh.w, drag.original.x + dx));
        sh.y = Math.max(0, Math.min(1 - sh.h, drag.original.y + dy));
      } else {
        const nw = Math.max(0.02, p.x - drag.original.x);
        const nh = Math.max(0.02, p.y - drag.original.y);
        sh.w = Math.min(1 - drag.original.x, nw);
        sh.h = Math.min(1 - drag.original.y, nh);
      }
      shapesRef.current.set(sh.id, sh);
      redrawAll();
      return;
    }
  }

  async function onUp(e: React.PointerEvent<HTMLCanvasElement>) {
    (e.target as HTMLCanvasElement).releasePointerCapture?.(e.pointerId);

    if (tool === "pen") {
      if (!drawingRef.current.active) return;
      drawingRef.current.active = false;
      const pts = drawingRef.current.points;
      if (pts.length > 1) {
        strokesRef.current.push({ points: pts, color, width: strokeWidth });
        await broadcastPacket({ v: 1, type: "stroke", points: pts, color, width: strokeWidth });
      }
      drawingRef.current.points = [];
      return;
    }

    if (tool === "rect" || tool === "ellipse") {
      const p = previewRef.current;
      previewRef.current = null;
      if (!p) return;
      const x = Math.min(p.start.x, p.end.x);
      const y = Math.min(p.start.y, p.end.y);
      const w = Math.abs(p.end.x - p.start.x);
      const h = Math.abs(p.end.y - p.start.y);
      // Ignore tiny accidental drags (< ~2 % of canvas).
      if (w < 0.02 || h < 0.02) {
        redrawAll();
        return;
      }
      const shape: WhiteboardShapeDTO = {
        id: newId(),
        kind: tool,
        x, y, w, h,
        color,
        stroke_width: strokeWidth,
        text: null,
        font_size: null,
      };
      shapesRef.current.set(shape.id, shape);
      setSelectedShapeId(shape.id);
      await broadcastPacket({ v: 1, type: "shape", shape });
      redrawAll();
      return;
    }

    if (tool === "select" && dragRef.current) {
      const drag = dragRef.current;
      dragRef.current = null;
      const sh = shapesRef.current.get(drag.shapeId);
      if (sh) {
        await broadcastPacket({ v: 1, type: "shape", shape: { ...sh } });
      }
      return;
    }
  }

  function onDoubleClick(e: React.MouseEvent<HTMLCanvasElement>) {
    if (tool !== "select") return;
    const rect = canvasRef.current!.getBoundingClientRect();
    const nx = (e.clientX - rect.left) / rect.width;
    const ny = (e.clientY - rect.top) / rect.height;
    const hit = hitTestShape(nx, ny);
    if (hit && hit.kind === "text") {
      setSelectedShapeId(hit.id);
      setEditingTextId(hit.id);
    }
  }

  async function clear() {
    strokesRef.current = [];
    shapesRef.current.clear();
    setSelectedShapeId(null);
    setEditingTextId(null);
    redrawAll();
    await broadcastPacket({ v: 1, type: "clear" });
  }

  async function deleteSelected() {
    if (!selectedShapeId) return;
    const id = selectedShapeId;
    shapesRef.current.delete(id);
    setSelectedShapeId(null);
    if (editingTextId === id) setEditingTextId(null);
    redrawAll();
    await broadcastPacket({ v: 1, type: "shape-delete", id });
  }

  function exportPdf() {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dataUrl = canvas.toDataURL("image/png");
    const doc = new jsPDF({ orientation: "landscape", unit: "pt", format: "a4" });
    const pageW = doc.internal.pageSize.getWidth();
    const pageH = doc.internal.pageSize.getHeight();
    // Fit-image-to-page preserving aspect ratio.
    const imgAspect = canvas.width / canvas.height;
    let renderW = pageW - 32;
    let renderH = renderW / imgAspect;
    if (renderH > pageH - 32) {
      renderH = pageH - 32;
      renderW = renderH * imgAspect;
    }
    const x = (pageW - renderW) / 2;
    const y = (pageH - renderH) / 2;
    doc.addImage(dataUrl, "PNG", x, y, renderW, renderH);
    doc.save(`whiteboard-${roomName}.pdf`);
  }

  // Commit a text shape's text content from the overlay textarea.
  async function commitTextEdit(id: string, value: string) {
    const sh = shapesRef.current.get(id);
    if (!sh) return;
    sh.text = value;
    shapesRef.current.set(id, sh);
    // Paint the text onto the canvas immediately. The textarea will
    // unmount on blur and the user sees the canvas copy without any gap.
    redrawAll();
    await broadcastPacket({ v: 1, type: "shape", shape: { ...sh } });
  }

  const palette = ["#fbbf24", "#ef4444", "#22c55e", "#38bdf8", "#a855f7", "#ffffff"];

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-1.5 px-2 py-2 border-b border-primary-700 flex-wrap">
        <ToolBtn icon={<PenTool size={14} />} label={t("notes.toolPen", { defaultValue: "Pen" })}
          active={tool === "pen"} onClick={() => setTool("pen")} />
        <ToolBtn icon={<SquareIcon size={14} />} label={t("notes.toolRect", { defaultValue: "Rectangle" })}
          active={tool === "rect"} onClick={() => setTool("rect")} />
        <ToolBtn icon={<CircleIcon size={14} />} label={t("notes.toolEllipse", { defaultValue: "Ellipse" })}
          active={tool === "ellipse"} onClick={() => setTool("ellipse")} />
        <ToolBtn icon={<Type size={14} />} label={t("notes.toolText", { defaultValue: "Text" })}
          active={tool === "text"} onClick={() => setTool("text")} />
        <ToolBtn icon={<MousePointer2 size={14} />} label={t("notes.toolSelect", { defaultValue: "Select" })}
          active={tool === "select"} onClick={() => setTool("select")} />
        <span className="w-px h-5 bg-primary-700 mx-1" aria-hidden />
        {palette.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => setColor(c)}
            aria-label={t("notes.color", { defaultValue: "Color" })}
            aria-pressed={color === c ? "true" : "false"}
            className={[
              "w-6 h-6 rounded-full border-2 flex-shrink-0",
              color === c ? "border-slate-100 scale-110" : "border-transparent",
            ].join(" ")}
            style={{ backgroundColor: c }}
          />
        ))}
        <input
          type="range"
          min={1}
          max={10}
          value={strokeWidth}
          onChange={(e) => setStrokeWidth(parseInt(e.target.value, 10))}
          className="ml-1 flex-1 min-w-[60px]"
          aria-label={t("notes.thickness", { defaultValue: "Stroke thickness" })}
        />
        {/* Font size for the text tool. When a text shape is selected it
            also live-updates that shape's font size. */}
        <label
          className="inline-flex items-center gap-1 text-[10px] text-slate-400"
          title={t("notes.fontSize", { defaultValue: "Text size" })}
        >
          <Type size={11} />
          <input
            type="number"
            min={10}
            max={96}
            value={fontSize}
            onChange={(e) => changeFontSize(parseInt(e.target.value || "22", 10))}
            data-testid="whiteboard-font-size"
            aria-label={t("notes.fontSize", { defaultValue: "Text size" })}
            className="w-12 px-1 py-0.5 rounded bg-primary-800 text-slate-100 border border-primary-700 text-xs"
          />
        </label>
        {selectedShapeId && (
          <button
            type="button"
            onClick={() => void deleteSelected()}
            title={t("notes.deleteShape", { defaultValue: "Delete selected" })}
            aria-label={t("notes.deleteShape", { defaultValue: "Delete selected" })}
            className="p-1.5 rounded hover:bg-red-700/30 text-red-300"
          >
            <X size={14} />
          </button>
        )}
        <button
          type="button"
          onClick={exportPdf}
          title={t("notes.exportPdf", { defaultValue: "Export as PDF" })}
          aria-label={t("notes.exportPdf", { defaultValue: "Export as PDF" })}
          className="p-1.5 rounded hover:bg-primary-800 text-slate-300"
        >
          <Download size={14} />
        </button>
        <button
          type="button"
          onClick={() => void clear()}
          title={t("notes.clear", { defaultValue: "Clear board" })}
          aria-label={t("notes.clear", { defaultValue: "Clear board" })}
          className="p-1.5 rounded hover:bg-primary-800 text-slate-300"
        >
          <Eraser size={14} />
        </button>
      </div>
      {/* Scrollable viewport. The canvas inside has a FIXED CSS size so a
          wider/narrower panel doesn't stretch the drawings; the viewport
          scrolls when the panel is narrower than the board, and shows
          empty space to the right when it's wider. */}
      <div className="flex-1 overflow-auto bg-primary-900">
        <div
          ref={stageRef}
          className="relative"
          style={{ width: BOARD_CSS_W, height: BOARD_CSS_H }}
        >
          <canvas
            ref={canvasRef}
            data-testid="whiteboard-canvas"
            onPointerDown={(e) => void onDown(e)}
            onPointerMove={onMove}
            onPointerUp={(e) => void onUp(e)}
            onPointerCancel={(e) => void onUp(e)}
            onDoubleClick={onDoubleClick}
            className={[
              "block touch-none bg-primary-950",
              tool === "select" ? "cursor-default" : "cursor-crosshair",
            ].join(" ")}
            style={{ width: BOARD_CSS_W, height: BOARD_CSS_H }}
          />
          {editingTextId && (
            <TextOverlay
              shape={shapesRef.current.get(editingTextId)!}
              cssSize={sizeRef.current}
              onCommit={(v) => {
                void commitTextEdit(editingTextId, v);
                setEditingTextId(null);
                forceRedraw();
              }}
              onCancel={() => setEditingTextId(null)}
            />
          )}
        </div>
      </div>
    </div>
  );
}


function ToolBtn({ icon, label, active, onClick }: { icon: React.ReactNode; label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active ? "true" : "false"}
      title={label}
      aria-label={label}
      className={[
        "p-1.5 rounded text-slate-300 inline-flex items-center justify-center",
        active ? "bg-accent-500 text-white" : "hover:bg-primary-800",
      ].join(" ")}
    >
      {icon}
    </button>
  );
}


function TextOverlay({
  shape,
  cssSize,
  onCommit,
  onCancel,
}: {
  shape: WhiteboardShapeDTO;
  cssSize: { cssW: number; cssH: number };
  onCommit: (v: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(shape.text ?? "");
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  useLayoutEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);
  const left = shape.x * cssSize.cssW;
  const top = shape.y * cssSize.cssH;
  const width = shape.w * cssSize.cssW;
  const height = shape.h * cssSize.cssH;
  return (
    <textarea
      ref={inputRef}
      data-testid="whiteboard-text-overlay"
      aria-label="Whiteboard text"
      placeholder="Type text…"
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onBlur={(e) => onCommit(e.currentTarget.value)}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          onCancel();
        } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          onCommit(value);
        }
      }}
      style={{
        position: "absolute",
        left,
        top,
        width,
        height,
        color: shape.color,
        fontSize: (shape.font_size ?? 20) * (cssSize.cssH / 720),
        lineHeight: 1.2,
        fontFamily: "system-ui, sans-serif",
      }}
      // Opaque background so the textarea fully covers any canvas content
      // underneath while editing. When the user blurs the textarea, it
      // unmounts and the canvas already has the latest text drawn, so the
      // transition is seamless and there's no race where the text could be
      // briefly invisible.
      className="bg-primary-950 border border-accent-500 outline-none resize-none p-0 m-0"
    />
  );
}

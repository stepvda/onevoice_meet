import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";
import { useRoomContext } from "@livekit/components-react";
import { RoomEvent } from "livekit-client";
import { Brush, Eraser, FileText, X } from "lucide-react";
import { api } from "../lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  /** When non-null, the panel switches to this tab the next time it opens.
   *  Used by Room.tsx to route auto-opens to the right tab based on which
   *  data-channel topic triggered them. */
  initialTab?: Tab | null;
  /** Cleared by the parent after the requested tab is applied so the same
   *  signal doesn't keep re-applying on later renders. */
  onConsumeInitialTab?: () => void;
}

type Tab = "notes" | "board";

export const NOTES_TOPIC = "meet-notes";
export const BOARD_TOPIC = "meet-board";
const TEXT_ENC = new TextEncoder();
const TEXT_DEC = new TextDecoder();

interface BoardPacket {
  v: 1;
  type: "stroke" | "clear";
  // stroke fields
  points?: Array<{ x: number; y: number }>;
  color?: string;
  width?: number;
}

/**
 * Combined collaborative notes + whiteboard side panel.
 *
 * - **Notes:** plain-text textarea persisted to `Meeting.notes` server-side
 *   with debounced writes (last-writer-wins). Refresh is pushed via a
 *   "notes-refetch" data-channel signal.
 * - **Whiteboard:** ephemeral; stroke points and clear events are sent over
 *   the data channel and replayed on the receiver's canvas. Coordinates are
 *   normalised to [0, 1] so participants on different canvas sizes see the
 *   same drawing.
 */
export default function NotesWhiteboardPanel({ open, onClose, initialTab, onConsumeInitialTab }: Props) {
  const { t } = useTranslation();
  const { roomName = "" } = useParams();
  const room = useRoomContext();
  const [tab, setTab] = useState<Tab>("notes");

  // Apply a requested tab when the panel is open. Cleared by the parent
  // via `onConsumeInitialTab` so the same request doesn't re-fire later.
  useEffect(() => {
    if (open && initialTab) {
      setTab(initialTab);
      onConsumeInitialTab?.();
    }
  }, [open, initialTab, onConsumeInitialTab]);

  if (!open) return null;
  return (
    <aside
      data-testid="notes-board-panel"
      role="complementary"
      aria-label={t("notes.title", { defaultValue: "Notes & whiteboard" })}
      className="h-full w-full sm:w-96 flex-shrink-0 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col"
    >
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

  // Listen for refetch hints from peers.
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

  // Debounced save on local edits.
  useEffect(() => {
    if (!loaded) return;
    if (skipNextLocalSave.current) {
      skipNextLocalSave.current = false;
      return;
    }
    const id = window.setTimeout(async () => {
      try {
        await api.putNotes(roomName, text);
        // Signal peers to refetch.
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

  return (
    <div className="h-full p-3 flex flex-col gap-2">
      <p className="text-xs text-slate-400">
        {t("notes.hint", { defaultValue: "Everyone in the room can edit. Saved automatically." })}
      </p>
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

function Whiteboard({ room, roomName }: { room: ReturnType<typeof useRoomContext>; roomName: string }) {
  const { t } = useTranslation();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const drawingRef = useRef<{ active: boolean; points: Array<{ x: number; y: number }> }>({
    active: false,
    points: [],
  });
  const sizeRef = useRef<{ w: number; h: number }>({ w: 1, h: 1 });
  // Every committed stroke (in order). Kept in memory so we can redraw the
  // entire board on canvas resize and so late-tab-openers see the full
  // history. Initialised from the server when the component mounts.
  const strokesRef = useRef<Array<{ points: Array<{ x: number; y: number }>; color: string; width: number }>>([]);
  const [color, setColor] = useState("#fbbf24");
  const [stroke, setStroke] = useState(3);

  function getCtx(): CanvasRenderingContext2D | null {
    const c = canvasRef.current;
    if (!c) return null;
    return c.getContext("2d");
  }

  function drawStroke(points: Array<{ x: number; y: number }>, color: string, w: number) {
    const ctx = getCtx();
    if (!ctx || points.length === 0) return;
    const { w: cw, h: ch } = sizeRef.current;
    ctx.strokeStyle = color;
    ctx.lineWidth = w;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(points[0].x * cw, points[0].y * ch);
    for (let i = 1; i < points.length; i++) {
      ctx.lineTo(points[i].x * cw, points[i].y * ch);
    }
    ctx.stroke();
  }

  function clearBoard() {
    const ctx = getCtx();
    if (!ctx) return;
    const { w, h } = sizeRef.current;
    ctx.clearRect(0, 0, w, h);
  }

  /** Re-render the entire stroke history. Cheap unless the board is huge. */
  function redrawAll() {
    clearBoard();
    for (const s of strokesRef.current) {
      drawStroke(s.points, s.color, s.width);
    }
  }

  // On mount: fetch any strokes already drawn on this board by other
  // participants (or earlier in this session) and replay them.
  useEffect(() => {
    let cancelled = false;
    api
      .getWhiteboardStrokes(roomName)
      .then((packets) => {
        if (cancelled) return;
        const strokes: typeof strokesRef.current = [];
        for (const p of packets) {
          const o = p as unknown as BoardPacket;
          if (o?.v !== 1) continue;
          if (o.type === "clear") {
            strokes.length = 0;
          } else if (o.type === "stroke" && o.points) {
            strokes.push({
              points: o.points,
              color: o.color || "#fff",
              width: o.width || 3,
            });
          }
        }
        strokesRef.current = strokes;
        redrawAll();
      })
      .catch(() => {
        /* no history yet — fresh board */
      });
    return () => {
      cancelled = true;
    };
  }, [roomName]);

  // Subscribe to remote strokes. Append to the in-memory history AND draw
  // the new stroke incrementally, so a redraw-on-resize keeps it.
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
          const s = {
            points: obj.points,
            color: obj.color || "#fff",
            width: obj.width || 3,
          };
          strokesRef.current.push(s);
          drawStroke(s.points, s.color, s.width);
        } else if (obj.type === "clear") {
          strokesRef.current = [];
          clearBoard();
        }
      } catch {
        /* ignore malformed */
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room]);

  // Resize handling — keep the canvas at its CSS size in actual pixels so
  // the drawing isn't blurry on HiDPI. Replay all strokes after resize so
  // the board doesn't visually wipe on a panel/window size change.
  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const ro = new ResizeObserver(() => {
      const rect = c.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      c.width = Math.max(1, Math.floor(rect.width * dpr));
      c.height = Math.max(1, Math.floor(rect.height * dpr));
      sizeRef.current = { w: c.width, h: c.height };
      const ctx = c.getContext("2d");
      ctx?.setTransform(1, 0, 0, 1, 0, 0);
      redrawAll();
    });
    ro.observe(c);
    return () => ro.disconnect();
  }, []);

  async function broadcast(packet: BoardPacket) {
    // Persist to the server first so late joiners can replay it; the data
    // channel is best-effort for live peers.
    try {
      if (packet.type === "clear") {
        await api.clearWhiteboardStrokes(roomName);
      } else {
        await api.postWhiteboardStroke(roomName, packet as unknown as Record<string, unknown>);
      }
    } catch {
      /* persistence failed — peer broadcast will still happen so live users
         see it, but late joiners won't get this stroke. */
    }
    try {
      await room.localParticipant.publishData(TEXT_ENC.encode(JSON.stringify(packet)), {
        reliable: true,
        topic: BOARD_TOPIC,
      });
    } catch {
      /* ignore */
    }
  }

  function toNormalised(e: React.PointerEvent<HTMLCanvasElement>) {
    const c = canvasRef.current!;
    const rect = c.getBoundingClientRect();
    return {
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height,
    };
  }

  function onDown(e: React.PointerEvent<HTMLCanvasElement>) {
    drawingRef.current = { active: true, points: [toNormalised(e)] };
    (e.target as HTMLCanvasElement).setPointerCapture(e.pointerId);
  }
  function onMove(e: React.PointerEvent<HTMLCanvasElement>) {
    if (!drawingRef.current.active) return;
    const p = toNormalised(e);
    const pts = drawingRef.current.points;
    pts.push(p);
    drawStroke(pts.slice(-2), color, stroke);
  }
  async function onUp(_e: React.PointerEvent<HTMLCanvasElement>) {
    if (!drawingRef.current.active) return;
    drawingRef.current.active = false;
    if (drawingRef.current.points.length > 1) {
      const pts = drawingRef.current.points;
      // LiveKit doesn't echo a participant's own `publishData` back to
      // them, so we append to the local history ourselves — otherwise the
      // stroke would vanish on the next canvas resize / redraw.
      strokesRef.current.push({ points: pts, color, width: stroke });
      await broadcast({
        v: 1,
        type: "stroke",
        points: pts,
        color,
        width: stroke,
      });
    }
    drawingRef.current.points = [];
  }

  async function clear() {
    strokesRef.current = [];
    clearBoard();
    await broadcast({ v: 1, type: "clear" });
  }

  const palette = ["#fbbf24", "#ef4444", "#22c55e", "#38bdf8", "#a855f7", "#ffffff"];

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-primary-700">
        {palette.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => setColor(c)}
            aria-label={t("notes.color", { defaultValue: "Color" })}
            aria-pressed={color === c ? "true" : "false"}
            className={[
              "w-6 h-6 rounded-full border-2",
              color === c ? "border-slate-100 scale-110" : "border-transparent",
            ].join(" ")}
            style={{ backgroundColor: c }}
          />
        ))}
        <input
          type="range"
          min={1}
          max={10}
          value={stroke}
          onChange={(e) => setStroke(parseInt(e.target.value, 10))}
          className="ml-2 flex-1"
          aria-label={t("notes.thickness", { defaultValue: "Stroke thickness" })}
        />
        <button
          type="button"
          onClick={() => void clear()}
          title={t("notes.clear", { defaultValue: "Clear board" })}
          aria-label={t("notes.clear", { defaultValue: "Clear board" })}
          className="p-1.5 rounded hover:bg-primary-800 text-slate-300"
        >
          <Eraser size={16} />
        </button>
      </div>
      <canvas
        ref={canvasRef}
        data-testid="whiteboard-canvas"
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        className="flex-1 w-full touch-none bg-primary-950 cursor-crosshair"
      />
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ArrowDown,
  ArrowUp,
  CircleStopIcon,
  Download,
  Film,
  Link2,
  ListVideo,
  Pause,
  Play,
  Repeat,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { api, MeetingOut, PlaybackItemOut, PlaybackStateOut } from "../lib/api";
import { Button, Toggle } from "./ui";

interface Props {
  meeting: MeetingOut;
  open: boolean;
  onClose: () => void;
  onMeetingUpdated: (m: MeetingOut) => void;
}

const ACCEPT = "video/mp4,video/quicktime,video/x-m4v";
const MAX_BYTES = 500 * 1024 * 1024;

function fmtSize(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 * 1024 * 1024) return `${(b / (1024 * 1024)).toFixed(1)} MB`;
  return `${(b / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function fmtTime(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—:—";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * Probe a chosen video file via a hidden HTMLVideoElement to read its
 * duration. Resolves the duration in seconds, or `null` if the browser
 * can't decode the metadata (e.g. exotic codec). Uses a 10-second
 * timeout so a hung load doesn't block the upload.
 */
async function probeDuration(file: File): Promise<number | null> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    v.preload = "metadata";
    let settled = false;
    const finish = (d: number | null) => {
      if (settled) return;
      settled = true;
      try { URL.revokeObjectURL(url); } catch { /* ignore */ }
      resolve(d);
    };
    v.onloadedmetadata = () => {
      const d = v.duration;
      finish(Number.isFinite(d) && d > 0 ? d : null);
    };
    v.onerror = () => finish(null);
    v.src = url;
    window.setTimeout(() => finish(null), 10_000);
  });
}

/**
 * Playlist side drawer — same right-edge panel pattern as ChatPanel /
 * ParticipantsPanel. Replaces the older modal. Holds:
 *
 *   - Enable + Loop toggles (gates the toolbar Playlist button entirely
 *     and the playlist-wrap behaviour respectively).
 *   - Now Playing controls: a Play / Pause / Stop trio plus a
 *     read-only progress bar (computed from `playback_started_at`).
 *   - The playlist itself: each row is clickable to jump to that
 *     item; the currently-playing row is highlighted. The existing
 *     per-row download / link / reorder / delete buttons stay where
 *     they were.
 *
 * Note: Pause currently stops the ingress with no resume-from-position
 * (true seek requires ffmpeg in meeting-api). Clicking Play again
 * restarts the same item from the beginning. The slider is read-only
 * for the same reason. Both are spelled out in the relevant comments
 * below so future work can swap in real seek without rearchitecting.
 */
export default function VideoPlaybackPanel({ meeting, open, onClose, onMeetingUpdated }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<PlaybackItemOut[] | null>(null);
  const [pbState, setPbState] = useState<PlaybackStateOut | null>(null);
  const [enabled, setEnabled] = useState(!!meeting.playback_enabled);
  const [loop, setLoop] = useState(!!meeting.playback_loop);
  const [whatsUpNext, setWhatsUpNext] = useState(!!meeting.playback_whats_up_next);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [list, state] = await Promise.all([
        api.listPlaybackItems(meeting.id),
        api.playbackState(meeting.id).catch(() => null),
      ]);
      setItems(list);
      if (state) setPbState(state);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [meeting.id]);

  // First load + re-seed when the panel opens (or the meeting changes
  // — e.g. cohost promotion). The poller below keeps it fresh.
  useEffect(() => {
    if (!open) return;
    setEnabled(!!meeting.playback_enabled);
    setLoop(!!meeting.playback_loop);
    setWhatsUpNext(!!meeting.playback_whats_up_next);
    setErr(null);
    void refresh();
  }, [open, meeting, refresh]);

  // Poll while open so the progress bar advances and the
  // currently-playing highlight stays accurate when items advance
  // via the auto-advance webhook.
  useEffect(() => {
    if (!open) return;
    const id = window.setInterval(() => {
      void api
        .playbackState(meeting.id)
        .then(setPbState)
        .catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(id);
  }, [open, meeting.id]);

  // Ticking elapsed time — derived from the server's `started_at`
  // timestamp so multiple participants stay in sync. We tick locally
  // every 250 ms to make the bar smooth without hammering the server.
  const [tickNow, setTickNow] = useState(() => Date.now());
  useEffect(() => {
    if (!open || !pbState?.active) return;
    const id = window.setInterval(() => setTickNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, [open, pbState?.active, pbState?.started_at]);
  const elapsedSeconds = (() => {
    if (!pbState?.started_at) return 0;
    const t0 = new Date(pbState.started_at).getTime();
    return Math.max(0, (tickNow - t0) / 1000);
  })();
  const durationSeconds = pbState?.current_item_duration_seconds ?? null;
  const progressPct = (() => {
    if (!durationSeconds) return 0;
    return Math.max(0, Math.min(100, (elapsedSeconds / durationSeconds) * 100));
  })();

  if (!open) return null;

  async function toggleEnabled(v: boolean) {
    setEnabled(v);
    setErr(null);
    try {
      const updated = await api.updateMeeting(meeting.id, { playback_enabled: v });
      onMeetingUpdated(updated);
    } catch (e) {
      setEnabled(!v);
      setErr((e as Error).message);
    }
  }

  async function toggleLoop(v: boolean) {
    setLoop(v);
    setErr(null);
    try {
      const updated = await api.updateMeeting(meeting.id, { playback_loop: v });
      onMeetingUpdated(updated);
    } catch (e) {
      setLoop(!v);
      setErr((e as Error).message);
    }
  }

  async function toggleWhatsUpNext(v: boolean) {
    setWhatsUpNext(v);
    setErr(null);
    try {
      const updated = await api.updateMeeting(meeting.id, { playback_whats_up_next: v });
      onMeetingUpdated(updated);
    } catch (e) {
      setWhatsUpNext(!v);
      setErr((e as Error).message);
    }
  }

  async function onPick(files: FileList | null) {
    if (!files || files.length === 0) return;
    setErr(null);
    for (const file of Array.from(files)) {
      if (file.size > MAX_BYTES) {
        setErr(
          t("playback.tooLarge", {
            defaultValue: "{{name}} is over the 500 MB cap.",
            name: file.name,
          }),
        );
        continue;
      }
      setBusy(`upload:${file.name}`);
      try {
        // Probe duration locally so the panel can render a progress
        // bar after upload. Best-effort: if probing fails (codec,
        // timeout) we still upload with null duration.
        const dur = await probeDuration(file);
        await api.uploadPlaybackItem(meeting.id, file, undefined, dur ?? undefined);
      } catch (e) {
        setErr((e as Error).message);
        break;
      } finally {
        setBusy(null);
      }
    }
    if (fileInput.current) fileInput.current.value = "";
    await refresh();
  }

  async function deleteItem(item: PlaybackItemOut) {
    if (!confirm(t("playback.deleteConfirm", { defaultValue: "Remove “{{name}}” from the playlist?", name: item.filename })))
      return;
    setBusy(`delete:${item.id}`);
    setErr(null);
    try {
      await api.deletePlaybackItem(meeting.id, item.id);
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function downloadItem(item: PlaybackItemOut) {
    setErr(null);
    try {
      await api.downloadPlaybackItem(meeting.id, item.id, item.filename);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function duplicateItem(item: PlaybackItemOut) {
    setBusy(`duplicate:${item.id}`);
    setErr(null);
    try {
      await api.duplicatePlaybackItem(meeting.id, item.id);
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function move(item: PlaybackItemOut, direction: -1 | 1) {
    if (!items) return;
    const idx = items.findIndex((x) => x.id === item.id);
    const newIdx = idx + direction;
    if (newIdx < 0 || newIdx >= items.length) return;
    const next = [...items];
    [next[idx], next[newIdx]] = [next[newIdx], next[idx]];
    setItems(next.map((x, i) => ({ ...x, position: i })));
    try {
      await api.reorderPlaybackItems(meeting.id, next.map((x) => x.id));
    } catch (e) {
      setErr((e as Error).message);
      await refresh();
    }
  }

  async function playItem(item: PlaybackItemOut) {
    setBusy(`play:${item.id}`);
    setErr(null);
    try {
      await api.playPlaybackItem(meeting.id, item.id);
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function startFromBeginning() {
    setBusy("play-start");
    setErr(null);
    try {
      await api.startPlayback(meeting.id);
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function stop() {
    setBusy("play-stop");
    setErr(null);
    try {
      await api.stopPlayback(meeting.id);
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function seekTo(positionSeconds: number) {
    if (!pbState?.active || !durationSeconds) return;
    const clamped = Math.max(0, Math.min(durationSeconds - 0.5, positionSeconds));
    setBusy("play-seek");
    setErr(null);
    try {
      await api.seekPlayback(meeting.id, clamped);
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  function onProgressClick(e: React.MouseEvent<HTMLDivElement>) {
    if (!pbState?.active || !durationSeconds) return;
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    const fraction = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    void seekTo(fraction * durationSeconds);
  }

  const playing = !!pbState?.active;
  const currentId = pbState?.current_item_id ?? null;
  const hasItems = !!items && items.length > 0;
  // Sum every item's duration — including aliases, since the server
  // populates an alias's `duration_seconds` from its source row. NULLs
  // (legacy items still pending lazy backfill) contribute 0; the footer
  // surfaces this with a "—" suffix when any row is unknown.
  const totalDurationSeconds = items
    ? items.reduce((acc, it) => acc + (it.duration_seconds ?? 0), 0)
    : 0;
  const hasUnknownDuration = !!items && items.some((it) => it.duration_seconds == null);

  return (
    <aside
      data-testid="playback-panel"
      className={[
        // Same overlay/inline split as ChatPanel: overlays the stage on
        // mobile, becomes a flex column on sm+.
        "absolute inset-y-0 right-0 z-20 sm:static sm:z-auto",
        "h-full w-full sm:w-96 flex-shrink-0 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col",
      ].join(" ")}
      role="complementary"
      aria-label={t("playback.title", { defaultValue: "Playlist" })}
    >
      <header className="flex items-center justify-between px-4 py-3 border-b border-primary-700">
        <h2 className="text-sm font-semibold text-slate-100 inline-flex items-center gap-2">
          <Film size={14} className="text-accent-500" />
          {t("playback.title", { defaultValue: "Playlist" })}
          {items && <span className="text-slate-500 font-normal">({items.length})</span>}
        </h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("common.close", { defaultValue: "Close" })}
          data-testid="playback-close"
          className="p-1 rounded hover:bg-primary-700 text-slate-300"
        >
          <X size={18} />
        </button>
      </header>

      <div className="px-4 py-3 border-b border-primary-700 space-y-2">
        <Toggle
          id="playback-enabled"
          label={t("playback.enable", { defaultValue: "Enable video playback" })}
          checked={enabled}
          onChange={toggleEnabled}
        />
        <Toggle
          id="playback-loop"
          label={
            <span className="inline-flex items-center gap-1.5">
              <Repeat size={14} />
              {t("playback.loop", { defaultValue: "Loop the playlist" })}
            </span>
          }
          checked={loop}
          onChange={toggleLoop}
        />
        <Toggle
          id="playback-whats-up-next"
          label={
            <span className="inline-flex items-center gap-1.5">
              <ListVideo size={14} />
              {t("playback.whatsUpNext", { defaultValue: "What's up next" })}
            </span>
          }
          checked={whatsUpNext}
          onChange={toggleWhatsUpNext}
        />
      </div>

      {/* Now-playing / playback controls. Visible even when nothing is
          actively playing — Play starts from the top, the bar is at
          zero. Stop is disabled until something is running. */}
      <div className="px-4 py-3 border-b border-primary-700 space-y-2">
        <div className="text-[11px] uppercase tracking-wide text-slate-500 font-semibold">
          {t("playback.nowPlaying", { defaultValue: "Now playing" })}
        </div>
        <div className="text-sm text-slate-200 truncate" title={pbState?.current_item_filename ?? undefined}>
          {pbState?.current_item_filename ?? <span className="text-slate-500">{t("playback.idle", { defaultValue: "Idle" })}</span>}
        </div>

        {/* Click-to-seek bar — only active while something is playing
            and the current item's duration is known. The server
            restarts the ingress at the requested offset via
            POST /playback:seek (ffmpeg stream-copies the source from
            time T as MPEG-TS into the LiveKit ingress). */}
        <div className="space-y-1">
          <div
            className={[
              "h-2 w-full rounded-full bg-primary-800 overflow-hidden",
              playing && durationSeconds ? "cursor-pointer" : "cursor-default",
            ].join(" ")}
            role="slider"
            tabIndex={playing && durationSeconds ? 0 : -1}
            aria-valuenow={Math.round(playing ? elapsedSeconds : 0)}
            aria-valuemin={0}
            aria-valuemax={Math.round(durationSeconds ?? 0)}
            aria-label={t("playback.progress", { defaultValue: "Playback progress" })}
            aria-disabled={!playing || !durationSeconds}
            onClick={onProgressClick}
            onKeyDown={(e) => {
              if (!playing || !durationSeconds) return;
              if (e.key === "ArrowLeft") {
                e.preventDefault();
                void seekTo(elapsedSeconds - 5);
              } else if (e.key === "ArrowRight") {
                e.preventDefault();
                void seekTo(elapsedSeconds + 5);
              }
            }}
            data-testid="playback-progress"
          >
            <div
              className="h-full bg-accent-500 transition-[width] duration-150 pointer-events-none"
              style={{ width: `${progressPct}%` }}
            />
          </div>
          <div className="flex justify-between text-[10px] text-slate-400 tabular-nums">
            <span>{fmtTime(playing ? elapsedSeconds : 0)}</span>
            <span>{fmtTime(durationSeconds)}</span>
          </div>
        </div>

        <div className="flex items-center gap-2 pt-1">
          {playing ? (
            // Server-side this calls stopPlayback. Labelled "Pause"
            // because the UX is "stop the stream, want to resume" —
            // restoring with seek is a follow-up.
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={stop}
              disabled={!!busy}
              data-testid="playback-pause"
            >
              <Pause size={14} /> {t("playback.pause", { defaultValue: "Pause" })}
            </Button>
          ) : (
            <Button
              type="button"
              variant="accent"
              size="sm"
              onClick={startFromBeginning}
              disabled={!!busy || !enabled || !hasItems}
              data-testid="playback-play"
              title={
                !enabled
                  ? t("playback.disabledHint", { defaultValue: "Enable video playback first" })
                  : !hasItems
                  ? t("playback.emptyHint", { defaultValue: "Add a video first" })
                  : undefined
              }
            >
              <Play size={14} /> {t("playback.play", { defaultValue: "Play" })}
            </Button>
          )}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={stop}
            disabled={!!busy || !playing}
            data-testid="playback-stop"
          >
            <CircleStopIcon size={14} /> {t("playback.stop", { defaultValue: "Stop" })}
          </Button>
        </div>
      </div>

      {/* Playlist scroller — flex-1 so it claims the remaining height
          and scrolls independently of the toggles + controls above. */}
      <div className="flex-1 min-h-0 overflow-y-auto px-2 py-2">
        <div className="flex items-center justify-between px-2 pb-2">
          <span className="text-[11px] uppercase tracking-wide text-slate-500 font-semibold">
            {t("playback.playlistTitle", { defaultValue: "Playlist" })}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => fileInput.current?.click()}
            disabled={!!busy}
            data-testid="playback-upload-btn"
          >
            <Upload size={14} /> {t("playback.add", { defaultValue: "Add MP4" })}
          </Button>
          <input
            ref={fileInput}
            type="file"
            accept={ACCEPT}
            multiple
            className="hidden"
            onChange={(e) => onPick(e.target.files)}
            data-testid="playback-file-input"
            aria-label={t("playback.add", { defaultValue: "Add MP4" })}
          />
        </div>

        {items === null ? (
          <p className="text-sm text-slate-400 px-2">{t("playback.loading", { defaultValue: "Loading…" })}</p>
        ) : items.length === 0 ? (
          <p className="text-sm text-slate-500 px-2">
            {t("playback.empty", { defaultValue: "No videos yet. Add one or more MP4 files to build a playlist." })}
          </p>
        ) : (
          <ul className="flex flex-col divide-y divide-primary-700">
            {items.map((it, i) => {
              const isAlias = it.source_item_id !== null;
              const isCurrent = currentId === it.id;
              return (
                <li
                  key={it.id}
                  data-testid={`playback-item-${it.id}`}
                  data-current={isCurrent ? "true" : "false"}
                  className={[
                    "py-2 px-2 flex items-center gap-2 first:pt-1 last:pb-1",
                    // Bright accent highlight on the currently-playing
                    // row — replaces the old "Now playing: <file>" pill
                    // in the toolbar, which the user explicitly asked
                    // to remove now that this is visible.
                    isCurrent
                      ? "bg-accent-500/20 border-l-2 border-accent-500"
                      : "hover:bg-primary-800/60 border-l-2 border-transparent",
                  ].join(" ")}
                >
                  <span className="text-xs text-slate-500 w-5 text-right flex-shrink-0">{i + 1}.</span>
                  {/* Click-to-play: pressing the title area starts (or
                      switches to) this item. We don't make the whole
                      <li> clickable so the per-row buttons keep their
                      individual click targets. */}
                  <button
                    type="button"
                    onClick={() => playItem(it)}
                    disabled={!!busy || !enabled}
                    title={
                      !enabled
                        ? t("playback.disabledHint", { defaultValue: "Enable video playback first" })
                        : isCurrent
                        ? t("playback.alreadyPlaying", { defaultValue: "Already playing" })
                        : t("playback.playThisItem", { defaultValue: "Play this item" })
                    }
                    data-testid={`playback-play-item-${it.id}`}
                    className="flex-1 min-w-0 text-left disabled:cursor-default disabled:opacity-70"
                  >
                    <div
                      className={[
                        "text-sm truncate flex items-center gap-1.5",
                        isAlias ? "text-slate-300 italic" : "text-slate-100",
                        isCurrent ? "font-semibold" : "",
                      ].join(" ")}
                    >
                      {isAlias && <Link2 size={12} className="text-accent-500 flex-shrink-0" aria-hidden />}
                      {isCurrent && (
                        <Play size={12} className="text-accent-500 flex-shrink-0 animate-pulse" aria-hidden />
                      )}
                      <span className="truncate">{it.filename}</span>
                    </div>
                    <div className="text-xs text-slate-500">
                      {isAlias
                        ? t("playback.aliasLabel", { defaultValue: "Link" })
                        : `${fmtTime(it.duration_seconds)} · ${fmtSize(it.file_size_bytes)}`}
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={() => downloadItem(it)}
                    disabled={!!busy}
                    aria-label={t("playback.download", { defaultValue: "Download original file" })}
                    title={t("playback.download", { defaultValue: "Download original file" })}
                    data-testid={`playback-download-${it.id}`}
                    className="p-1 rounded hover:bg-primary-700 disabled:opacity-30 text-slate-300"
                  >
                    <Download size={14} />
                  </button>
                  <button
                    type="button"
                    onClick={() => duplicateItem(it)}
                    disabled={!!busy}
                    aria-label={t("playback.duplicate", { defaultValue: "Add a link to this video" })}
                    title={t("playback.duplicate", { defaultValue: "Add a link to this video" })}
                    data-testid={`playback-duplicate-${it.id}`}
                    className="p-1 rounded hover:bg-primary-700 disabled:opacity-30 text-slate-300"
                  >
                    <Link2 size={14} />
                  </button>
                  <button
                    type="button"
                    onClick={() => move(it, -1)}
                    disabled={i === 0 || !!busy}
                    aria-label={t("playback.moveUp", { defaultValue: "Move up" })}
                    className="p-1 rounded hover:bg-primary-700 disabled:opacity-30 text-slate-300"
                  >
                    <ArrowUp size={14} />
                  </button>
                  <button
                    type="button"
                    onClick={() => move(it, 1)}
                    disabled={i === items.length - 1 || !!busy}
                    aria-label={t("playback.moveDown", { defaultValue: "Move down" })}
                    className="p-1 rounded hover:bg-primary-700 disabled:opacity-30 text-slate-300"
                  >
                    <ArrowDown size={14} />
                  </button>
                  <button
                    type="button"
                    onClick={() => deleteItem(it)}
                    disabled={!!busy || isCurrent}
                    aria-label={t("playback.delete", { defaultValue: "Delete" })}
                    title={
                      isCurrent
                        ? t("playback.cantDeleteCurrent", { defaultValue: "Currently playing — stop first" })
                        : undefined
                    }
                    className="p-1 rounded hover:bg-red-900/40 disabled:opacity-30 text-red-400"
                  >
                    <Trash2 size={14} />
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        {busy?.startsWith("upload:") && (
          <p className="text-xs text-slate-400 px-2 pt-2" data-testid="playback-uploading">
            {t("playback.uploading", { defaultValue: "Uploading…" })}
          </p>
        )}
      </div>

      {hasItems && (
        <div
          className="px-4 py-2 border-t border-primary-700 flex items-center justify-between text-xs text-slate-400"
          data-testid="playback-total-duration"
        >
          <span>{t("playback.totalDuration", { defaultValue: "Total duration" })}</span>
          <span className="tabular-nums text-slate-200 font-medium">
            {fmtTime(totalDurationSeconds)}
            {hasUnknownDuration && (
              <span
                className="text-slate-500 ml-1"
                title={t("playback.someDurationsPending", {
                  defaultValue: "Some videos are still being measured",
                })}
              >
                +
              </span>
            )}
          </span>
        </div>
      )}

      {err && (
        <div className="px-3 py-2 text-xs text-red-400 border-t border-primary-700">{err}</div>
      )}
    </aside>
  );
}

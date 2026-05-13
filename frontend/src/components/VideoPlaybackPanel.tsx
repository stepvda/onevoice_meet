import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ArrowDown, ArrowUp, Film, Link2, Repeat, Trash2, Upload, X } from "lucide-react";
import { api, MeetingOut, PlaybackItemOut } from "../lib/api";
import { Button, Card, Toggle } from "./ui";

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

/**
 * Per-meeting video playback configurator. Host uploads MP4s into the
 * playlist, optionally enables loop, and toggles the master "Video
 * playback" switch — that switch is what makes the Play/Stop button
 * appear in the meeting toolbar. Modal-style so we don't fight for
 * the side-drawer space with chat / participants / settings.
 */
export default function VideoPlaybackPanel({ meeting, open, onClose, onMeetingUpdated }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<PlaybackItemOut[] | null>(null);
  const [enabled, setEnabled] = useState(!!meeting.playback_enabled);
  const [loop, setLoop] = useState(!!meeting.playback_loop);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await api.listPlaybackItems(meeting.id);
      setItems(list);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [meeting.id]);

  useEffect(() => {
    if (!open) return;
    setEnabled(!!meeting.playback_enabled);
    setLoop(!!meeting.playback_loop);
    setErr(null);
    void refresh();
  }, [open, meeting, refresh]);

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
        await api.uploadPlaybackItem(meeting.id, file);
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
      await api.reorderPlaybackItems(
        meeting.id,
        next.map((x) => x.id),
      );
    } catch (e) {
      setErr((e as Error).message);
      await refresh();
    }
  }

  return (
    <div
      data-testid="playback-modal"
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 backdrop-blur-sm pt-10 px-4 pb-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("playback.title", { defaultValue: "Video playback" })}
    >
      <Card className="w-full max-w-xl relative max-h-[88vh] overflow-y-auto">
        <button
          type="button"
          onClick={onClose}
          aria-label={t("common.close", { defaultValue: "Close" })}
          data-testid="playback-modal-close"
          className="absolute top-3 right-3 p-1 rounded-md text-slate-300 hover:bg-primary-700"
        >
          <X size={18} />
        </button>
        <h2 className="text-lg font-semibold mb-1 flex items-center gap-2 text-slate-50">
          <Film size={18} className="text-accent-500" />
          {t("playback.title", { defaultValue: "Video playback" })}
        </h2>
        <p className="text-sm text-slate-400 mb-4">
          {t("playback.subtitle", {
            defaultValue:
              "Upload MP4 files to play to everyone in the meeting. When playback starts, all participants' mics and cameras are muted automatically and everyone sees the same single video stream.",
          })}
        </p>

        <div className="space-y-3">
          <Toggle
            id="playback-enabled"
            label={t("playback.enable", { defaultValue: "Enable video playback for this meeting" })}
            description={t("playback.enableDesc", {
              defaultValue:
                "When on, a Play / Stop button appears in the meeting toolbar (only once the playlist has at least one item). Playback never starts automatically — the host clicks Play.",
            })}
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
            description={t("playback.loopDesc", {
              defaultValue: "When the last item finishes, restart from the first.",
            })}
            checked={loop}
            onChange={toggleLoop}
          />
        </div>

        <div className="border-t border-primary-700 my-4" />

        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-200">
              {t("playback.playlistTitle", { defaultValue: "Playlist" })}
              {items && <span className="text-slate-500 font-normal"> ({items.length})</span>}
            </h3>
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
            <p className="text-sm text-slate-400">{t("playback.loading", { defaultValue: "Loading…" })}</p>
          ) : items.length === 0 ? (
            <p className="text-sm text-slate-500">
              {t("playback.empty", {
                defaultValue: "No videos yet. Add one or more MP4 files to build a playlist.",
              })}
            </p>
          ) : (
            // Cap the list at ~half the viewport so the toggles + upload
            // header stay visible above and the host can scroll long
            // playlists independently. The outer Card scroll still acts
            // as a safety net when the whole modal somehow grows past
            // 88vh (e.g. tiny screens).
            <ul className="flex flex-col divide-y divide-primary-700 max-h-[50vh] overflow-y-auto pr-1">
              {items.map((it, i) => {
                const isAlias = it.source_item_id !== null;
                return (
                <li
                  key={it.id}
                  data-testid={`playback-item-${it.id}`}
                  className="py-2.5 flex items-center gap-3 first:pt-0 last:pb-0"
                >
                  <span className="text-xs text-slate-500 w-5 text-right">{i + 1}.</span>
                  <div className="flex-1 min-w-0">
                    <div
                      className={[
                        "text-sm truncate flex items-center gap-1.5",
                        isAlias ? "text-slate-300 italic" : "text-slate-100",
                      ].join(" ")}
                      title={
                        isAlias
                          ? t("playback.aliasTitle", {
                              defaultValue: "Link to another video in this playlist",
                            })
                          : it.filename
                      }
                    >
                      {isAlias && (
                        <Link2 size={12} className="text-accent-500 flex-shrink-0" aria-hidden />
                      )}
                      <span className="truncate">{it.filename}</span>
                    </div>
                    <div className="text-xs text-slate-500">
                      {isAlias
                        ? t("playback.aliasLabel", { defaultValue: "Link" })
                        : fmtSize(it.file_size_bytes)}
                    </div>
                  </div>
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
                    disabled={!!busy || meeting.playback_current_item_id === it.id}
                    aria-label={t("playback.delete", { defaultValue: "Delete" })}
                    title={
                      meeting.playback_current_item_id === it.id
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
            <p className="text-xs text-slate-400" data-testid="playback-uploading">
              {t("playback.uploading", { defaultValue: "Uploading…" })}
            </p>
          )}
          {err && <div className="text-red-400 text-sm">{err}</div>}
        </div>
      </Card>
    </div>
  );
}

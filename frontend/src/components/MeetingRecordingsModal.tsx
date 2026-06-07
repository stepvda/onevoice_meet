import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Download, FileVideo, X, Loader2, AlertCircle } from "lucide-react";
import { api } from "../lib/api";
import { Card } from "./ui";

interface Recording {
  id: string;
  meeting_id: string;
  meeting_title: string | null;
  filename: string | null;
  status: string;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  file_size_bytes: number | null;
  has_local_file: boolean;
}

interface Props {
  open: boolean;
  onClose: () => void;
  meetingId: string;
}

/**
 * Modal listing recordings for a single in-progress meeting. Each row is
 * downloadable directly via `api.downloadRecording` (Bearer-authed Blob fetch).
 *
 * The endpoint is owner-only (server-side guarded); non-owners would get a
 * 403 and we surface that as an error. The modal re-fetches when reopened so
 * a recording the host just stopped becomes downloadable without a refresh.
 */
export default function MeetingRecordingsModal({ open, onClose, meetingId }: Props) {
  const { t } = useTranslation();
  const [rows, setRows] = useState<Recording[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setErr(null);
    (async () => {
      try {
        const r = await api.listMeetingRecordings(meetingId);
        if (!cancelled) setRows(r as Recording[]);
      } catch (e) {
        if (!cancelled) setErr((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, meetingId]);

  async function downloadOne(r: Recording) {
    setBusyId(r.id);
    setErr(null);
    try {
      await api.downloadRecording(r.id, r.filename ?? `meet-${r.meeting_id}-${r.id}.mp4`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  if (!open) return null;

  return (
    <div
      data-testid="meeting-recordings-modal"
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 backdrop-blur-sm pt-16 px-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("meetingRecordings.modalTitle", { defaultValue: "Recordings of this meeting" })}
    >
      <Card className="w-full max-w-2xl relative max-h-[85vh] overflow-y-auto">
        <button
          type="button"
          onClick={onClose}
          aria-label={t("common.close", { defaultValue: "Close" })}
          data-testid="meeting-recordings-close"
          className="absolute top-3 right-3 p-1 rounded-md text-slate-300 hover:bg-primary-700"
        >
          <X size={18} />
        </button>
        <h2 className="text-lg font-semibold mb-1 flex items-center gap-2 text-slate-50">
          <FileVideo size={18} className="text-accent-500" />
          {t("meetingRecordings.modalTitle", { defaultValue: "Recordings of this meeting" })}
        </h2>
        <p className="text-sm text-slate-400 mb-4">
          {t("meetingRecordings.modalSubtitle", {
            defaultValue:
              "All recordings of the current meeting. Files stay available until their retention window expires.",
          })}
        </p>

        {loading && (
          <div className="flex items-center gap-2 text-sm text-slate-300 py-6">
            <Loader2 size={16} className="animate-spin" />
            {t("common.loading", { defaultValue: "Loading…" })}
          </div>
        )}

        {!loading && err && (
          <div
            data-testid="meeting-recordings-error"
            className="flex items-start gap-2 text-sm text-red-300 bg-red-900/30 border border-red-700/50 rounded-md px-3 py-2"
          >
            <AlertCircle size={16} className="mt-0.5 flex-shrink-0" />
            <span>{err}</span>
          </div>
        )}

        {!loading && !err && rows.length === 0 && (
          <p
            data-testid="meeting-recordings-empty"
            className="text-sm text-slate-400 italic py-6 text-center"
          >
            {t("meetingRecordings.empty", {
              defaultValue:
                "No recordings yet. Start one from the toolbar and it'll appear here when it finishes.",
            })}
          </p>
        )}

        {!loading && !err && rows.length > 0 && (
          <ul data-testid="meeting-recordings-list" className="flex flex-col gap-2">
            {rows.map((r) => (
              <li
                key={r.id}
                data-testid={`meeting-recordings-row-${r.id}`}
                className="flex items-center justify-between gap-3 px-3 py-2 rounded-md bg-primary-800 border border-primary-700"
              >
                <div className="flex flex-col min-w-0">
                  <span className="text-sm text-slate-100 truncate" title={r.filename ?? r.id}>
                    {r.filename ?? r.id}
                  </span>
                  <span className="text-xs text-slate-400">
                    {formatStarted(r.started_at)}
                    {r.duration_seconds !== null && ` · ${formatDuration(r.duration_seconds)}`}
                    {r.file_size_bytes !== null && ` · ${formatBytes(r.file_size_bytes)}`}
                    {!r.has_local_file && r.status !== "running" && (
                      <>
                        {" · "}
                        <span className="text-amber-400">
                          {t("meetingRecordings.fileMissing", { defaultValue: "file unavailable" })}
                        </span>
                      </>
                    )}
                    {r.status === "running" && (
                      <>
                        {" · "}
                        <span className="text-emerald-400">
                          {t("meetingRecordings.statusRunning", { defaultValue: "recording…" })}
                        </span>
                      </>
                    )}
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => downloadOne(r)}
                  disabled={busyId === r.id || !r.has_local_file || r.status === "running"}
                  data-testid={`meeting-recordings-download-${r.id}`}
                  title={
                    r.status === "running"
                      ? t("meetingRecordings.downloadDisabledRunning", {
                          defaultValue: "Stop the recording before downloading",
                        })
                      : !r.has_local_file
                        ? t("meetingRecordings.downloadDisabledMissing", {
                            defaultValue: "File no longer available on the server",
                          })
                        : t("meetingRecordings.download", { defaultValue: "Download" })
                  }
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary-600 hover:bg-primary-500 text-slate-100 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0"
                >
                  {busyId === r.id ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <Download size={14} />
                  )}
                  <span className="hidden sm:inline">
                    {t("meetingRecordings.download", { defaultValue: "Download" })}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function formatStarted(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function formatDuration(sec: number): string {
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${h}h ${m.toString().padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${r.toString().padStart(2, "0")}s`;
  return `${r}s`;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

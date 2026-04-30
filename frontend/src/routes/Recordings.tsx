import { useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Download, ExternalLink, Trash2, Video } from "lucide-react";
import { api } from "../lib/api";
import { bootstrapFromOneWitysk, clearAccessToken, isAuthenticated } from "../lib/auth";
import { Button, Card } from "../components/ui";
import SignInPrompt from "../components/SignInPrompt";

interface Recording {
  id: string;
  meeting_id: string;
  filename: string | null;
  branding_url: string | null;
  status: string;
  started_at: string;
  ended_at: string | null;
  expires_at: string;
  expires_in_seconds: number | null;
  duration_seconds: number | null;
  file_size_bytes: number | null;
  has_local_file: boolean;
  youtube_url: string | null;
  youtube_status: string | null;
  youtube_error: string | null;
}

export default function Recordings() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<Recording[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!isAuthenticated()) {
        const tok = await bootstrapFromOneWitysk();
        if (!tok) {
          if (!cancelled) {
            setNeedsSignIn(true);
            setLoading(false);
          }
          return;
        }
      }
      try {
        const r = await api.listRecordings();
        if (!cancelled) setRows(r as Recording[]);
      } catch (e) {
        if (cancelled) return;
        // 401 from a stale upstream session — fall through to the sign-in
        // prompt instead of showing the raw HTTP error.
        const msg = (e as Error).message || "";
        if (/401|invalid token|expired/i.test(msg)) {
          clearAccessToken();
          setNeedsSignIn(true);
        } else {
          setErr(msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function downloadRecording(r: Recording) {
    setBusyId(`dl-${r.id}`);
    setErr(null);
    try {
      await api.downloadRecording(r.id, r.filename ?? `meet-${r.meeting_id}-${r.id}.mp4`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function deleteRow(r: Recording) {
    if (!confirm(t("recordings.deleteConfirm"))) return;
    setBusyId(r.id);
    try {
      await api.deleteRecording(r.id);
      setRows((cur) => cur.filter((x) => x.id !== r.id));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="p-4 lg:p-8 max-w-4xl mx-auto" data-testid="recordings-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-1 flex items-center gap-3">
        <Video size={22} className="text-accent-500" /> {t("recordings.title")}
      </h1>
      <p className="text-slate-400 mb-6">{t("recordings.subtitle")}</p>

      {needsSignIn && (
        <SignInPrompt
          icon={Video}
          title={t("recordings.signInTitle")}
          body={t("recordings.signInBody")}
          testId="recordings-signin"
        />
      )}

      {err && !needsSignIn && (
        <Card>
          <p className="text-red-400" data-testid="recordings-error">
            {err}
          </p>
        </Card>
      )}

      {loading && !err && !needsSignIn && (
        <Card>
          <p className="text-slate-300">{t("recordings.loading")}</p>
        </Card>
      )}

      {!loading && !err && !needsSignIn && rows.length === 0 && (
        <Card data-testid="recordings-empty">
          <p className="text-slate-300">
            <Trans i18nKey="recordings.empty" components={{ 1: <b /> }} />
          </p>
        </Card>
      )}

      {!loading && rows.length > 0 && (
        <div className="flex flex-col gap-3">
          {rows.map((r) => (
            <Card key={r.id} data-testid={`rec-row-${r.id}`}>
              <div className="flex items-start justify-between gap-4 flex-wrap">
                {r.branding_url && (
                  <img
                    src={r.branding_url}
                    alt=""
                    data-testid={`rec-branding-${r.id}`}
                    className="h-12 w-12 object-cover rounded-md border border-primary-700 flex-shrink-0"
                  />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <code className="text-sm text-slate-200 break-all">{r.filename ?? r.id}</code>
                    <Badge status={r.status} />
                    {r.youtube_status && <YtBadge status={r.youtube_status} />}
                  </div>
                  <div className="text-xs text-slate-400 mt-1">
                    {t("recordings.started", { when: new Date(r.started_at).toLocaleString() })}
                    {r.duration_seconds !== null && ` · ${formatDuration(r.duration_seconds)}`}
                    {r.file_size_bytes !== null && ` · ${formatBytes(r.file_size_bytes)}`}
                    {r.expires_in_seconds !== null && r.has_local_file && (
                      <span className="ml-2 text-slate-500">
                        {" "}
                        {t("recordings.expiresInDays", { count: Math.max(0, Math.floor(r.expires_in_seconds / 86400)) })}
                      </span>
                    )}
                  </div>
                  {r.youtube_url && (
                    <a
                      href={r.youtube_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      data-testid={`rec-youtube-link-${r.id}`}
                      className="inline-flex items-center gap-1 text-sm text-accent-500 hover:underline mt-2"
                    >
                      <ExternalLink size={14} />
                      {r.youtube_url}
                    </a>
                  )}
                  {r.youtube_status === "failed" && r.youtube_error && (
                    <div className="text-xs text-red-400 mt-2" data-testid={`rec-yt-err-${r.id}`}>
                      {t("recordings.lastUploadFailed", { message: r.youtube_error })}
                    </div>
                  )}
                </div>

                <div className="flex items-center gap-2 flex-wrap">
                  {r.status === "completed" && r.has_local_file && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={busyId === `dl-${r.id}`}
                      onClick={() => downloadRecording(r)}
                      data-testid={`rec-download-${r.id}`}
                    >
                      <Download size={16} />
                      {busyId === `dl-${r.id}` ? t("recordings.downloading") : t("recordings.download")}
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => deleteRow(r)}
                    disabled={busyId === r.id}
                    data-testid={`rec-delete-${r.id}`}
                    title={t("recordings.deleteTitle")}
                  >
                    <Trash2 size={16} />
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function Badge({ status }: { status: string }) {
  const { t } = useTranslation();
  const styles: Record<string, string> = {
    completed: "bg-accent-500/20 text-accent-500 border-accent-500/40",
    running: "bg-amber-500/20 text-amber-300 border-amber-500/40",
    failed: "bg-red-500/20 text-red-300 border-red-500/40",
    deleted: "bg-slate-600/20 text-slate-400 border-slate-600",
  };
  const label = t(`recordings.status.${status}`, { defaultValue: status });
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full border ${styles[status] ?? styles.deleted}`}>
      {label}
    </span>
  );
}

function YtBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  const styles: Record<string, string> = {
    uploading: "bg-amber-500/20 text-amber-300 border-amber-500/40",
    published: "bg-red-600/20 text-red-300 border-red-600/40",
    failed: "bg-red-500/20 text-red-300 border-red-500/40",
  };
  const label = t(`recordings.ytStatus.${status}`, { defaultValue: status });
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full border ${styles[status] ?? styles.failed}`}>
      {label}
    </span>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

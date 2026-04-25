import { useEffect, useState } from "react";
import { Download, ExternalLink, Loader2, Trash2, Upload, Video } from "lucide-react";
import { api } from "../lib/api";
import { bootstrapFromOneWitysk, isAuthenticated } from "../lib/auth";
import { Button, Card } from "../components/ui";

interface Recording {
  id: string;
  meeting_id: string;
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

type YtPrivacy = "public" | "unlisted" | "private";

export default function Recordings() {
  const [rows, setRows] = useState<Recording[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function refresh() {
    try {
      const r = await api.listRecordings();
      setRows(r as Recording[]);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!isAuthenticated()) {
        const tok = await bootstrapFromOneWitysk();
        if (!tok) {
          if (!cancelled) {
            setErr("Sign in on one.witysk.org first.");
            setLoading(false);
          }
          return;
        }
      }
      try {
        const r = await api.listRecordings();
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
  }, []);

  async function publishToYouTube(r: Recording) {
    const privacy = (
      prompt('Privacy: "public", "unlisted", or "private"', "unlisted") || ""
    ).toLowerCase();
    if (!["public", "unlisted", "private"].includes(privacy)) return;
    if (!confirm(
      `Upload this recording to YouTube as ${privacy}?\n` +
      `On success the local file will be deleted; only the YouTube link will remain.`
    )) return;

    setBusyId(r.id);
    setErr(null);
    try {
      const result = await api.publishYoutube(r.id, { privacy: privacy as YtPrivacy });
      // Optimistically update the row, then re-fetch the canonical state.
      setRows((cur) =>
        cur.map((x) =>
          x.id === r.id
            ? { ...x, youtube_url: result.url, youtube_status: "published", has_local_file: false }
            : x
        )
      );
      await refresh();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function downloadRecording(r: Recording) {
    setBusyId(`dl-${r.id}`);
    setErr(null);
    try {
      await api.downloadRecording(r.id, `meet-${r.meeting_id}-${r.id}.mp4`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function deleteRow(r: Recording) {
    if (!confirm("Delete this recording? The local file (if any) is removed but the YouTube link is kept.")) return;
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
        <Video size={22} className="text-accent-500" /> Recordings
      </h1>
      <p className="text-slate-400 mb-6">
        Server-side recordings of your meetings. Local files auto-delete after 30 days
        (or when you publish to YouTube). Uploads are owner-initiated only.
      </p>

      {err && (
        <Card>
          <p className="text-red-400" data-testid="recordings-error">
            {err}
          </p>
        </Card>
      )}

      {loading && !err && (
        <Card>
          <p className="text-slate-300">Loading…</p>
        </Card>
      )}

      {!loading && !err && rows.length === 0 && (
        <Card data-testid="recordings-empty">
          <p className="text-slate-300">
            No recordings yet. Start a meeting and click <b>Record</b> to capture one.
          </p>
        </Card>
      )}

      {!loading && rows.length > 0 && (
        <div className="flex flex-col gap-3">
          {rows.map((r) => (
            <Card key={r.id} data-testid={`rec-row-${r.id}`}>
              <div className="flex items-start justify-between gap-4 flex-wrap">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <code className="text-sm text-slate-200">{r.id}</code>
                    <Badge status={r.status} />
                    {r.youtube_status && <YtBadge status={r.youtube_status} />}
                  </div>
                  <div className="text-xs text-slate-400 mt-1">
                    Started {new Date(r.started_at).toLocaleString()}
                    {r.duration_seconds !== null && ` · ${formatDuration(r.duration_seconds)}`}
                    {r.file_size_bytes !== null && ` · ${formatBytes(r.file_size_bytes)}`}
                    {r.expires_in_seconds !== null && r.has_local_file && (
                      <span className="ml-2 text-slate-500">
                        (file expires in {Math.max(0, Math.floor(r.expires_in_seconds / 86400))} days)
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
                      Last upload failed: {r.youtube_error}
                    </div>
                  )}
                </div>

                <div className="flex items-center gap-2 flex-wrap">
                  {r.status === "completed" && r.has_local_file && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={busyId === r.id}
                      onClick={() => downloadRecording(r)}
                      data-testid={`rec-download-${r.id}`}
                    >
                      <Download size={16} />
                      {busyId === `dl-${r.id}` ? "Downloading…" : "Download"}
                    </Button>
                  )}
                  {r.status === "completed" && r.has_local_file && r.youtube_status !== "published" && (
                    <Button
                      type="button"
                      variant="accent"
                      size="sm"
                      disabled={busyId === r.id || r.youtube_status === "uploading"}
                      onClick={() => publishToYouTube(r)}
                      data-testid={`rec-publish-${r.id}`}
                      title="Upload to YouTube and delete the local file on success"
                    >
                      {busyId === r.id || r.youtube_status === "uploading" ? (
                        <>
                          <Loader2 size={16} className="animate-spin" />
                          Uploading…
                        </>
                      ) : (
                        <>
                          <Upload size={16} />
                          Publish to YouTube
                        </>
                      )}
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => deleteRow(r)}
                    disabled={busyId === r.id}
                    data-testid={`rec-delete-${r.id}`}
                    title="Delete this recording"
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
  const styles: Record<string, string> = {
    completed: "bg-accent-500/20 text-accent-500 border-accent-500/40",
    running: "bg-amber-500/20 text-amber-300 border-amber-500/40",
    failed: "bg-red-500/20 text-red-300 border-red-500/40",
    deleted: "bg-slate-600/20 text-slate-400 border-slate-600",
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full border ${styles[status] ?? styles.deleted}`}>
      {status}
    </span>
  );
}

function YtBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    uploading: "bg-amber-500/20 text-amber-300 border-amber-500/40",
    published: "bg-red-600/20 text-red-300 border-red-600/40",
    failed: "bg-red-500/20 text-red-300 border-red-500/40",
  };
  const label =
    status === "uploading" ? "YouTube uploading…" :
    status === "published" ? "on YouTube" :
    status === "failed" ? "YouTube failed" : status;
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

import { useEffect, useState } from "react";
import { Download, Video } from "lucide-react";
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
}

export default function Recordings() {
  const [rows, setRows] = useState<Recording[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

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

  return (
    <div className="p-4 lg:p-8 max-w-4xl mx-auto" data-testid="recordings-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-1 flex items-center gap-3">
        <Video size={22} className="text-accent-500" /> Recordings
      </h1>
      <p className="text-slate-400 mb-6">
        Server-side recordings of your meetings. Files are deleted automatically after 30 days.
      </p>

      {loading && (
        <Card>
          <p className="text-slate-300">Loading…</p>
        </Card>
      )}

      {!loading && err && (
        <Card>
          <p className="text-red-400" data-testid="recordings-error">
            {err}
          </p>
        </Card>
      )}

      {!loading && !err && rows.length === 0 && (
        <Card>
          <p className="text-slate-300">
            No recordings yet. Start a meeting and click <b>Start recording</b> to capture one.
          </p>
        </Card>
      )}

      {!loading && rows.length > 0 && (
        <div className="flex flex-col gap-3">
          {rows.map((r) => (
            <Card key={r.id} data-testid={`rec-row-${r.id}`}>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <code className="text-sm text-slate-200">{r.id}</code>
                    <Badge status={r.status} />
                  </div>
                  <div className="text-xs text-slate-400 mt-1">
                    Started {new Date(r.started_at).toLocaleString()}
                    {r.duration_seconds !== null && ` · ${formatDuration(r.duration_seconds)}`}
                    {r.file_size_bytes !== null && ` · ${formatBytes(r.file_size_bytes)}`}
                    {r.expires_in_seconds !== null && (
                      <span className="ml-2 text-slate-500">
                        (expires in {Math.max(0, Math.floor(r.expires_in_seconds / 86400))} days)
                      </span>
                    )}
                  </div>
                </div>
                {r.status === "completed" && (
                  <a
                    href={`/api/v1/recordings/${r.id}/download`}
                    className="shrink-0"
                    data-testid={`rec-download-${r.id}`}
                  >
                    <Button variant="accent" size="sm">
                      <Download size={16} />
                      Download
                    </Button>
                  </a>
                )}
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
    <span
      className={`text-xs px-2 py-0.5 rounded-full border ${styles[status] ?? styles.deleted}`}
    >
      {status}
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

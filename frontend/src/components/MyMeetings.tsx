import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Copy, LogIn, Lock, Trash2, Video } from "lucide-react";
import { api, MeetingOut } from "../lib/api";
import { Button, Card } from "./ui";

export default function MyMeetings({ refreshKey = 0 }: { refreshKey?: number }) {
  const [rows, setRows] = useState<MeetingOut[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .listMeetings()
      .then((m) => {
        if (!cancelled) setRows(m);
      })
      .catch((e) => {
        if (!cancelled) setErr((e as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  async function endMeeting(m: MeetingOut) {
    if (!confirm(`End "${m.display_title}" for everyone? This kicks all participants.`))
      return;
    setBusyId(m.id);
    try {
      await api.endMeeting(m.id);
      setRows((cur) => (cur ? cur.map((x) => (x.id === m.id ? { ...x, is_active: false } : x)) : cur));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function copyJoinUrl(m: MeetingOut) {
    const url = `${window.location.origin}/j/${m.room_name}`;
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      window.prompt("Copy this join link", url);
    }
  }

  if (rows === null && err === null) {
    return (
      <Card data-testid="my-meetings-loading">
        <p className="text-slate-300">Loading your meetings…</p>
      </Card>
    );
  }
  if (err) {
    return (
      <Card>
        <p className="text-red-400">{err}</p>
      </Card>
    );
  }
  if (!rows || rows.length === 0) {
    return (
      <Card data-testid="my-meetings-empty">
        <p className="text-slate-300">No meetings yet — create one below.</p>
      </Card>
    );
  }

  const active = rows.filter((m) => m.is_active);
  const closed = rows.filter((m) => !m.is_active);

  return (
    <div data-testid="my-meetings" className="flex flex-col gap-4">
      {active.length > 0 && (
        <Card>
          <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
            <Video size={18} className="text-accent-500" /> Active meetings
          </h2>
          <ul className="flex flex-col divide-y divide-primary-700">
            {active.map((m) => (
              <li
                key={m.id}
                data-testid={`meeting-row-${m.id}`}
                className="py-3 flex items-center gap-3 first:pt-0 last:pb-0"
              >
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-slate-50 truncate flex items-center gap-2">
                    {m.display_title}
                    {m.require_password && <Lock size={14} className="text-slate-400" />}
                  </div>
                  <div className="text-xs text-slate-400 mt-0.5">
                    <code>{m.room_name}</code> · max {m.max_participants}
                  </div>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => copyJoinUrl(m)}
                  data-testid={`meeting-copy-${m.id}`}
                  title="Copy join URL"
                >
                  <Copy size={16} />
                </Button>
                <Link to={`/j/${m.room_name}`}>
                  <Button variant="accent" size="sm" data-testid={`meeting-join-${m.id}`}>
                    <LogIn size={16} /> Join
                  </Button>
                </Link>
                <Button
                  type="button"
                  variant="danger"
                  size="sm"
                  disabled={busyId === m.id}
                  onClick={() => endMeeting(m)}
                  data-testid={`meeting-end-${m.id}`}
                  title="End meeting for everyone"
                >
                  <Trash2 size={16} /> End
                </Button>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {closed.length > 0 && (
        <Card>
          <h2 className="text-lg font-semibold mb-3 text-slate-300">
            Closed meetings
          </h2>
          <ul className="flex flex-col divide-y divide-primary-700">
            {closed.slice(0, 10).map((m) => (
              <li
                key={m.id}
                className="py-2 flex items-center gap-3 first:pt-0 last:pb-0 text-sm"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-slate-200 truncate">{m.display_title}</div>
                  <div className="text-xs text-slate-500">
                    <code>{m.room_name}</code>
                  </div>
                </div>
                <span className="text-xs text-slate-500">closed</span>
              </li>
            ))}
            {closed.length > 10 && (
              <li className="pt-3 text-xs text-slate-500">
                + {closed.length - 10} more
              </li>
            )}
          </ul>
        </Card>
      )}
    </div>
  );
}

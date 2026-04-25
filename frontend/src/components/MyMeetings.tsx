import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Copy, Eye, EyeOff, Globe, LogIn, Lock, Mail, RotateCcw, Trash2, Video } from "lucide-react";
import { api, MeetingOut } from "../lib/api";
import { Button, Card } from "./ui";
import InviteModal from "./InviteModal";

export default function MyMeetings({ refreshKey = 0 }: { refreshKey?: number }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [rows, setRows] = useState<MeetingOut[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [inviteFor, setInviteFor] = useState<MeetingOut | null>(null);

  function visibilityIcon(m: MeetingOut) {
    if (m.list_for_anonymous)
      return { icon: <Globe size={14} className="text-accent-500" />, label: t("myMeetings.visibilityPublic") };
    if (m.list_for_authenticated)
      return { icon: <Eye size={14} className="text-amber-400" />, label: t("myMeetings.visibilityListed") };
    return { icon: <EyeOff size={14} className="text-slate-500" />, label: t("myMeetings.visibilityPrivate") };
  }

  function joinAsOwner(m: MeetingOut) {
    sessionStorage.setItem(`owner:${m.room_name}`, m.id);
    navigate(`/${m.room_name}`);
  }

  async function cycleVisibility(m: MeetingOut) {
    // Three-step cycle: private → authenticated → public → private.
    let next: { list_for_authenticated: boolean; list_for_anonymous: boolean };
    if (m.list_for_anonymous) {
      next = { list_for_authenticated: false, list_for_anonymous: false };
    } else if (m.list_for_authenticated) {
      next = { list_for_authenticated: true, list_for_anonymous: true };
    } else {
      next = { list_for_authenticated: true, list_for_anonymous: false };
    }
    setBusyId(m.id);
    try {
      const updated = await api.updateMeeting(m.id, next);
      setRows((cur) => (cur ? cur.map((x) => (x.id === m.id ? { ...x, ...updated } : x)) : cur));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function restartMeeting(m: MeetingOut) {
    setBusyId(m.id);
    try {
      const updated = await api.reopenMeeting(m.id);
      setRows((cur) => (cur ? cur.map((x) => (x.id === m.id ? { ...x, ...updated } : x)) : cur));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function deleteClosed(m: MeetingOut) {
    if (!confirm(t("myMeetings.deleteClosedConfirm", { title: m.display_title }))) return;
    setBusyId(m.id);
    try {
      await api.deleteMeeting(m.id);
      setRows((cur) => (cur ? cur.filter((x) => x.id !== m.id) : cur));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

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
    if (!confirm(t("myMeetings.endMeetingConfirm", { title: m.display_title }))) return;
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
    const url = `${window.location.origin}/${m.room_name}`;
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      window.prompt(t("myMeetings.copyJoinPrompt"), url);
    }
  }

  if (rows === null && err === null) {
    return (
      <Card data-testid="my-meetings-loading">
        <p className="text-slate-300">{t("myMeetings.loading")}</p>
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
        <p className="text-slate-300">{t("myMeetings.empty")}</p>
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
            <Video size={18} className="text-accent-500" /> {t("myMeetings.active")}
          </h2>
          <ul className="flex flex-col divide-y divide-primary-700">
            {active.map((m) => (
              <li
                key={m.id}
                data-testid={`meeting-row-${m.id}`}
                className="py-3 flex items-center gap-3 first:pt-0 last:pb-0"
              >
                {m.branding_url && (
                  <img
                    src={m.branding_url}
                    alt=""
                    className="h-10 w-10 object-cover rounded-md border border-primary-700 flex-shrink-0"
                  />
                )}
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-slate-50 truncate flex items-center gap-2">
                    {m.display_title}
                    {m.require_password && <Lock size={14} className="text-slate-400" />}
                    <button
                      type="button"
                      onClick={() => cycleVisibility(m)}
                      disabled={busyId === m.id}
                      data-testid={`meeting-visibility-${m.id}`}
                      title={t("myMeetings.visibilityChange", { label: visibilityIcon(m).label })}
                      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-primary-900/40 hover:bg-primary-700/60 disabled:opacity-50 text-xs"
                    >
                      {visibilityIcon(m).icon}
                    </button>
                  </div>
                  <div className="text-xs text-slate-400 mt-0.5">
                    <code>{m.room_name}</code> · {t("myMeetings.maxParticipants", { n: m.max_participants })} · {visibilityIcon(m).label}
                  </div>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => copyJoinUrl(m)}
                  data-testid={`meeting-copy-${m.id}`}
                  title={t("myMeetings.copyTitle")}
                >
                  <Copy size={16} />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setInviteFor(m)}
                  data-testid={`meeting-invite-${m.id}`}
                  title={t("myMeetings.inviteTitle")}
                >
                  <Mail size={16} />
                </Button>
                <Button
                  type="button"
                  variant="accent"
                  size="sm"
                  onClick={() => joinAsOwner(m)}
                  data-testid={`meeting-join-${m.id}`}
                >
                  <LogIn size={16} /> {t("myMeetings.join")}
                </Button>
                <Button
                  type="button"
                  variant="danger"
                  size="sm"
                  disabled={busyId === m.id}
                  onClick={() => endMeeting(m)}
                  data-testid={`meeting-end-${m.id}`}
                  title={t("myMeetings.endTitle")}
                >
                  <Trash2 size={16} /> {t("myMeetings.end")}
                </Button>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {closed.length > 0 && (
        <Card>
          <h2 className="text-lg font-semibold mb-3 text-slate-300">
            {t("myMeetings.closed")}
          </h2>
          <ul className="flex flex-col divide-y divide-primary-700">
            {closed.slice(0, 25).map((m) => (
              <li
                key={m.id}
                data-testid={`closed-row-${m.id}`}
                className="py-2 flex items-center gap-3 first:pt-0 last:pb-0 text-sm"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-slate-200 truncate">{m.display_title}</div>
                  <div className="text-xs text-slate-500">
                    <code>{m.room_name}</code>
                  </div>
                </div>
                <span className="text-xs text-slate-500">{t("myMeetings.closedLabel")}</span>
                <Button
                  type="button"
                  variant="accent"
                  size="sm"
                  disabled={busyId === m.id}
                  onClick={() => restartMeeting(m)}
                  data-testid={`closed-restart-${m.id}`}
                  title={t("myMeetings.restartTitle")}
                >
                  <RotateCcw size={14} /> {t("myMeetings.restart")}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={busyId === m.id}
                  onClick={() => deleteClosed(m)}
                  data-testid={`closed-delete-${m.id}`}
                  title={t("myMeetings.deleteTitle")}
                >
                  <Trash2 size={14} />
                </Button>
              </li>
            ))}
            {closed.length > 25 && (
              <li className="pt-3 text-xs text-slate-500">
                {t("myMeetings.more", { count: closed.length - 25 })}
              </li>
            )}
          </ul>
        </Card>
      )}

      {inviteFor && (
        <InviteModal
          meetingId={inviteFor.id}
          meetingTitle={inviteFor.display_title}
          open={!!inviteFor}
          onClose={() => setInviteFor(null)}
        />
      )}
    </div>
  );
}

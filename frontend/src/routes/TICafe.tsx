/**
 * TI Café — surfaces the "currently online" panel from one.witysk.org's
 * Dashboard inside meet.witysk.org, and adds the always-on audio control bar
 * at the bottom (TICafeBar).
 *
 * Integration approach (no replication of presence/auth state):
 *   - Online users come from one.witysk.org/api/users/online (Bearer-authed,
 *     called from the browser so the session's IP-binding stays valid).
 *   - "Live in TI Café" set comes from meet-api /api/v1/ti-cafe/live, polled
 *     every 5 seconds. The flag is passed into <Facepic live={...}> so the
 *     purple ring + LIVE pill render anywhere we draw the user's circle.
 *   - The audio session itself is owned by <TICafeProvider>, which lives
 *     above this route, so navigating away does NOT drop the call.
 */
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Coffee, ExternalLink, MessageSquare, Smartphone } from "lucide-react";
import { api } from "../lib/api";
import { bootstrapFromOneWitysk, clearAccessToken, getAccessToken, isAuthenticated } from "../lib/auth";
import { useTICafe } from "../lib/tiCafe";
import { Button, Card, CardHeader } from "../components/ui";
import Facepic from "../components/Facepic";
import SignInPrompt from "../components/SignInPrompt";
import TICafeBar from "../components/TICafeBar";

const ONE_WITYSK = "https://one.witysk.org";
const POLL_USERS_MS = 30_000;
const POLL_LIVE_MS = 5_000;

class FetchError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

interface OnlineUser {
  id: number;
  username: string;
  name: string | null;
  facepic_path: string | null;
  is_mobile: boolean;
  is_birthday: boolean;
  vouch_count: number;
  open_to_talk: boolean;
}

async function fetchOnlineUsers(): Promise<OnlineUser[]> {
  const tok = getAccessToken();
  if (!tok) throw new FetchError(401, "no_token");
  const res = await fetch(`${ONE_WITYSK}/api/users/online`, {
    headers: { Authorization: `Bearer ${tok}` },
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new FetchError(res.status, detail || `HTTP ${res.status}`);
  }
  return (await res.json()) as OnlineUser[];
}

export default function TICafe() {
  const { t } = useTranslation();
  const ticafe = useTICafe();
  const [users, setUsers] = useState<OnlineUser[] | null>(null);
  const [liveIds, setLiveIds] = useState<Set<number>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const [authState, setAuthState] = useState<"checking" | "ok" | "anonymous">(
    isAuthenticated() ? "ok" : "checking"
  );
  const usersTimer = useRef<number | null>(null);
  const liveTimer = useRef<number | null>(null);

  // Make sure we have a token before doing anything.
  useEffect(() => {
    if (authState !== "checking") return;
    let cancelled = false;
    bootstrapFromOneWitysk().then((tok) => {
      if (cancelled) return;
      setAuthState(tok ? "ok" : "anonymous");
    });
    return () => {
      cancelled = true;
    };
  }, [authState]);

  // Online users — poll the onevoice endpoint every 30 s.
  useEffect(() => {
    if (authState !== "ok") return;
    let cancelled = false;
    const tick = async () => {
      try {
        const r = await fetchOnlineUsers();
        if (!cancelled) {
          setUsers(r);
          setErr(null);
        }
      } catch (e) {
        if (cancelled) return;
        if (e instanceof FetchError && e.status === 401) {
          clearAccessToken();
          setAuthState("anonymous");
          if (usersTimer.current) {
            window.clearInterval(usersTimer.current);
            usersTimer.current = null;
          }
          return;
        }
        setErr(t("tiCafe.loadFailed"));
      }
    };
    void tick();
    usersTimer.current = window.setInterval(tick, POLL_USERS_MS);
    return () => {
      cancelled = true;
      if (usersTimer.current) window.clearInterval(usersTimer.current);
    };
  }, [authState, t]);

  // TI Café live presence — poll meet-api every 5 s.
  useEffect(() => {
    if (authState !== "ok") return;
    let cancelled = false;
    const tick = async () => {
      try {
        const r = await api.tiCafeLive();
        if (!cancelled) setLiveIds(new Set(r.user_ids));
      } catch {
        /* tolerated — it's just presence; we'll retry on the next tick */
      }
    };
    void tick();
    liveTimer.current = window.setInterval(tick, POLL_LIVE_MS);
    return () => {
      cancelled = true;
      if (liveTimer.current) window.clearInterval(liveTimer.current);
    };
  }, [authState, ticafe.connected]);

  if (authState === "checking") {
    return (
      <div className="p-4 lg:p-8 max-w-3xl mx-auto" data-testid="ti-cafe-page">
        <Card>
          <p className="text-slate-300">{t("createMeeting.checkingSession")}</p>
        </Card>
      </div>
    );
  }

  if (authState === "anonymous") {
    return (
      <div className="p-4 lg:p-8 max-w-3xl mx-auto" data-testid="ti-cafe-page">
        <h1 className="text-2xl font-bold text-slate-50 mb-1 flex items-center gap-3">
          <Coffee size={22} className="text-accent-500" /> {t("tiCafe.title")}
        </h1>
        <p className="text-slate-400 mb-6">{t("tiCafe.subtitle")}</p>
        <SignInPrompt
          icon={Coffee}
          title={t("tiCafe.signInTitle")}
          body={t("tiCafe.signInBody")}
          testId="ti-cafe-signin"
        />
      </div>
    );
  }

  return (
    <div className="p-4 lg:p-8 max-w-3xl mx-auto" data-testid="ti-cafe-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-1 flex items-center gap-3">
        <Coffee size={22} className="text-accent-500" /> {t("tiCafe.title")}
      </h1>
      <p className="text-slate-400 mb-6">{t("tiCafe.subtitle")}</p>

      <Card>
        <CardHeader
          title={
            <span className="flex items-center gap-2">
              <span className="inline-block w-2 h-2 rounded-full bg-green-500 animate-pulse" />
              {t("tiCafe.currentlyOnline", { count: users?.length ?? 0 })}
            </span>
          }
        />

        {err && (
          <p className="text-red-400 text-sm" data-testid="ti-cafe-error">
            {err}
          </p>
        )}

        {!err && users === null && (
          <p className="text-slate-300">{t("recordings.loading")}</p>
        )}

        {!err && users !== null && users.length === 0 && (
          <p className="text-slate-400" data-testid="ti-cafe-empty">
            {t("tiCafe.empty")}
          </p>
        )}

        {!err && users && users.length > 0 && (
          <ul className="flex flex-col divide-y divide-primary-700">
            {users.map((u) => (
              <li
                key={u.id}
                data-testid={`ti-cafe-row-${u.id}`}
                className="py-3 flex items-center gap-3 first:pt-0 last:pb-0"
              >
                <Facepic user={u} size={40} live={liveIds.has(u.id)} />
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-slate-100 truncate flex items-center gap-1.5">
                    <span className="truncate">{u.name || u.username}</span>
                    {u.is_birthday && (
                      <span title={t("tiCafe.birthday")} className="text-amber-300 cursor-default">★</span>
                    )}
                    {u.is_mobile && (
                      <Smartphone size={13} className="text-slate-400" />
                    )}
                    {u.open_to_talk && (
                      <span
                        title={t("tiCafe.openToTalk")}
                        className="text-[9px] font-bold tracking-wider px-1.5 py-px rounded bg-accent-500/20 text-accent-500 border border-accent-500/40"
                      >
                        {t("tiCafe.dmMe")}
                      </span>
                    )}
                    {u.vouch_count > 0 && (
                      <span
                        title={t("tiCafe.vouches", { count: u.vouch_count })}
                        className="text-[10px] font-bold px-1.5 py-px rounded bg-primary-700 text-slate-100 border border-primary-600"
                      >
                        ✓ {u.vouch_count > 99 ? "99+" : u.vouch_count}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-slate-400 truncate">@{u.username}</div>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => window.open(`${ONE_WITYSK}/messages/${u.id}`, "_blank", "noopener,noreferrer")}
                  data-testid={`ti-cafe-message-${u.id}`}
                  title={t("tiCafe.message")}
                >
                  <MessageSquare size={14} />
                </Button>
                <Button
                  type="button"
                  variant="accent"
                  size="sm"
                  onClick={() => window.open(`${ONE_WITYSK}/profile/${u.username}`, "_blank", "noopener,noreferrer")}
                  data-testid={`ti-cafe-profile-${u.id}`}
                  title={t("tiCafe.viewProfile")}
                >
                  <ExternalLink size={14} /> {t("tiCafe.view")}
                </Button>
              </li>
            ))}
          </ul>
        )}

        {/* The audio control bar lives at the bottom of the card. The connection
            itself is owned by the global <TICafeProvider>, so it survives if
            the user navigates away to /recordings or /. */}
        <TICafeBar liveCount={liveIds.size} />
      </Card>
    </div>
  );
}

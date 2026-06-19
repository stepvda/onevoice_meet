import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Compass, Globe, LogIn, Lock, Radio, Tv } from "lucide-react";
import { api, PublicMeeting } from "../lib/api";
import { isAuthenticated } from "../lib/auth";
import { Button, Card } from "./ui";

const POLL_INTERVAL_MS = 10_000;

/**
 * Discover panel for the home page. Two subsections:
 *
 * 1. Meetings open to join — meetings owned by OTHER users that have opted
 *    into discoverability. Signed-in viewers use `/api/v1/discoverable`
 *    (authenticated OR anonymous visibility); anonymous viewers fall back to
 *    `/api/v1/public-meetings` (anonymous visibility only).
 * 2. Public livestreams — meetings with a public view-only page enabled
 *    (`public_enabled` + `public_slug`), fetched from the no-auth
 *    `/api/v1/public-streams` so even signed-out visitors can find streams to
 *    watch at /public/<slug>. These are watch-only and live in their own
 *    subsection rather than being mixed in with joinable rooms.
 *
 * Renders nothing when both lists are empty (no clutter on solo/private
 * setups).
 */
export default function DiscoverableMeetings() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [rows, setRows] = useState<PublicMeeting[] | null>(null);
  const [streams, setStreams] = useState<PublicMeeting[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Track whether we've ever had a successful fetch. After the first
  // success, transient poll failures stay silent — the user keeps seeing
  // the most recent lists rather than the section vanishing.
  const loadedOnceRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      // Re-check auth on every poll so signing in mid-session promotes the
      // user to the authenticated meetings endpoint without a page refresh.
      const auth = isAuthenticated();
      Promise.all([
        auth ? api.listDiscoverable() : api.listPublicMeetings(),
        api.listPublicStreams(),
      ])
        .then(([meetings, publicStreams]) => {
          if (cancelled) return;
          loadedOnceRef.current = true;
          setErr(null);
          setRows(meetings);
          setStreams(publicStreams);
        })
        .catch((e) => {
          if (cancelled || loadedOnceRef.current) return;
          setErr((e as Error).message);
        });
    };
    load();
    // Poll every 10s so newly-published meetings / streams show up without a
    // refresh.
    const id = window.setInterval(load, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  if (err) {
    // Quiet failure — discover isn't critical to the page.
    return null;
  }

  // Main list is the joinable meetings; public view-only streams live in
  // their own subsection below so they aren't mixed in with rooms you can
  // actually join.
  const meetings = (rows ?? []).filter((m) => m.joinable);
  const liveStreams = streams ?? [];
  if (meetings.length === 0 && liveStreams.length === 0) {
    return null;
  }

  return (
    <Card data-testid="discoverable-meetings">
      <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
        <Compass size={18} className="text-accent-500" />
        {t("discover.title")}
      </h2>

      {meetings.length > 0 && (
        <div data-testid="discover-meetings">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
            {t("discover.subtitle")}
          </h3>
          <ul className="flex flex-col divide-y divide-primary-700">
            {meetings.map((m) => (
              <li
                key={m.room_name}
                data-testid={`discover-row-${m.room_name}`}
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
                    <Globe size={14} className="text-accent-500" />
                  </div>
                  <div className="text-xs text-slate-400 mt-0.5">
                    <code>{m.room_name}</code> · {t("discover.maxParticipants", { n: m.max_participants })}
                    {m.owner_name && (
                      <span className="ml-2" data-testid={`discover-host-${m.room_name}`}>
                        · {t("discover.hostedBy", { name: m.owner_name, defaultValue: "Hosted by {{name}}" })}
                      </span>
                    )}
                  </div>
                </div>
                <Button
                  type="button"
                  variant="accent"
                  size="sm"
                  onClick={() => navigate(`/${m.room_name}`)}
                  data-testid={`discover-join-${m.room_name}`}
                >
                  <LogIn size={16} /> {t("discover.join")}
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {liveStreams.length > 0 && (
        <div
          data-testid="public-streams"
          className={meetings.length > 0 ? "mt-6" : undefined}
        >
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2 flex items-center gap-2">
            <Radio size={14} className="text-accent-500" />
            {t("discover.streamsTitle", { defaultValue: "Public livestreams" })}
            <span className="font-normal normal-case tracking-normal text-slate-500">
              {t("discover.streamsSubtitle", { defaultValue: "— watch-only, no account needed" })}
            </span>
          </h3>
          <ul className="flex flex-col divide-y divide-primary-700">
            {liveStreams.map((s) => (
              <li
                key={s.public_slug ?? s.room_name}
                data-testid={`stream-row-${s.room_name}`}
                className="py-3 flex items-center gap-3 first:pt-0 last:pb-0"
              >
                {s.branding_url && (
                  <img
                    src={s.branding_url}
                    alt=""
                    className="h-10 w-10 object-cover rounded-md border border-primary-700 flex-shrink-0"
                  />
                )}
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-slate-50 truncate flex items-center gap-2">
                    {s.display_title}
                    <Tv size={14} className="text-accent-500" />
                  </div>
                  <div className="text-xs text-slate-400 mt-0.5">
                    {s.owner_name ? (
                      t("discover.hostedBy", { name: s.owner_name, defaultValue: "Hosted by {{name}}" })
                    ) : (
                      <code>{s.room_name}</code>
                    )}
                  </div>
                </div>
                {s.public_slug && (
                  <Button
                    type="button"
                    variant="accent"
                    size="sm"
                    onClick={() => navigate(`/public/${s.public_slug}`)}
                    data-testid={`stream-view-${s.room_name}`}
                    title={t("discover.viewTitle", {
                      defaultValue: "Watch the public view-only stream",
                    })}
                  >
                    <Tv size={16} /> {t("discover.view", { defaultValue: "View" })}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

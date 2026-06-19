import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Clock, MonitorPlay, Play, Tv, X } from "lucide-react";
import { api, OnDemandMeeting, OnDemandVideo } from "../lib/api";
import { cleanPlaylistTitle } from "../lib/playlistName";
import { Button, Card } from "../components/ui";

const POLL_INTERVAL_MS = 30_000;

/**
 * On Demand — public catalogue of videos longer than five minutes, taken
 * from the playlists of ongoing meetings that have a public livestream.
 * One subsection per meeting. Reachable by anonymous visitors; clicking a
 * video streams it from the no-auth `/api/v1/on-demand/items/<id>` route.
 */
function fmtDuration(total: number): string {
  const s = Math.max(0, Math.round(total));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

export default function OnDemand() {
  const { t } = useTranslation();
  const [meetings, setMeetings] = useState<OnDemandMeeting[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [playing, setPlaying] = useState<OnDemandVideo | null>(null);

  useEffect(() => {
    let cancelled = false;
    let loadedOnce = false;
    const load = () => {
      api
        .listOnDemand()
        .then((r) => {
          if (cancelled) return;
          loadedOnce = true;
          setErr(null);
          setMeetings(r);
        })
        .catch((e) => {
          // Stay silent after the first successful load — keep showing the
          // last catalogue rather than flashing an error on a transient poll.
          if (cancelled || loadedOnce) return;
          setErr((e as Error).message);
        });
    };
    load();
    const id = window.setInterval(load, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  return (
    <div className="p-4 lg:p-8 max-w-4xl mx-auto flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-100 flex items-center gap-2">
          <MonitorPlay className="text-accent-500" />
          {t("onDemand.title", { defaultValue: "On Demand" })}
        </h1>
        <p className="text-slate-400 mt-1">
          {t("onDemand.subtitle", {
            defaultValue:
              "Videos from ongoing public livestreams — watch any time, no account needed.",
          })}
        </p>
      </div>

      {err && (
        <Card>
          <p className="text-slate-300">
            {t("onDemand.error", { defaultValue: "Couldn't load On Demand right now." })}
          </p>
        </Card>
      )}

      {!err && meetings === null && (
        <Card>
          <p className="text-slate-400">{t("onDemand.loading", { defaultValue: "Loading…" })}</p>
        </Card>
      )}

      {!err && meetings !== null && meetings.length === 0 && (
        <Card data-testid="on-demand-empty">
          <p className="text-slate-300">
            {t("onDemand.empty", {
              defaultValue:
                "Nothing on demand right now. Check back when a public livestream has videos in its playlist.",
            })}
          </p>
        </Card>
      )}

      {meetings?.map((m) => (
        <Card key={m.room_name} data-testid={`on-demand-meeting-${m.room_name}`}>
          <div className="flex items-center gap-3 mb-3">
            {m.branding_url && (
              <img
                src={m.branding_url}
                alt=""
                className="h-10 w-10 object-cover rounded-md border border-primary-700 flex-shrink-0"
              />
            )}
            <div className="min-w-0">
              <h2 className="text-lg font-semibold text-slate-50 truncate flex items-center gap-2">
                <Tv size={16} className="text-accent-500" />
                {m.display_title}
              </h2>
              {m.owner_name && (
                <div className="text-xs text-slate-400">
                  {t("discover.hostedBy", { name: m.owner_name, defaultValue: "Hosted by {{name}}" })}
                </div>
              )}
            </div>
          </div>
          <ul className="flex flex-col divide-y divide-primary-700">
            {m.videos.map((v) => (
              <li
                key={v.id}
                data-testid={`on-demand-video-${v.id}`}
                className="py-2.5 flex items-center gap-3 first:pt-0 last:pb-0"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-slate-100 truncate">{cleanPlaylistTitle(v.filename)}</div>
                  <div className="text-xs text-slate-400 mt-0.5 flex items-center gap-1">
                    <Clock size={12} /> {fmtDuration(v.duration_seconds)}
                  </div>
                </div>
                <Button
                  type="button"
                  variant="accent"
                  size="sm"
                  onClick={() => setPlaying(v)}
                  data-testid={`on-demand-play-${v.id}`}
                >
                  <Play size={16} /> {t("onDemand.watch", { defaultValue: "Watch" })}
                </Button>
              </li>
            ))}
          </ul>
        </Card>
      ))}

      {playing && (
        <div
          className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4"
          onClick={() => setPlaying(null)}
          data-testid="on-demand-player"
        >
          <div className="w-full max-w-4xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-2">
              <div className="text-slate-100 font-medium truncate pr-3">{cleanPlaylistTitle(playing.filename)}</div>
              <button
                type="button"
                onClick={() => setPlaying(null)}
                aria-label={t("common.close", { defaultValue: "Close" })}
                className="p-2 rounded-lg hover:bg-white/10 text-slate-200"
              >
                <X size={20} />
              </button>
            </div>
            {/* Same-origin source → allowed by the default-page CSP media-src 'self'. */}
            <video
              src={playing.stream_url}
              controls
              autoPlay
              playsInline
              className="w-full max-h-[80vh] rounded-lg bg-black"
            />
          </div>
        </div>
      )}
    </div>
  );
}

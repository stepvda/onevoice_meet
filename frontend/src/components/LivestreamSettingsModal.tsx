import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Radio, X } from "lucide-react";
import { api, MeetingOut } from "../lib/api";
import { Button, Card } from "./ui";
import LivestreamDestinationBlock from "./LivestreamDestinationBlock";
import YoutubeDestinationBlock from "./YoutubeDestinationBlock";
import { LIVESTREAM_DESTINATIONS } from "../lib/livestreamDestinations";

interface Props {
  meeting: MeetingOut;
  open: boolean;
  onClose: () => void;
  onSaved: (updated: MeetingOut) => void;
}

type DestState = { enabled: boolean; url: string; streamKey: string };

function seedFromMeeting(meeting: MeetingOut): Record<string, DestState> {
  const m = meeting as unknown as Record<string, unknown>;
  return Object.fromEntries(
    LIVESTREAM_DESTINATIONS.map((d) => [
      d.id,
      {
        enabled: !!m[d.fields.enabled],
        url: (m[d.fields.rtmps_url] as string | null) ?? "",
        streamKey: (m[d.fields.stream_key] as string | null) ?? "",
      },
    ]),
  );
}

/**
 * Edit a meeting's livestream destinations (X, Substack, YouTube, Facebook,
 * Rumble). Used both from MyMeetings and from the in-meeting toolbar so the
 * host can paste new credentials mid-call without leaving the room. When
 * multiple destinations are enabled, the egress fans the same composite out
 * to all of them — one Chrome, one encoder, N RTMP muxers.
 */
type DestStatus = {
  status: "idle" | "streaming" | "failed" | "complete";
  error: string | null;
  viewer_count?: number | null;
};

export default function LivestreamSettingsModal({ meeting, open, onClose, onSaved }: Props) {
  const { t } = useTranslation();
  const [state, setState] = useState<Record<string, DestState>>(() => seedFromMeeting(meeting));
  const [youtubeMode, setYoutubeMode] = useState<"rtmp" | "api">(
    (meeting.livestream_youtube_mode as "rtmp" | "api") ?? "rtmp",
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Live publish status per destination, updated by polling
  // `streamDestinations` every 4 s while the modal is open AND a
  // livestream is active. We keep polling even when status is
  // "failed" so a fixed key flips back to green within ~4 s.
  const [statuses, setStatuses] = useState<Record<string, DestStatus>>({});

  // Re-seed when the modal is reopened against a different meeting.
  useEffect(() => {
    if (!open) return;
    setState(seedFromMeeting(meeting));
    setYoutubeMode((meeting.livestream_youtube_mode as "rtmp" | "api") ?? "rtmp");
    setErr(null);
  }, [open, meeting]);

  // Poll per-destination status while the modal is open. We always
  // attempt the fetch — when no stream is active the backend returns
  // `idle` rows for every enabled destination, which still gives the
  // host useful "yes I have creds for this" feedback.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const rows = await api.streamDestinations(meeting.id);
        if (cancelled) return;
        setStatuses(
          Object.fromEntries(
            rows.map((r) => [
              r.platform_id,
              { status: r.status, error: r.error, viewer_count: r.viewer_count ?? null },
            ]),
          ),
        );
      } catch {
        /* not fatal — the dots just won't update this tick */
      }
    };
    void tick();
    const id = window.setInterval(tick, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [open, meeting.id]);

  if (!open) return null;

  function setDestState(id: string, next: DestState) {
    setState((cur) => ({ ...cur, [id]: next }));
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const body: Record<string, unknown> = Object.fromEntries(
        LIVESTREAM_DESTINATIONS.flatMap((d) => {
          const s = state[d.id];
          return [
            [d.fields.enabled, s.enabled],
            [d.fields.rtmps_url, s.url.trim() || null],
            [d.fields.stream_key, s.streamKey.trim() || null],
          ];
        }),
      );
      // Persist the YouTube mode selection alongside the credentials.
      // Backend rejects mode="api" if the channel isn't OAuth-connected,
      // so the modal won't silently end up in an unusable state.
      body.livestream_youtube_mode = youtubeMode;
      const updated = await api.updateMeeting(meeting.id, body);
      onSaved(updated);
      onClose();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      data-testid="livestream-modal"
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 backdrop-blur-sm pt-16 px-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("livestream.modalTitle", { defaultValue: "Configure live stream" })}
    >
      <Card className="w-full max-w-5xl relative max-h-[85vh] overflow-y-auto">
        <button
          type="button"
          onClick={onClose}
          aria-label={t("common.close", { defaultValue: "Close" })}
          data-testid="livestream-modal-close"
          className="absolute top-3 right-3 p-1 rounded-md text-slate-300 hover:bg-primary-700"
        >
          <X size={18} />
        </button>
        <h2 className="text-lg font-semibold mb-1 flex items-center gap-2 text-slate-50">
          <Radio size={18} className="text-accent-500" />
          {t("livestream.modalTitle", { defaultValue: "Configure live stream" })}
        </h2>
        <p className="text-sm text-slate-400 mb-4">
          {t("livestream.modalMeeting", { defaultValue: "Meeting:" })}{" "}
          <span className="text-slate-200">{meeting.display_title}</span>
        </p>

        <form onSubmit={save} className="flex flex-col gap-4">
          <p className="text-xs text-slate-400">
            {t("createMeeting.livestreamHint", {
              defaultValue:
                "Toggle one or more destinations. The in-meeting Start streaming button fans the same composite out to every enabled destination. Streaming is OFF by default — you start it manually when the meeting begins.",
            })}
          </p>

          {LIVESTREAM_DESTINATIONS.map((d, i) => {
            if (d.id === "youtube") {
              return (
                <YoutubeDestinationBlock
                  key={d.id}
                  dest={d}
                  enabled={state[d.id].enabled}
                  url={state[d.id].url}
                  streamKey={state[d.id].streamKey}
                  onChange={(next) => setDestState(d.id, next)}
                  isFirst={i === 0}
                  status={statuses[d.id]?.status}
                  statusError={statuses[d.id]?.error ?? null}
                  viewerCount={statuses[d.id]?.viewer_count ?? null}
                  meetingId={meeting.id}
                  mode={youtubeMode}
                  onModeChange={setYoutubeMode}
                  initialOauthConnected={!!meeting.livestream_youtube_oauth_connected}
                  initialChannelTitle={meeting.livestream_youtube_channel_title ?? null}
                  initialWatchUrl={meeting.livestream_youtube_watch_url ?? null}
                />
              );
            }
            return (
              <LivestreamDestinationBlock
                key={d.id}
                dest={d}
                enabled={state[d.id].enabled}
                url={state[d.id].url}
                streamKey={state[d.id].streamKey}
                onChange={(next) => setDestState(d.id, next)}
                isFirst={i === 0}
                status={statuses[d.id]?.status}
                statusError={statuses[d.id]?.error ?? null}
              />
            );
          })}

          <div className="flex items-center gap-3">
            <Button type="submit" disabled={busy} data-testid="ls-save">
              {busy
                ? t("common.saving", { defaultValue: "Saving…" })
                : t("common.save", { defaultValue: "Save" })}
            </Button>
            <Button type="button" variant="ghost" onClick={onClose}>
              {t("common.cancel", { defaultValue: "Cancel" })}
            </Button>
          </div>

          {err && <div className="text-red-400 text-sm">{err}</div>}
        </form>
      </Card>
    </div>
  );
}

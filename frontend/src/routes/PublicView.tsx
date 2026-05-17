import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useRoomContext,
} from "@livekit/components-react";
import { RoomEvent } from "livekit-client";
import type { RemoteParticipant } from "livekit-client";
import { api } from "../lib/api";
import type { PublicViewerTokenResponse } from "../lib/api";
import { roomOptions } from "../lib/livekit";
import PresenterSpotlight from "../components/PresenterSpotlight";
import OutputVolumeControl from "../components/OutputVolumeControl";

/**
 * View-only stream page at /public/<publicSlug>.
 *
 * Public viewers connect with a `hidden=true` LiveKit token so they
 * don't appear in the meeting's participants panel and don't count
 * against `max_participants`. They cannot publish anything — audio,
 * video, screenshare or data — and the page UI has no controls beyond
 * an output volume slider in the corner.
 */
export default function PublicView() {
  const { t } = useTranslation();
  const { publicSlug = "" } = useParams();
  const navigate = useNavigate();
  const [tokenResp, setTokenResp] = useState<PublicViewerTokenResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!publicSlug) {
      setErr("missing slug");
      return;
    }
    void (async () => {
      try {
        const r = await api.publicViewerToken(publicSlug, { display_name: "Viewer" });
        if (!cancelled) setTokenResp(r);
      } catch (e) {
        if (!cancelled) setErr((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [publicSlug]);

  if (err) {
    return (
      <div className="flex flex-col h-dvh w-screen bg-witysk-page items-center justify-center text-slate-200 gap-3 p-6 text-center">
        <h1 className="text-xl font-semibold">
          {t("publicView.notAvailableTitle", { defaultValue: "Stream not available" })}
        </h1>
        <p className="text-sm text-slate-400 max-w-md">
          {t("publicView.notAvailableBody", {
            defaultValue:
              "This public stream either doesn't exist, has ended, or is not currently being broadcast.",
          })}
        </p>
        <button
          type="button"
          onClick={() => navigate("/")}
          className="mt-3 px-4 py-2 rounded-lg bg-primary-700 hover:bg-primary-600 text-slate-100 text-sm"
        >
          {t("publicView.home", { defaultValue: "Back to home" })}
        </button>
      </div>
    );
  }

  if (!tokenResp) {
    return (
      <div className="flex flex-col h-dvh w-screen bg-witysk-page items-center justify-center text-slate-400 text-sm">
        {t("publicView.loading", { defaultValue: "Connecting to stream…" })}
      </div>
    );
  }

  const cfg = roomOptions(tokenResp);

  return (
    <LiveKitRoom
      serverUrl={cfg.serverUrl}
      token={cfg.token}
      connect
      audio={false}
      video={false}
      options={cfg.roomOptions}
      connectOptions={cfg.connectOptions}
    >
      <PublicViewerInner
        title={tokenResp.display_title}
        brandingUrl={tokenResp.branding_url}
      />
    </LiveKitRoom>
  );
}

function PublicViewerInner({
  title,
  brandingUrl,
}: {
  title: string;
  brandingUrl: string | null;
}) {
  const { t } = useTranslation();
  const room = useRoomContext();

  // Hidden viewers can subscribe but never publish. Defensively force-mute
  // local mic + camera in case any future change to roomOptions starts
  // capturing on join — `audio={false} video={false}` on the LiveKitRoom
  // already keeps capture off at connect time.
  useEffect(() => {
    const lp = room.localParticipant;
    void lp.setMicrophoneEnabled(false).catch(() => undefined);
    void lp.setCameraEnabled(false).catch(() => undefined);
    void lp.setScreenShareEnabled(false).catch(() => undefined);
  }, [room]);

  // Re-apply the persisted output volume whenever the set of remote
  // participants changes. OutputVolumeControl handles slider changes
  // and new tracks, but we want the very first audio packet to land at
  // the saved volume too.
  useEffect(() => {
    const apply = (p: RemoteParticipant) => {
      try {
        p.setVolume(1);
      } catch {
        /* track may not be subscribed yet */
      }
    };
    room.remoteParticipants.forEach(apply);
    room.on(RoomEvent.ParticipantConnected, apply);
    return () => {
      room.off(RoomEvent.ParticipantConnected, apply);
    };
  }, [room]);

  return (
    <div className="flex flex-col h-dvh w-screen bg-witysk-page overflow-hidden">
      <header
        data-testid="public-view-topbar"
        className="flex items-center gap-3 px-3 py-2 bg-primary-900/90 backdrop-blur border-b border-primary-700 flex-shrink-0"
      >
        {brandingUrl && (
          <img
            src={brandingUrl}
            alt=""
            className="h-8 w-8 object-cover rounded-md border border-primary-700 flex-shrink-0"
          />
        )}
        <div className="min-w-0 leading-tight flex-1">
          <div className="font-semibold text-slate-50 truncate" title={title}>
            {title}
          </div>
          <div className="text-xs text-slate-400">
            {t("publicView.subtitle", { defaultValue: "Live stream · view only" })}
          </div>
        </div>
        <OutputVolumeControl />
      </header>

      <div className="flex-1 min-h-0 relative">
        <PresenterSpotlight />
        <RoomAudioRenderer />
      </div>
    </div>
  );
}

import { useEffect } from "react";
import type { Room, RemoteTrackPublication } from "livekit-client";
import { RoomEvent, Track, VideoQuality } from "livekit-client";
import { usePreferences } from "./preferences";
import { useCqStore } from "./cq/connectionQualityStore";

const MAP: Record<"low" | "medium" | "high", VideoQuality> = {
  low: VideoQuality.LOW,
  medium: VideoQuality.MEDIUM,
  high: VideoQuality.HIGH,
};

/**
 * Applies `network.preferredVideoQuality` by calling `setVideoQuality` on
 * every subscribed camera/screen-share track. "auto" lets LiveKit's
 * adaptiveStream pick — we don't override.
 */
export function useVideoQualityPref(room: Room) {
  const pref = usePreferences((s) => s.network.preferredVideoQuality);
  const overrides = useCqStore((s) => s.overrides);
  useEffect(() => {
    if (pref === "auto") return;
    const target = MAP[pref];

    const apply = () => {
      const pinned = useCqStore.getState().overrides;
      for (const p of room.remoteParticipants.values()) {
        for (const pub of p.trackPublications.values()) {
          if (
            pub.kind === Track.Kind.Video &&
            (pub.source === Track.Source.Camera || pub.source === Track.Source.ScreenShare)
          ) {
            const key = `${p.identity}-${pub.source ?? ""}`;
            const override = pinned[key]?.videoQuality;
            if (override && override !== "auto") continue;
            try {
              (pub as RemoteTrackPublication).setVideoQuality(target);
            } catch {
              /* track may not be subscribed yet */
            }
          }
        }
      }
    };

    apply();
    room.on(RoomEvent.TrackSubscribed, apply);
    room.on(RoomEvent.ParticipantConnected, apply);
    return () => {
      room.off(RoomEvent.TrackSubscribed, apply);
      room.off(RoomEvent.ParticipantConnected, apply);
    };
  }, [room, pref, overrides]);
}

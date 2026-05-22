import { useEffect, useMemo, useState } from "react";
import {
  GridLayout,
  TrackRefContext,
  useRoomContext,
  useTracks,
} from "@livekit/components-react";
import { RoomEvent, Track } from "livekit-client";
import type { TrackReferenceOrPlaceholder } from "@livekit/components-react";
import FlippableTile from "./FlippableTile";
import { usePreferences } from "../lib/preferences";

/**
 * Layout that switches between grid and spotlight based on:
 *   1. `display.layout` preference (grid / speaker / spotlight / auto)
 *   2. `room.metadata.presenter_identity` for the legacy "Take stage" path
 *   3. `hideSelfView` to remove the local participant from the grid
 *
 * Pref semantics:
 *   - "auto" / "spotlight": existing behaviour (presenter spotlight or grid)
 *   - "grid": always grid, even if a presenter is set
 *   - "speaker": always spotlight the currently active speaker (or presenter
 *     if one is set)
 */
export default function PresenterSpotlight() {
  const room = useRoomContext();
  const display = usePreferences((s) => s.display);
  const [presenterId, setPresenterId] = useState<string | null>(null);
  const [activeSpeakerId, setActiveSpeakerId] = useState<string | null>(null);
  // Picture-in-Picture composition state, mirrored from the meeting's
  // server-side toggle via LiveKit room metadata. When `pipEnabled` is
  // true and there's a screenshare or playback (or, as fallback, an
  // active speaker), the stage renders main full-bleed + the chosen
  // webcam in the bottom-right corner. Layout matches the egress page
  // at `/egress-layout/pip` so recordings / livestreams look the same.
  const [pipEnabled, setPipEnabled] = useState(false);
  const [pipOverlayIdentity, setPipOverlayIdentity] = useState<string | null>(
    null,
  );

  useEffect(() => {
    const apply = () => {
      let md: Record<string, unknown> = {};
      try {
        md = JSON.parse(room.metadata || "{}");
      } catch {
        /* leave md empty */
      }
      setPresenterId(
        typeof md.presenter_identity === "string" ? md.presenter_identity : null,
      );
      setPipEnabled(!!md.pip_enabled);
      setPipOverlayIdentity(
        typeof md.pip_overlay_identity === "string"
          ? md.pip_overlay_identity
          : null,
      );
    };
    apply();
    // LiveKit only fires `RoomMetadataChanged` for *changes* after the
    // initial join — late joiners receive the room metadata silently
    // during the handshake and never see an event. Without the
    // additional `Connected` and `Reconnected` listeners,
    // PresenterSpotlight would stay stuck at the empty initial read
    // and miss `pip_enabled` set on the server before they joined.
    // Symptom we hit: the host's in-meeting tab showed PiP (their PATCH
    // *was* a change → event fired) while a `/public/<slug>` viewer
    // opened in a separate tab afterwards stayed un-composited.
    room.on(RoomEvent.RoomMetadataChanged, apply);
    room.on(RoomEvent.Connected, apply);
    room.on(RoomEvent.Reconnected, apply);
    return () => {
      room.off(RoomEvent.RoomMetadataChanged, apply);
      room.off(RoomEvent.Connected, apply);
      room.off(RoomEvent.Reconnected, apply);
    };
  }, [room]);

  // Active-speaker tracking runs whenever the "speaker" layout pref OR
  // PiP composition is active — both need it as a fallback "main".
  useEffect(() => {
    if (display.layout !== "speaker" && !pipEnabled) return;
    const apply = () => {
      const top = room.activeSpeakers[0];
      if (top) setActiveSpeakerId(top.identity);
    };
    apply();
    room.on(RoomEvent.ActiveSpeakersChanged, apply);
    return () => {
      room.off(RoomEvent.ActiveSpeakersChanged, apply);
    };
  }, [room, display.layout, pipEnabled]);

  const rawTracks = useTracks(
    [
      { source: Track.Source.Camera, withPlaceholder: true },
      { source: Track.Source.ScreenShare, withPlaceholder: false },
    ],
    { onlySubscribed: false }
  );

  const tracks: TrackReferenceOrPlaceholder[] = useMemo(() => {
    const me = room.localParticipant.identity;
    return rawTracks.filter((t) => {
      // Hide the compositor bot from the regular grid / sidebar — its
      // screenshare track is handled by the dedicated composite branch
      // below (renders full-bleed) and its participant entry never
      // belongs in a tile alongside humans.
      if (t.participant.identity.startsWith("composite-")) return false;
      if (display.hideSelfView && t.participant.identity === me) return false;
      // `hideEmptyTiles`: skip placeholder tiles (no published track).
      if (
        display.hideEmptyTiles &&
        !(t as { publication?: unknown }).publication
      ) {
        return false;
      }
      return true;
    });
  }, [rawTracks, display.hideSelfView, display.hideEmptyTiles, room.localParticipant.identity]);

  const focus = useMemo(() => {
    // 1. Pref "grid" always wins.
    if (display.layout === "grid") return null;
    // 2. Presenter set on room metadata → spotlight them.
    const pickFor = (identity: string | null) => {
      if (!identity) return null;
      return (
        tracks.find((t) => t.participant.identity === identity && t.source === Track.Source.ScreenShare) ??
        tracks.find((t) => t.participant.identity === identity) ??
        null
      );
    };
    if (presenterId) return pickFor(presenterId);
    // 3. `pinFirstScreenshare`: any active screen-share gets the spotlight
    //    even when no presenter has been explicitly chosen.
    if (display.pinFirstScreenshare) {
      const screen = tracks.find((t) => t.source === Track.Source.ScreenShare);
      if (screen) return screen;
    }
    // 4. Pref "speaker" → spotlight active speaker (if any).
    if (display.layout === "speaker") return pickFor(activeSpeakerId);
    // 5. Default ("auto" / "spotlight"): grid until someone takes the stage.
    return null;
  }, [presenterId, tracks, display.layout, display.pinFirstScreenshare, activeSpeakerId]);

  // Video-playback hijacks the stage: when LiveKit Ingress publishes the
  // current playlist item as participant identity "playback", show that
  // tile full-screen and hide every other tile (no sidebar grid). This is
  // independent of the presenter logic — playback always wins.
  const playbackTrack = useMemo(
    () => tracks.find((t) => t.participant.identity === "playback" && t.source === Track.Source.Camera),
    [tracks],
  );

  // Server-side PiP composite. When the meeting has `pip_enabled` on,
  // the compositor service publishes a ScreenShare track from identity
  // `composite-<room>`. Every client (including the publisher who
  // contributed the source tracks) shows this composite full-bleed,
  // hiding all raw tracks — so what people see live matches the
  // recording / livestream byte-for-byte.
  //
  // While `pipEnabled` is true but the compositor session hasn't
  // landed its first frame yet (~3 s after toggle), this is null and
  // we fall through to the default grid; once the track shows up we
  // swap to it without further input.
  const compositeTrack = useMemo(() => {
    return (
      rawTracks.find(
        (t) =>
          t.participant.identity.startsWith("composite-") &&
          t.source === Track.Source.ScreenShare,
      ) ?? null
    );
  }, [rawTracks]);

  if (compositeTrack) {
    return (
      <div className="relative h-full bg-black overflow-hidden">
        <div className="absolute inset-0">
          <TrackRefContext.Provider value={compositeTrack}>
            <FlippableTile />
          </TrackRefContext.Provider>
        </div>
      </div>
    );
  }

  // Client-side PiP composition. Identical priority ladder + visual
  // layout as `/egress-layout/pip` so recordings + livestream + live
  // viewers all see the same thing.
  //
  //   main (full-bleed):  screenshare > playback > active speaker > any cam
  //   overlay (corner):   the `pip_overlay_identity` camera
  const pipMain = (() => {
    if (!pipEnabled) return null;
    const screen = tracks.find((t) => t.source === Track.Source.ScreenShare);
    if (screen) return screen;
    if (playbackTrack) return playbackTrack;
    if (activeSpeakerId) {
      const sp = tracks.find(
        (t) =>
          t.participant.identity === activeSpeakerId &&
          t.source === Track.Source.Camera,
      );
      if (sp) return sp;
    }
    return tracks.find((t) => t.source === Track.Source.Camera) ?? null;
  })();

  const pipOverlayTrack = (() => {
    if (!pipEnabled || !pipOverlayIdentity) return null;
    return (
      tracks.find(
        (t) =>
          t.participant.identity === pipOverlayIdentity &&
          t.source === Track.Source.Camera,
      ) ?? null
    );
  })();

  if (pipEnabled && pipMain) {
    // Don't render the same person as main AND overlay.
    const showOverlay =
      pipOverlayTrack &&
      !(
        pipOverlayTrack.participant.identity === pipMain.participant.identity &&
        pipOverlayTrack.source === pipMain.source
      );
    return (
      <div className="relative h-full bg-black overflow-hidden">
        <div className="absolute inset-0">
          <TrackRefContext.Provider value={pipMain}>
            <FlippableTile />
          </TrackRefContext.Provider>
        </div>
        {showOverlay && pipOverlayTrack && (
          <div
            data-testid="pip-overlay"
            className="absolute right-3 bottom-3 sm:right-4 sm:bottom-4 w-[28%] sm:w-[22%] aspect-video rounded-lg overflow-hidden border-2 border-white/90 shadow-xl bg-black"
          >
            <TrackRefContext.Provider value={pipOverlayTrack}>
              <FlippableTile />
            </TrackRefContext.Provider>
          </div>
        )}
      </div>
    );
  }

  if (playbackTrack) {
    return (
      <div className="flex h-full bg-black">
        <div className="flex-1 min-w-0 min-h-0">
          <TrackRefContext.Provider value={playbackTrack}>
            <FlippableTile />
          </TrackRefContext.Provider>
        </div>
      </div>
    );
  }

  if (focus) {
    const others = tracks.filter(
      (t) => !(t.participant.identity === focus.participant.identity && t.source === focus.source)
    );
    return (
      <div className="flex h-full gap-2 p-2 flex-col landscape:flex-row sm:flex-row">
        <div className="flex-[3] min-w-0 min-h-0">
          <TrackRefContext.Provider value={focus}>
            <FlippableTile />
          </TrackRefContext.Provider>
        </div>
        <div className="flex-1 min-w-0 min-h-0 overflow-auto">
          <GridLayout tracks={others}>
            <FlippableTile />
          </GridLayout>
        </div>
      </div>
    );
  }

  return (
    <GridLayout tracks={tracks} className="h-full p-2">
      <FlippableTile />
    </GridLayout>
  );
}

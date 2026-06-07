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

type RoomLayout = "single-speaker" | "speaker" | "grid";

/**
 * Room-wide composition shared between every live viewer, the recording,
 * and the livestream. The layout is the room's, not per-user:
 *
 *   - "single-speaker": one full-bleed main = screenshare > playback >
 *     presenter (Take stage) > active speaker > any cam.
 *   - "speaker": same main, plus a centered bottom thumbnail strip of
 *     every OTHER camera-publishing participant.
 *   - "grid": equal-tile grid of everyone.
 *
 * Host changes the layout via the toolbar picker, which POSTs to
 * `/meetings/{id}/layout`. That endpoint persists the choice on the
 * meeting AND pushes it to LiveKit room metadata, so every connected
 * client re-renders in lockstep on `RoomMetadataChanged`.
 *
 * Two things stay isolated from `roomLayout`:
 *   1. Server-side composite (the PiP compositor publishes a track from
 *      `composite-<room>`). When present, it's the final composite and we
 *      render it full-bleed regardless of the room layout.
 *   2. Client-side PiP (room metadata `pip_enabled` + `pip_overlay_identity`).
 *      Same fallback layout as the old PiP page — main + corner overlay,
 *      full-bleed. PiP is a separate toggle and takes precedence over the
 *      room layout when on.
 *
 * Note: the per-user `display.layout` zustand pref (auto/grid/speaker/
 * spotlight) is now ignored at composition time. `hideSelfView` and
 * `hideEmptyTiles` are still honoured.
 */
const VALID_ROOM_LAYOUTS: ReadonlySet<RoomLayout> = new Set([
  "single-speaker",
  "speaker",
  "grid",
]);

function parseRoomLayout(v: unknown): RoomLayout | null {
  return typeof v === "string" && VALID_ROOM_LAYOUTS.has(v as RoomLayout)
    ? (v as RoomLayout)
    : null;
}

export default function PresenterSpotlight() {
  const room = useRoomContext();
  const display = usePreferences((s) => s.display);
  const [roomLayout, setRoomLayout] = useState<RoomLayout>("grid");
  const [presenterId, setPresenterId] = useState<string | null>(null);
  const [activeSpeakerId, setActiveSpeakerId] = useState<string | null>(null);
  // Picture-in-Picture is independent of `roomLayout`; mirrors the
  // meeting's server-side toggle via LiveKit room metadata.
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
      const layout = parseRoomLayout(md.room_layout);
      if (layout) setRoomLayout(layout);
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
    // during the handshake and never see an event. The `Connected` and
    // `Reconnected` listeners cover that initial-state gap.
    room.on(RoomEvent.RoomMetadataChanged, apply);
    room.on(RoomEvent.Connected, apply);
    room.on(RoomEvent.Reconnected, apply);
    return () => {
      room.off(RoomEvent.RoomMetadataChanged, apply);
      room.off(RoomEvent.Connected, apply);
      room.off(RoomEvent.Reconnected, apply);
    };
  }, [room]);

  // Active-speaker tracking — needed in single-speaker, speaker, and PiP
  // modes. Cheaper to always subscribe than to gate by layout (the event
  // fires regardless and toggling subscribe on/off is its own footgun).
  useEffect(() => {
    const apply = () => {
      const top = room.activeSpeakers[0];
      if (top) setActiveSpeakerId(top.identity);
    };
    apply();
    room.on(RoomEvent.ActiveSpeakersChanged, apply);
    return () => {
      room.off(RoomEvent.ActiveSpeakersChanged, apply);
    };
  }, [room]);

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
      // Hide the compositor bot from regular tiles — its screenshare
      // track is consumed by the composite branch below (full-bleed) and
      // its entry never belongs in a tile alongside humans.
      if (t.participant.identity.startsWith("composite-")) return false;
      if (display.hideSelfView && t.participant.identity === me) return false;
      if (
        display.hideEmptyTiles &&
        !(t as { publication?: unknown }).publication
      ) {
        return false;
      }
      return true;
    });
  }, [rawTracks, display.hideSelfView, display.hideEmptyTiles, room.localParticipant.identity]);

  // Video-playback hijacks the stage: when LiveKit Ingress publishes the
  // current playlist item as participant identity "playback", that tile
  // dominates in single-speaker and speaker layouts. In grid mode the
  // playback tile still wins (it's the meeting's primary content).
  const playbackTrack = useMemo(
    () => tracks.find((t) => t.participant.identity === "playback" && t.source === Track.Source.Camera) ?? null,
    [tracks],
  );

  // Server-side PiP composite. When the meeting has `pip_enabled` on,
  // the compositor service publishes a ScreenShare track from identity
  // `composite-<room>`. Every client (including the publisher who
  // contributed the source tracks) shows this composite full-bleed,
  // hiding all raw tracks — so what people see live matches the
  // recording / livestream byte-for-byte. While `pipEnabled` is true but
  // the compositor session hasn't landed its first frame yet (~3 s
  // after toggle), this is null and we fall through to the client-side
  // PiP fallback below.
  const compositeTrack = useMemo(() => {
    return (
      rawTracks.find(
        (t) =>
          t.participant.identity.startsWith("composite-") &&
          t.source === Track.Source.ScreenShare,
      ) ?? null
    );
  }, [rawTracks]);

  // Main video for single-speaker / speaker layouts AND for the
  // client-side PiP fallback. Same priority ladder as the egress page so
  // live + recording / livestream pick the same person.
  const main = useMemo<TrackReferenceOrPlaceholder | null>(() => {
    const screen = tracks.find((t) => t.source === Track.Source.ScreenShare);
    if (screen) return screen;
    if (playbackTrack) return playbackTrack;
    const pickCamFor = (identity: string | null) => {
      if (!identity) return null;
      return (
        tracks.find(
          (t) => t.participant.identity === identity && t.source === Track.Source.Camera,
        ) ?? null
      );
    };
    return (
      pickCamFor(presenterId) ??
      pickCamFor(activeSpeakerId) ??
      tracks.find((t) => t.source === Track.Source.Camera) ??
      null
    );
  }, [tracks, playbackTrack, presenterId, activeSpeakerId]);

  // ── 1. Server composite always wins ─────────────────────────────────
  if (compositeTrack) {
    return <FullBleed track={compositeTrack} />;
  }

  // ── 2. Client-side PiP fallback (active when pip_enabled but the
  //       compositor track hasn't landed yet) ─────────────────────────
  if (pipEnabled && main) {
    const pipOverlayTrack: TrackReferenceOrPlaceholder | null =
      pipOverlayIdentity
        ? tracks.find(
            (t) =>
              t.participant.identity === pipOverlayIdentity &&
              t.source === Track.Source.Camera,
          ) ?? null
        : null;
    const showOverlay =
      pipOverlayTrack &&
      !(
        pipOverlayTrack.participant.identity === main.participant.identity &&
        pipOverlayTrack.source === main.source
      );
    return (
      <div className="relative h-full bg-black overflow-hidden">
        <div className="absolute inset-0">
          <TrackRefContext.Provider value={main}>
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

  // ── 3. Playback hijack (full-bleed in single-speaker / speaker; in
  //       grid we still tile but playback is the natural "main") ─────
  if (playbackTrack && roomLayout !== "grid") {
    return <FullBleed track={playbackTrack} />;
  }

  // ── 4. Room layout ───────────────────────────────────────────────────
  // Grid + active screenshare auto-promotes to "speaker": the shared
  // screen owns the main tile and webcams form a thumbnail strip. Same
  // rule as the egress page, so live and recording match.
  const hasScreenshare = tracks.some((t) => t.source === Track.Source.ScreenShare);
  const effectiveRoomLayout: RoomLayout =
    roomLayout === "grid" && hasScreenshare ? "speaker" : roomLayout;

  if (effectiveRoomLayout === "grid") {
    return (
      <GridLayout tracks={tracks} className="h-full p-2">
        <FlippableTile />
      </GridLayout>
    );
  }

  if (!main) {
    // No video yet — render an empty grid so placeholders fill the stage
    // gracefully instead of going black.
    return (
      <GridLayout tracks={tracks} className="h-full p-2">
        <FlippableTile />
      </GridLayout>
    );
  }

  if (effectiveRoomLayout === "single-speaker") {
    return <FullBleed track={main} />;
  }

  // effective "speaker": main + bottom thumbnail strip of others.
  const others = tracks.filter(
    (t) => !(t.participant.identity === main.participant.identity && t.source === main.source),
  );
  return (
    <div className="flex h-full flex-col bg-black">
      <div className="flex-1 min-h-0 p-2">
        <div className="relative h-full">
          <div className="absolute inset-0">
            <TrackRefContext.Provider value={main}>
              <FlippableTile />
            </TrackRefContext.Provider>
          </div>
        </div>
      </div>
      {others.length > 0 && (
        <div
          data-testid="speaker-thumbnails"
          className="h-[20%] min-h-[120px] flex justify-center items-stretch gap-2 px-3 py-2 bg-black/40 overflow-x-auto"
        >
          {others.map((t) => (
            <div
              key={`${t.participant.identity}-${t.source}`}
              className="aspect-video h-full flex-shrink-0 rounded-md overflow-hidden bg-primary-900"
            >
              <TrackRefContext.Provider value={t}>
                <FlippableTile />
              </TrackRefContext.Provider>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function FullBleed({ track }: { track: TrackReferenceOrPlaceholder }) {
  return (
    <div className="relative h-full bg-black overflow-hidden">
      <div className="absolute inset-0">
        <TrackRefContext.Provider value={track}>
          <FlippableTile />
        </TrackRefContext.Provider>
      </div>
    </div>
  );
}

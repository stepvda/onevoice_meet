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

  useEffect(() => {
    const apply = () => {
      try {
        const md = JSON.parse(room.metadata || "{}");
        setPresenterId(md.presenter_identity ?? null);
      } catch {
        setPresenterId(null);
      }
    };
    apply();
    room.on(RoomEvent.RoomMetadataChanged, apply);
    return () => {
      room.off(RoomEvent.RoomMetadataChanged, apply);
    };
  }, [room]);

  useEffect(() => {
    if (display.layout !== "speaker") return;
    const apply = () => {
      const top = room.activeSpeakers[0];
      if (top) setActiveSpeakerId(top.identity);
    };
    apply();
    room.on(RoomEvent.ActiveSpeakersChanged, apply);
    return () => {
      room.off(RoomEvent.ActiveSpeakersChanged, apply);
    };
  }, [room, display.layout]);

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

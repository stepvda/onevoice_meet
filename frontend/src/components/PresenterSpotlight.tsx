import { useEffect, useMemo, useState } from "react";
import {
  GridLayout,
  TrackRefContext,
  useRoomContext,
  useTracks,
} from "@livekit/components-react";
import { RoomEvent, Track } from "livekit-client";
import FlippableTile from "./FlippableTile";

/**
 * Layout that switches between grid view and spotlight view based on
 * `room.metadata.presenter_identity`. The backend writes that key on
 * POST /api/v1/meetings/{id}/presenter.
 */
export default function PresenterSpotlight() {
  const room = useRoomContext();
  const [presenterId, setPresenterId] = useState<string | null>(null);

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

  const tracks = useTracks(
    [
      { source: Track.Source.Camera, withPlaceholder: true },
      { source: Track.Source.ScreenShare, withPlaceholder: false },
    ],
    { onlySubscribed: false }
  );

  const focus = useMemo(() => {
    if (!presenterId) return null;
    // Prefer a screenshare over a camera if the presenter is sharing both.
    return (
      tracks.find((t) => t.participant.identity === presenterId && t.source === Track.Source.ScreenShare) ??
      tracks.find((t) => t.participant.identity === presenterId) ??
      null
    );
  }, [presenterId, tracks]);

  if (focus) {
    const others = tracks.filter(
      (t) => !(t.participant.identity === focus.participant.identity && t.source === focus.source)
    );
    return (
      <div className="flex h-full gap-2 p-2">
        <div className="flex-[3] min-w-0">
          <TrackRefContext.Provider value={focus}>
            <FlippableTile />
          </TrackRefContext.Provider>
        </div>
        <div className="flex-1 min-w-0 overflow-y-auto">
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

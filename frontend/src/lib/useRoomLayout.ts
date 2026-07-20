import { useEffect, useState } from "react";
import { useRoomContext } from "@livekit/components-react";
import { RoomEvent } from "livekit-client";

export type RoomLayout = "single-speaker" | "speaker" | "grid";

/**
 * The room-wide layout, read from LiveKit room metadata (the same source
 * PresenterSpotlight composes from). Available to every participant — not just
 * the owner — so viewer-side UI (e.g. the grid-only tile-shape toggle in the
 * toolbar) can show/hide itself in lockstep with the host's layout choice.
 *
 * Covers the late-joiner gap the same way PresenterSpotlight does: metadata
 * arrives silently during the join handshake, so we also re-read on Connected
 * and Reconnected, not only on RoomMetadataChanged.
 */
export function useRoomLayout(): RoomLayout {
  const room = useRoomContext();
  const [layout, setLayout] = useState<RoomLayout>("grid");
  useEffect(() => {
    const apply = () => {
      let md: Record<string, unknown> = {};
      try {
        md = JSON.parse(room.metadata || "{}");
      } catch {
        /* leave md empty */
      }
      const l = md.room_layout;
      setLayout(
        l === "grid" || l === "speaker" || l === "single-speaker" ? l : "grid",
      );
    };
    apply();
    room.on(RoomEvent.RoomMetadataChanged, apply);
    room.on(RoomEvent.Connected, apply);
    room.on(RoomEvent.Reconnected, apply);
    return () => {
      room.off(RoomEvent.RoomMetadataChanged, apply);
      room.off(RoomEvent.Connected, apply);
      room.off(RoomEvent.Reconnected, apply);
    };
  }, [room]);
  return layout;
}

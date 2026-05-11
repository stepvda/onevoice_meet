import { useEffect, useRef } from "react";
import type { Room } from "livekit-client";
import { ConnectionState, RoomEvent } from "livekit-client";

/**
 * Reads the local participant's join metadata stamped by the backend
 * `anon_token` endpoint and, if present, mutes the mic / disables the
 * camera on first connect. Runs once per session — after that the user
 * can unmute / re-enable normally.
 */
export function useJoinPolicy(room: Room) {
  const applied = useRef(false);
  useEffect(() => {
    const apply = async () => {
      if (applied.current) return;
      if (room.state !== ConnectionState.Connected) return;
      const raw = room.localParticipant.metadata;
      if (!raw) {
        applied.current = true;
        return;
      }
      let policy: { auto_mute?: boolean; auto_disable_camera?: boolean } = {};
      try {
        policy = JSON.parse(raw);
      } catch {
        applied.current = true;
        return;
      }
      applied.current = true;
      const lp = room.localParticipant;
      try {
        if (policy.auto_mute) await lp.setMicrophoneEnabled(false);
      } catch {
        /* ignore — owner may have disabled mic publish entirely */
      }
      try {
        if (policy.auto_disable_camera) await lp.setCameraEnabled(false);
      } catch {
        /* ignore */
      }
    };
    void apply();
    const handler = () => {
      void apply();
    };
    room.on(RoomEvent.Connected, handler);
    return () => {
      room.off(RoomEvent.Connected, handler);
    };
  }, [room]);
}

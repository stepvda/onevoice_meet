import { useEffect, useRef } from "react";
import type { Room, RemoteParticipant } from "livekit-client";
import { RoomEvent } from "livekit-client";
import { usePreferences } from "./preferences";

const CHAT_DATA_TOPIC = "meet-chat";

function playTone(freq: number, durationMs: number, gain: number) {
  try {
    const w = window as unknown as {
      AudioContext?: typeof AudioContext;
      webkitAudioContext?: typeof AudioContext;
    };
    const Ctx = w.AudioContext ?? w.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    g.gain.value = Math.max(0, gain);
    osc.connect(g).connect(ctx.destination);
    osc.start();
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + durationMs / 1000);
    osc.stop(ctx.currentTime + durationMs / 1000 + 0.05);
    osc.onended = () => ctx.close();
  } catch {
    /* audio context unavailable — silently no-op */
  }
}

export function useJoinSound(room: Room) {
  const enabled = usePreferences((s) => s.notifications.soundOnJoin);
  const ignoreOwn = usePreferences((s) => s.notifications.ignoreOwnJoins);
  const volume = usePreferences((s) => s.notifications.notificationVolume);
  // Suppress the initial flood that fires when we connect — LiveKit synthesises
  // ParticipantConnected for every participant already in the room.
  const ready = useRef(false);

  useEffect(() => {
    ready.current = false;
    const t = window.setTimeout(() => {
      ready.current = true;
    }, 1500);
    return () => window.clearTimeout(t);
  }, [room]);

  useEffect(() => {
    if (!enabled) return;
    const onJoin = (p: RemoteParticipant) => {
      if (!ready.current) return;
      if (ignoreOwn && p.isLocal) return;
      playTone(880, 180, (volume / 100) * 0.12);
    };
    room.on(RoomEvent.ParticipantConnected, onJoin);
    return () => {
      room.off(RoomEvent.ParticipantConnected, onJoin);
    };
  }, [room, enabled, ignoreOwn, volume]);
}

export function useChatSound(room: Room) {
  const enabled = usePreferences((s) => s.notifications.chatMessageSound);
  const volume = usePreferences((s) => s.notifications.notificationVolume);
  useEffect(() => {
    if (!enabled) return;
    const onData = (
      _payload: Uint8Array,
      _participant: unknown,
      _kind: unknown,
      topic?: string,
    ) => {
      // Chat fires a "chat-refetch" hint on this topic whenever a remote
      // participant posts. We don't decode the payload — the topic is enough.
      if (topic === CHAT_DATA_TOPIC) {
        playTone(660, 120, (volume / 100) * 0.1);
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, enabled, volume]);
}

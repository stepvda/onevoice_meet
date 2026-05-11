import { useEffect, useRef } from "react";
import type { Room, RemoteParticipant } from "livekit-client";
import { RoomEvent } from "livekit-client";
import { usePreferences } from "./preferences";

const CHAT_DATA_TOPIC = "meet-chat";

function inDnd(start: string | null, end: string | null): boolean {
  if (!start || !end) return false;
  const [sh, sm] = start.split(":").map(Number);
  const [eh, em] = end.split(":").map(Number);
  if (Number.isNaN(sh) || Number.isNaN(sm) || Number.isNaN(eh) || Number.isNaN(em)) return false;
  const now = new Date();
  const cur = now.getHours() * 60 + now.getMinutes();
  const a = sh * 60 + sm;
  const b = eh * 60 + em;
  if (a === b) return false;
  return a < b ? cur >= a && cur < b : cur >= a || cur < b;
}

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

function playJoinTone(kind: "none" | "chime" | "ping" | "doorbell", gain: number) {
  if (kind === "none") return;
  if (kind === "ping") {
    playTone(1320, 90, gain);
    return;
  }
  if (kind === "doorbell") {
    playTone(880, 220, gain);
    setTimeout(() => playTone(660, 260, gain), 200);
    return;
  }
  // chime (default)
  playTone(880, 180, gain);
}

export function useJoinSound(room: Room) {
  const enabled = usePreferences((s) => s.notifications.soundOnJoin);
  const ignoreOwn = usePreferences((s) => s.notifications.ignoreOwnJoins);
  const volume = usePreferences((s) => s.notifications.notificationVolume);
  const joinSound = usePreferences((s) => s.notifications.joinSound);
  const dndStart = usePreferences((s) => s.notifications.doNotDisturbStart);
  const dndEnd = usePreferences((s) => s.notifications.doNotDisturbEnd);
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
      if (inDnd(dndStart, dndEnd)) return;
      playJoinTone(joinSound, (volume / 100) * 0.12);
    };
    room.on(RoomEvent.ParticipantConnected, onJoin);
    return () => {
      room.off(RoomEvent.ParticipantConnected, onJoin);
    };
  }, [room, enabled, ignoreOwn, volume, joinSound, dndStart, dndEnd]);
}

export function useChatSound(room: Room) {
  const enabled = usePreferences((s) => s.notifications.chatMessageSound);
  const volume = usePreferences((s) => s.notifications.notificationVolume);
  const dndStart = usePreferences((s) => s.notifications.doNotDisturbStart);
  const dndEnd = usePreferences((s) => s.notifications.doNotDisturbEnd);
  useEffect(() => {
    if (!enabled) return;
    const onData = (
      _payload: Uint8Array,
      _participant: unknown,
      _kind: unknown,
      topic?: string,
    ) => {
      if (topic !== CHAT_DATA_TOPIC) return;
      if (inDnd(dndStart, dndEnd)) return;
      playTone(660, 120, (volume / 100) * 0.1);
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, enabled, volume, dndStart, dndEnd]);
}

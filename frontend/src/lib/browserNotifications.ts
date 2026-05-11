import { useEffect, useRef } from "react";
import type { Room, RemoteParticipant } from "livekit-client";
import { RoomEvent } from "livekit-client";
import { usePreferences } from "./preferences";

/** True when "HH:mm" `start` ≤ now < `end` (wraps over midnight). */
function inDndWindow(start: string | null, end: string | null, now = new Date()): boolean {
  if (!start || !end) return false;
  const [sh, sm] = start.split(":").map(Number);
  const [eh, em] = end.split(":").map(Number);
  if (Number.isNaN(sh) || Number.isNaN(sm) || Number.isNaN(eh) || Number.isNaN(em)) return false;
  const cur = now.getHours() * 60 + now.getMinutes();
  const a = sh * 60 + sm;
  const b = eh * 60 + em;
  if (a === b) return false;
  return a < b ? cur >= a && cur < b : cur >= a || cur < b;
}

export function useIsInDnd(): () => boolean {
  const start = usePreferences((s) => s.notifications.doNotDisturbStart);
  const end = usePreferences((s) => s.notifications.doNotDisturbEnd);
  return () => inDndWindow(start, end);
}

let permissionRequested = false;
async function ensurePermission(): Promise<boolean> {
  if (typeof Notification === "undefined") return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  if (permissionRequested) return false;
  permissionRequested = true;
  try {
    const result = await Notification.requestPermission();
    return result === "granted";
  } catch {
    return false;
  }
}

function notify(title: string, body: string) {
  if (typeof Notification === "undefined") return;
  if (Notification.permission !== "granted") return;
  if (document.visibilityState === "visible") return;
  try {
    const n = new Notification(title, { body, tag: "meet.witysk" });
    window.setTimeout(() => n.close(), 5000);
  } catch {
    /* some browsers throw if focused or permission was revoked */
  }
}

const CHAT_DATA_TOPIC = "meet-chat";

export function useBrowserNotifications(room: Room) {
  const enabled = usePreferences((s) => s.notifications.browserNotificationOnJoin);
  const ignoreOwn = usePreferences((s) => s.notifications.ignoreOwnJoins);
  const dndStart = usePreferences((s) => s.notifications.doNotDisturbStart);
  const dndEnd = usePreferences((s) => s.notifications.doNotDisturbEnd);
  const ready = useRef(false);

  useEffect(() => {
    if (!enabled) return;
    void ensurePermission();
    ready.current = false;
    const t = window.setTimeout(() => {
      ready.current = true;
    }, 1500);
    return () => window.clearTimeout(t);
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const suppressed = () => inDndWindow(dndStart, dndEnd);
    const onJoin = (p: RemoteParticipant) => {
      if (!ready.current) return;
      if (ignoreOwn && p.isLocal) return;
      if (suppressed()) return;
      const who = p.name?.trim() || p.identity;
      notify("Joined the meeting", who);
    };
    const onData = (
      _payload: Uint8Array,
      _participant: unknown,
      _kind: unknown,
      topic?: string,
    ) => {
      if (topic !== CHAT_DATA_TOPIC) return;
      if (suppressed()) return;
      notify("New chat message", "Open the meeting to read it.");
    };
    room.on(RoomEvent.ParticipantConnected, onJoin);
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.ParticipantConnected, onJoin);
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, enabled, ignoreOwn, dndStart, dndEnd]);
}

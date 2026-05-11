import { useEffect, useRef } from "react";
import type { Room } from "livekit-client";
import { usePreferences } from "./preferences";

/**
 * Push-to-talk: while the configured key is held, the mic is unmuted; on
 * release it goes back to muted. We remember the mic's state at the moment
 * PTT was enabled and restore it when the user turns PTT off.
 */
export function usePushToTalk(room: Room) {
  const enabled = usePreferences((s) => s.av.pushToTalk);
  const key = usePreferences((s) => s.av.pushToTalkKey);
  const holdingRef = useRef(false);

  useEffect(() => {
    if (!enabled) return;
    // When PTT turns on, force mic off so the key actually gates speech.
    void room.localParticipant.setMicrophoneEnabled(false).catch(() => {});

    const matchesKey = (e: KeyboardEvent) => {
      // Accept "Space" by code; otherwise match e.key case-insensitively.
      if (key === "Space" || key === " ") return e.code === "Space";
      return e.key.toLowerCase() === key.toLowerCase();
    };
    const inEditable = (target: EventTarget | null) => {
      const el = target as HTMLElement | null;
      if (!el) return false;
      const tag = el.tagName;
      return (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        (el as HTMLElement).isContentEditable === true
      );
    };

    const down = (e: KeyboardEvent) => {
      if (!matchesKey(e)) return;
      if (inEditable(e.target)) return;
      if (e.repeat) return; // ignore key auto-repeat
      e.preventDefault();
      if (holdingRef.current) return;
      holdingRef.current = true;
      void room.localParticipant.setMicrophoneEnabled(true).catch(() => {});
    };
    const up = (e: KeyboardEvent) => {
      if (!matchesKey(e)) return;
      if (!holdingRef.current) return;
      e.preventDefault();
      holdingRef.current = false;
      void room.localParticipant.setMicrophoneEnabled(false).catch(() => {});
    };
    const blur = () => {
      if (!holdingRef.current) return;
      holdingRef.current = false;
      void room.localParticipant.setMicrophoneEnabled(false).catch(() => {});
    };

    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", blur);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", blur);
    };
  }, [room, enabled, key]);

  return enabled;
}

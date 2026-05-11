import { useEffect } from "react";
import type { Room } from "livekit-client";
import { usePreferences } from "./preferences";
import { useToggleHandRaise } from "./handRaise";

/**
 * Parses a binding string like "Ctrl+Shift+D" into a matcher and runs it
 * against a KeyboardEvent. Modifiers are case-insensitive; the key part is
 * matched against `event.key` (or `event.code` for "Space").
 *
 * Editable targets (inputs, textareas, contenteditable) bypass shortcuts so
 * typing letters doesn't accidentally mute the user.
 */
function parseBinding(binding: string): { ctrl: boolean; shift: boolean; alt: boolean; meta: boolean; key: string } | null {
  if (!binding) return null;
  const parts = binding.split("+").map((p) => p.trim()).filter(Boolean);
  if (parts.length === 0) return null;
  const out = { ctrl: false, shift: false, alt: false, meta: false, key: "" };
  for (const p of parts) {
    const lower = p.toLowerCase();
    if (lower === "ctrl" || lower === "control") out.ctrl = true;
    else if (lower === "shift") out.shift = true;
    else if (lower === "alt" || lower === "option") out.alt = true;
    else if (lower === "meta" || lower === "cmd" || lower === "command") out.meta = true;
    else out.key = p;
  }
  if (!out.key) return null;
  return out;
}

function matches(event: KeyboardEvent, b: ReturnType<typeof parseBinding>): boolean {
  if (!b) return false;
  if (event.ctrlKey !== b.ctrl) return false;
  if (event.shiftKey !== b.shift) return false;
  if (event.altKey !== b.alt) return false;
  if (event.metaKey !== b.meta) return false;
  if (b.key.toLowerCase() === "space") return event.code === "Space";
  return event.key.toLowerCase() === b.key.toLowerCase();
}

function isEditable(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

interface MeetingShortcutOptions {
  room: Room;
  onToggleScreenShare: () => void;
  onLeave: () => void;
  onOpenHelp: () => void;
}

export function useMeetingShortcuts({
  room,
  onToggleScreenShare,
  onLeave,
  onOpenHelp,
}: MeetingShortcutOptions) {
  const enabled = usePreferences((s) => s.keyboard.enableShortcuts);
  const muteToggleKey = usePreferences((s) => s.keyboard.muteToggleKey);
  const cameraToggleKey = usePreferences((s) => s.keyboard.cameraToggleKey);
  const handRaiseKey = usePreferences((s) => s.keyboard.handRaiseKey);
  const leaveMeetingKey = usePreferences((s) => s.keyboard.leaveMeetingKey);
  const screenshareKey = usePreferences((s) => s.keyboard.screenshareKey);
  const { toggle: toggleHand } = useToggleHandRaise();

  useEffect(() => {
    if (!enabled) return;
    const bindings = {
      mute: parseBinding(muteToggleKey),
      camera: parseBinding(cameraToggleKey),
      hand: parseBinding(handRaiseKey),
      leave: parseBinding(leaveMeetingKey),
      screen: parseBinding(screenshareKey),
    };

    const onKey = (e: KeyboardEvent) => {
      if (isEditable(e.target)) return;
      // "?" opens the overlay regardless of editable state (we already skipped
      // editable). Most users press Shift+/ for "?".
      if (e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        onOpenHelp();
        return;
      }
      if (matches(e, bindings.mute)) {
        e.preventDefault();
        const lp = room.localParticipant;
        void lp.setMicrophoneEnabled(!lp.isMicrophoneEnabled);
        return;
      }
      if (matches(e, bindings.camera)) {
        e.preventDefault();
        const lp = room.localParticipant;
        void lp.setCameraEnabled(!lp.isCameraEnabled);
        return;
      }
      if (matches(e, bindings.hand)) {
        e.preventDefault();
        void toggleHand();
        return;
      }
      if (matches(e, bindings.screen)) {
        e.preventDefault();
        onToggleScreenShare();
        return;
      }
      if (matches(e, bindings.leave)) {
        e.preventDefault();
        onLeave();
        return;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [
    enabled,
    room,
    muteToggleKey,
    cameraToggleKey,
    handRaiseKey,
    leaveMeetingKey,
    screenshareKey,
    toggleHand,
    onToggleScreenShare,
    onLeave,
    onOpenHelp,
  ]);
}

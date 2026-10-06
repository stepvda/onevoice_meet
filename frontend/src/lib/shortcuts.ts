import { useEffect } from "react";
import type { Room } from "livekit-client";
import { usePreferences } from "./preferences";
import { useToggleHandRaise } from "./handRaise";
import {
  backToLive as meetppBackToLive,
  selectTab as meetppSelectTab,
  stepViewed as meetppStepViewed,
  toggleFollow as meetppToggleFollow,
  useMeetpp,
} from "./meetpp/store";
import { meetppActions } from "./meetpp/session";
import { TABS as MEETPP_TABS } from "./meetpp/types";

/** Meet++ board shortcuts (FDD §5.8), listed in the ShortcutOverlay. */
export const MEETPP_SHORTCUTS = {
  tabs: "Alt+1…6",
  outline: "Alt+↑ / Alt+↓",
  backToLive: "Esc",
  next: "Ctrl+Shift+→",
  back: "Ctrl+Shift+←",
  follow: "Ctrl+Shift+A",
} as const;

function anyModalOpen(): boolean {
  return !!document.querySelector('[role="dialog"][aria-modal="true"], [data-testid="shortcut-overlay"]');
}

/** Handle a Meet++ shortcut; returns true when the key was consumed. */
function handleMeetppKey(e: KeyboardEvent): boolean {
  const mp = useMeetpp.getState();
  if (!mp.active || !mp.snap) return false;
  // Chair: Next / Back work whatever is on the stage.
  if (e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
    if (!mp.ctx.isChair) return false;
    void (e.key === "ArrowRight" ? meetppActions.next() : meetppActions.back()).catch(() => undefined);
    return true;
  }
  // The board's own keys only while this viewer sees the full board (as the
  // main stream or zoomed): Esc and Alt+digits stay free otherwise.
  if (!mp.boardOnStage) return false;
  // Alt+1…6 → tabs (e.code: Alt+digit types other characters on macOS).
  if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && /^Digit[1-6]$/.test(e.code)) {
    meetppSelectTab(MEETPP_TABS[Number(e.code.slice(5)) - 1], true);
    return true;
  }
  if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
    meetppStepViewed(e.key === "ArrowDown" ? 1 : -1);
    return true;
  }
  if (e.key === "Escape" && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    if (anyModalOpen()) return false;
    meetppBackToLive();
    return true;
  }
  if (e.ctrlKey && e.shiftKey && !e.altKey && (e.key === "A" || e.key === "a")) {
    meetppToggleFollow();
    return true;
  }
  return false;
}

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
      // Meet++ board: Alt+1…6 tabs, Alt+↑/↓ outline, Esc back to live,
      // chair Ctrl+Shift+→/← Next/Back, Ctrl+Shift+A follow on/off.
      if (handleMeetppKey(e)) {
        e.preventDefault();
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

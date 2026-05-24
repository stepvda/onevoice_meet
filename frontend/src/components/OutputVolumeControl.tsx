import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { Volume2, VolumeX } from "lucide-react";
import { RoomEvent } from "livekit-client";
import type { RemoteParticipant } from "livekit-client";
import { useRoomContext } from "@livekit/components-react";
import { usePreferences } from "../lib/preferences";

const SLIDER_HEIGHT = 150;

/**
 * Top-toolbar speaker button for the meeting output volume. Click opens a
 * vertical slider popover that anchors directly under the button — same
 * UX as the Café's `VolumeControl` but mirrored (the Café bar lives at
 * the bottom of the screen so it pops *up*; this one is in the top
 * toolbar so it pops *down*).
 *
 * The popover is rendered into `document.body` via a portal so it isn't
 * clipped by any flex/overflow ancestor in the toolbar chain. Volume is
 * applied to remote participants via LiveKit's `RemoteParticipant.setVolume`
 * AND directly to every `<audio>` element in the document — the second
 * write makes the change audible the same tick the slider moves even if
 * LiveKit hasn't propagated `setVolume` to its renderer yet.
 *
 * Persisted under `prefs.av.defaultVolume` (0–100) so the next meeting
 * starts at the same level.
 */
export default function OutputVolumeControl() {
  const { t } = useTranslation();
  const room = useRoomContext();
  const volume0to100 = usePreferences((s) => s.av.defaultVolume);
  const setAv = usePreferences((s) => s.setAv);
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const [popoverPos, setPopoverPos] = useState<{ left: number; top: number } | null>(null);

  // Apply the current volume both ways every time the slider moves.
  useEffect(() => {
    const v = Math.max(0, Math.min(1, volume0to100 / 100));
    const applyParticipant = (p: RemoteParticipant) => {
      try {
        p.setVolume(v);
      } catch {
        /* harmless — fires before any audio track exists */
      }
    };
    // Two-pronged silence at 0: `el.volume = 0` is what slider feedback
    // expects, but some browsers (and the LiveKit element factory) don't
    // honour volume=0 reliably right after element creation — set `muted`
    // as well so silence is guaranteed regardless. Mirrors the Café's
    // `el.muted = volumeRef.current === 0` pattern in `lib/tiCafe.tsx`.
    const applyToAudio = (el: HTMLAudioElement) => {
      el.volume = v;
      el.muted = v === 0;
    };
    const applyAudioElements = () => {
      document.querySelectorAll<HTMLAudioElement>("audio").forEach(applyToAudio);
    };

    room.remoteParticipants.forEach(applyParticipant);
    applyAudioElements();

    // Re-apply for participants who join after the slider moved, and for
    // any audio track that gets subscribed (RoomAudioRenderer creates a
    // new <audio> element each time a remote track is subscribed).
    const onConn = (p: RemoteParticipant) => applyParticipant(p);
    const onSubOrPub = () => applyAudioElements();
    room.on(RoomEvent.ParticipantConnected, onConn);
    room.on(RoomEvent.TrackSubscribed, onSubOrPub);
    room.on(RoomEvent.TrackPublished, onSubOrPub);

    // The TrackSubscribed listener above is necessary but not sufficient:
    // @livekit/components-react's RoomAudioRenderer creates the <audio>
    // element in a React render cycle that runs AFTER the event fires,
    // so `applyAudioElements()` finds nothing and the freshly-created
    // element keeps the browser default volume (1.0). That's the bug
    // where joining a new meeting with a remembered 0% slider still
    // played audio. MutationObserver catches any <audio> element added
    // to the DOM regardless of when, and applies the current volume +
    // muted state to it.
    const observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        m.addedNodes.forEach((n) => {
          if (n instanceof HTMLAudioElement) {
            applyToAudio(n);
          } else if (n instanceof HTMLElement) {
            n.querySelectorAll<HTMLAudioElement>("audio").forEach(applyToAudio);
          }
        });
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    return () => {
      observer.disconnect();
      room.off(RoomEvent.ParticipantConnected, onConn);
      room.off(RoomEvent.TrackSubscribed, onSubOrPub);
      room.off(RoomEvent.TrackPublished, onSubOrPub);
    };
  }, [room, volume0to100]);

  // Compute popover position when it opens, and keep it pinned to the
  // button on viewport resize / scroll.
  useLayoutEffect(() => {
    if (!open) return;
    const reposition = () => {
      const btn = buttonRef.current;
      const pop = popoverRef.current;
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      const popW = pop?.offsetWidth ?? 56;
      // Slider + ~30px of chrome (padding + percentage label); used before
      // the popover is measured to avoid a single off-screen first paint.
      const popH = pop?.offsetHeight ?? (SLIDER_HEIGHT + 30);
      // Horizontally: centred under the button, clamped to the viewport.
      const left = Math.max(8, Math.min(window.innerWidth - popW - 8, rect.left + rect.width / 2 - popW / 2));
      // Vertically: prefer below the button. If that would render outside
      // the viewport (e.g. button sits near the bottom of a small iframe
      // viewport in PublicView's embed mode), flip and render above.
      const spaceBelow = window.innerHeight - rect.bottom;
      const top = spaceBelow >= popH + 8
        ? rect.bottom + 8
        : Math.max(8, rect.top - popH - 8);
      setPopoverPos({ left, top });
    };
    reposition();
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open]);

  // Close on outside click / Esc.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (buttonRef.current?.contains(t) || popoverRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const muted = volume0to100 === 0;
  const Icon = muted ? VolumeX : Volume2;
  const label = muted
    ? t("room.unmuteOutput", { defaultValue: "Unmute speakers" })
    : t("room.outputVolume", { defaultValue: "Output volume" });

  const popover =
    open && popoverPos
      ? createPortal(
          <div
            ref={popoverRef}
            role="dialog"
            aria-label={t("room.outputVolumeSlider", { defaultValue: "Output volume slider" })}
            data-testid="output-volume-popover"
            style={{ position: "fixed", left: popoverPos.left, top: popoverPos.top, zIndex: 80 }}
            className="px-3 py-3 rounded-lg bg-primary-900 border border-primary-700 shadow-xl flex flex-col items-center gap-2"
          >
            <input
              type="range"
              min={0}
              max={100}
              step={1}
              value={volume0to100}
              onChange={(e) => setAv({ defaultVolume: Number(e.target.value) })}
              data-testid="output-volume-slider"
              aria-label={t("room.outputVolumeSlider", { defaultValue: "Output volume slider" })}
              aria-orientation="vertical"
              style={{
                writingMode: "vertical-lr" as unknown as undefined,
                direction: "rtl",
                width: 24,
                height: SLIDER_HEIGHT,
              }}
            />
            <div className="text-[10px] font-medium text-slate-400 tabular-nums">
              {volume0to100}%
            </div>
          </div>,
          document.body,
        )
      : null;

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="btn-output-volume"
        title={label}
        aria-haspopup="dialog"
        aria-expanded={open ? "true" : "false"}
        aria-label={label}
        className={[
          "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
          muted
            ? "bg-primary-900/60 text-slate-400 hover:bg-primary-800"
            : "bg-primary-700 text-slate-100 hover:bg-primary-600",
        ].join(" ")}
      >
        <Icon size={16} />
      </button>
      {popover}
    </>
  );
}

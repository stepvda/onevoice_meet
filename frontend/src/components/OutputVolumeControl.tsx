import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Volume2, VolumeX } from "lucide-react";
import { RoomEvent } from "livekit-client";
import type { RemoteParticipant } from "livekit-client";
import { useRoomContext } from "@livekit/components-react";
import { usePreferences } from "../lib/preferences";

const SLIDER_HEIGHT = 150;

/**
 * Top-toolbar speaker button for the meeting output volume. Click opens a
 * vertical slider popover (top = 100 %, bottom = muted) — same UX as the
 * Café's `VolumeControl`. The chosen value is applied to every
 * RemoteParticipant in the room via `setVolume(0..1)` and re-applied to
 * anyone who joins after the user picks a value.
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
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  // Apply the current volume to every connected remote participant. Re-runs
  // whenever the slider moves OR a new participant connects (we add the
  // listener inside this effect, so the live `volume0to100` is captured).
  useEffect(() => {
    const v = Math.max(0, Math.min(1, volume0to100 / 100));
    const apply = (p: RemoteParticipant) => {
      try {
        p.setVolume(v);
      } catch {
        /* setVolume can throw if the participant has no audio tracks yet —
           harmless; LiveKit applies the default on first audio track. */
      }
    };
    room.remoteParticipants.forEach(apply);
    const onConn = (p: RemoteParticipant) => apply(p);
    room.on(RoomEvent.ParticipantConnected, onConn);
    return () => {
      room.off(RoomEvent.ParticipantConnected, onConn);
    };
  }, [room, volume0to100]);

  // Close on outside click / Esc.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrapperRef.current) return;
      if (!wrapperRef.current.contains(e.target as Node)) setOpen(false);
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
    : t("room.muteOutput", { defaultValue: "Output volume" });

  return (
    <div ref={wrapperRef} className="relative inline-block">
      <button
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

      {open && (
        <div
          role="dialog"
          aria-label={t("room.outputVolumeSlider", { defaultValue: "Output volume slider" })}
          data-testid="output-volume-popover"
          className="absolute z-30 left-1/2 -translate-x-1/2 top-full mt-2 px-3 py-3 rounded-lg bg-primary-900 border border-primary-700 shadow-xl flex flex-col items-center gap-2"
        >
          {/* Native vertical range — same trick as the Café slider so we
              inherit keyboard + screen-reader behaviour for free. */}
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
        </div>
      )}
    </div>
  );
}

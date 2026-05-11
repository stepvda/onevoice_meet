import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, Headphones } from "lucide-react";
import { useRoomContext } from "@livekit/components-react";
import { usePreferences } from "../lib/preferences";

interface DeviceInfo {
  deviceId: string;
  label: string;
}

interface DevicesByKind {
  mic: DeviceInfo[];
  cam: DeviceInfo[];
  speaker: DeviceInfo[];
}

const EMPTY: DevicesByKind = { mic: [], cam: [], speaker: [] };

/**
 * Lists available media devices and lets the user switch the live mic /
 * camera / speaker mid-meeting. LiveKit's `switchActiveDevice` reacquires
 * the underlying MediaStream with the new device id without a reconnect.
 * The selection is also persisted to `prefs.av.preferred*Id` so a re-join
 * picks the same device.
 */
export default function DeviceSwitcher() {
  const { t } = useTranslation();
  const room = useRoomContext();
  const prefs = usePreferences();
  const [open, setOpen] = useState(false);
  const [devices, setDevices] = useState<DevicesByKind>(EMPTY);
  const [current, setCurrent] = useState({
    mic: prefs.av.preferredMicId ?? "",
    cam: prefs.av.preferredCameraId ?? "",
    speaker: prefs.av.preferredSpeakerId ?? "",
  });
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // Outside-click / Escape to close.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      if (!navigator.mediaDevices?.enumerateDevices) return;
      const list = await navigator.mediaDevices.enumerateDevices();
      if (cancelled) return;
      const next: DevicesByKind = { mic: [], cam: [], speaker: [] };
      for (const d of list) {
        const info: DeviceInfo = { deviceId: d.deviceId, label: d.label || d.deviceId.slice(0, 8) };
        if (d.kind === "audioinput") next.mic.push(info);
        else if (d.kind === "videoinput") next.cam.push(info);
        else if (d.kind === "audiooutput") next.speaker.push(info);
      }
      setDevices(next);
    };
    void refresh();
    navigator.mediaDevices?.addEventListener?.("devicechange", refresh);
    return () => {
      cancelled = true;
      navigator.mediaDevices?.removeEventListener?.("devicechange", refresh);
    };
  }, [open]);

  async function pick(kind: "mic" | "cam" | "speaker", deviceId: string) {
    setCurrent((cur) => ({ ...cur, [kind]: deviceId }));
    try {
      if (kind === "mic") {
        await room.switchActiveDevice("audioinput", deviceId);
        prefs.setAv({ preferredMicId: deviceId });
      } else if (kind === "cam") {
        await room.switchActiveDevice("videoinput", deviceId);
        prefs.setAv({ preferredCameraId: deviceId });
      } else {
        // setSinkId() — speaker selection only works on Chromium and some
        // newer Safari. We try `room.switchActiveDevice('audiooutput')`
        // first (since LiveKit fans this out to all attached audio
        // elements), and silently fall back to no-op elsewhere.
        try {
          await room.switchActiveDevice("audiooutput", deviceId);
        } catch {
          /* ignore unsupported */
        }
        prefs.setAv({ preferredSpeakerId: deviceId });
      }
    } catch {
      /* user denied or device disappeared — leave selection unchanged */
    }
  }

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="btn-devices"
        aria-haspopup="menu"
        aria-expanded={open ? "true" : "false"}
        aria-label={t("devices.toolbar", { defaultValue: "Audio & video devices" })}
        title={t("devices.toolbarTitle", { defaultValue: "Choose microphone, camera and speaker" })}
        className={[
          "inline-flex items-center justify-center px-2 py-1.5 rounded-lg text-sm font-medium",
          open
            ? "bg-primary-500 text-white"
            : "bg-primary-700 text-slate-100 hover:bg-primary-600",
        ].join(" ")}
      >
        <Headphones size={16} />
      </button>
      {open && (
        <div
          role="menu"
          data-testid="devices-popover"
          className="absolute top-full right-0 mt-1 z-40 w-72 bg-primary-900 border border-primary-700 rounded-lg shadow-lg p-2 text-sm max-h-[60vh] overflow-y-auto"
        >
          <DeviceGroup
            title={t("devices.mic", { defaultValue: "Microphone" })}
            items={devices.mic}
            currentId={current.mic}
            onPick={(id) => void pick("mic", id)}
            emptyLabel={t("devices.none", { defaultValue: "No devices found" })}
          />
          <DeviceGroup
            title={t("devices.camera", { defaultValue: "Camera" })}
            items={devices.cam}
            currentId={current.cam}
            onPick={(id) => void pick("cam", id)}
            emptyLabel={t("devices.none", { defaultValue: "No devices found" })}
          />
          <DeviceGroup
            title={t("devices.speaker", { defaultValue: "Speaker" })}
            items={devices.speaker}
            currentId={current.speaker}
            onPick={(id) => void pick("speaker", id)}
            emptyLabel={t("devices.speakerUnsupported", { defaultValue: "Output device selection isn't supported in this browser." })}
          />
        </div>
      )}
    </div>
  );
}

function DeviceGroup({
  title,
  items,
  currentId,
  onPick,
  emptyLabel,
}: {
  title: string;
  items: DeviceInfo[];
  currentId: string;
  onPick: (id: string) => void;
  emptyLabel: string;
}) {
  return (
    <div className="mb-1">
      <div className="text-xs uppercase tracking-wide text-slate-400 px-2 py-1">{title}</div>
      {items.length === 0 && (
        <div className="text-xs text-slate-500 px-2 py-1">{emptyLabel}</div>
      )}
      {items.map((d) => (
        <button
          key={d.deviceId || d.label}
          type="button"
          onClick={() => onPick(d.deviceId)}
          className="w-full text-left flex items-center gap-2 px-2 py-1.5 rounded hover:bg-primary-800 text-slate-200"
        >
          <Check
            size={14}
            className={currentId === d.deviceId ? "text-accent-500" : "text-transparent"}
          />
          <span className="truncate">{d.label}</span>
        </button>
      ))}
    </div>
  );
}

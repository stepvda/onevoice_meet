import { useEffect, useState } from "react";
import { useLocalParticipant } from "@livekit/components-react";
import { BackgroundBlur, VirtualBackground } from "@livekit/track-processors";
import { LocalVideoTrack, Track } from "livekit-client";
import { ImageOff } from "lucide-react";

const PRESETS = [
  { key: "off", label: "Off" },
  { key: "blur", label: "Blur" },
  { key: "/backgrounds/office.jpg", label: "Office" },
  { key: "/backgrounds/park.jpg", label: "Park" },
] as const;

function getCameraTrack(local: ReturnType<typeof useLocalParticipant>["localParticipant"]): LocalVideoTrack | null {
  if (!local) return null;
  const pub = local.getTrackPublication(Track.Source.Camera);
  if (!pub || pub.kind !== Track.Kind.Video) return null;
  const t = pub.track as LocalVideoTrack | undefined;
  return t ?? null;
}

export default function BackgroundPicker() {
  const { localParticipant, cameraTrack } = useLocalParticipant();
  const [active, setActive] = useState<string>("off");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Re-apply the active processor whenever the camera track changes (e.g. user
  // toggles camera off then on again, or switches device).
  useEffect(() => {
    if (active === "off") return;
    const track = getCameraTrack(localParticipant);
    if (!track) return;
    let cancelled = false;
    (async () => {
      try {
        if (active === "blur") await track.setProcessor(BackgroundBlur(10));
        else await track.setProcessor(VirtualBackground(active));
      } catch (e) {
        if (!cancelled) setErr((e as Error).message || "processor failed");
      }
    })();
    return () => {
      cancelled = true;
    };
    // cameraTrack changes when LiveKit replaces the underlying track.
  }, [cameraTrack, active]); // eslint-disable-line react-hooks/exhaustive-deps

  const cameraPresent = !!cameraTrack || !!getCameraTrack(localParticipant);

  async function apply(key: string) {
    setErr(null);
    const track = getCameraTrack(localParticipant);
    if (!track) {
      setErr("Turn on your camera first.");
      return;
    }
    setBusy(true);
    try {
      if (key === "off") {
        await track.stopProcessor();
      } else if (key === "blur") {
        await track.setProcessor(BackgroundBlur(10));
      } else {
        await track.setProcessor(VirtualBackground(key));
      }
      setActive(key);
    } catch (e) {
      setErr((e as Error).message || "Background effects unsupported on this device.");
      setActive("off");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="inline-flex items-center gap-2">
      <select
        value={active}
        onChange={(e) => apply(e.target.value)}
        disabled={!cameraPresent || busy}
        data-testid="background-picker"
        className={[
          "px-2 py-1.5 text-sm rounded-lg border",
          "bg-primary-900/60 text-slate-100 border-primary-700",
          "focus:outline-none focus:ring-2 focus:ring-primary-500",
          "disabled:opacity-50 disabled:cursor-not-allowed",
        ].join(" ")}
        title={cameraPresent ? "Background effect" : "Turn on the camera to use backgrounds"}
      >
        {PRESETS.map((p) => (
          <option key={p.key} value={p.key}>
            {p.label}
          </option>
        ))}
      </select>
      {err && (
        <span className="inline-flex items-center gap-1 text-xs text-amber-400" title={err}>
          <ImageOff size={14} /> {err.length > 24 ? err.slice(0, 24) + "…" : err}
        </span>
      )}
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { useLocalParticipant } from "@livekit/components-react";
import { BackgroundBlur, VirtualBackground } from "@livekit/track-processors";
import { LocalVideoTrack, Track } from "livekit-client";
import { ImageOff, Upload } from "lucide-react";

/**
 * Background effect options. "Virtual backgrounds" use the MediaPipe selfie
 * segmenter to mask the foreground (participant) and draw the chosen image
 * behind it. "Blur" uses the same mask to blur everything except the person.
 *
 * User-uploaded images live only for the current tab session — the File is
 * kept in a blob: URL, nothing is sent to the server.
 */

const BUILTIN_PRESETS = [
  { key: "off", label: "Off" },
  { key: "blur", label: "Blur" },
  { key: "/backgrounds/office.jpg", label: "Office" },
  { key: "/backgrounds/park.jpg", label: "Park" },
  { key: "/backgrounds/isometric.jpg", label: "3D cubes" },
  { key: "/backgrounds/stacked.jpg", label: "Stacked cubes" },
  { key: "/backgrounds/cubist.jpg", label: "Cubist" },
  { key: "/backgrounds/orbs.jpg", label: "Orbs" },
  { key: "/backgrounds/waves.jpg", label: "Waves" },
] as const;

const MAX_CUSTOM_BYTES = 4 * 1024 * 1024; // 4 MB

function getCameraTrack(local: ReturnType<typeof useLocalParticipant>["localParticipant"]): LocalVideoTrack | null {
  if (!local) return null;
  const pub = local.getTrackPublication(Track.Source.Camera);
  if (!pub || pub.kind !== Track.Kind.Video) return null;
  return (pub.track as LocalVideoTrack | undefined) ?? null;
}

export default function BackgroundPicker() {
  const { localParticipant, cameraTrack } = useLocalParticipant();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const customUrlRef = useRef<string | null>(null);
  const [customLabel, setCustomLabel] = useState<string | null>(null);
  const [active, setActive] = useState<string>("off");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Re-apply the active processor whenever the camera track changes.
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
  }, [cameraTrack, active]); // eslint-disable-line react-hooks/exhaustive-deps

  // Release the blob URL when the component unmounts (avoid leak).
  useEffect(() => {
    return () => {
      if (customUrlRef.current) {
        URL.revokeObjectURL(customUrlRef.current);
        customUrlRef.current = null;
      }
    };
  }, []);

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

  function pickCustom(file: File | null) {
    if (!file) return;
    if (!/^image\//.test(file.type)) {
      setErr(`Not an image (${file.type || "unknown"})`);
      return;
    }
    if (file.size > MAX_CUSTOM_BYTES) {
      setErr(`Image is ${(file.size / 1_048_576).toFixed(1)} MB; max is 4 MB.`);
      return;
    }
    // Revoke any previous custom URL.
    if (customUrlRef.current) URL.revokeObjectURL(customUrlRef.current);
    const url = URL.createObjectURL(file);
    customUrlRef.current = url;
    setCustomLabel(file.name.length > 24 ? file.name.slice(0, 22) + "…" : file.name);
    void apply(url);
  }

  const options: { key: string; label: string }[] = [
    ...BUILTIN_PRESETS.map((p) => ({ key: p.key, label: p.label })),
  ];
  if (customUrlRef.current) {
    options.push({ key: customUrlRef.current, label: customLabel ? `Custom: ${customLabel}` : "Custom image" });
  }

  return (
    <div className="inline-flex items-center gap-2 flex-wrap">
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
        aria-label="Background effect"
      >
        {options.map((p) => (
          <option key={p.key} value={p.key}>
            {p.label}
          </option>
        ))}
      </select>

      {/* Custom upload */}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        aria-label="Upload a background image"
        title="Upload a background image"
        data-testid="background-upload-input"
        onChange={(e) => pickCustom(e.target.files?.[0] ?? null)}
        className="hidden"
      />
      <button
        type="button"
        onClick={() => fileInputRef.current?.click()}
        disabled={!cameraPresent || busy}
        data-testid="background-upload-btn"
        className={[
          "inline-flex items-center gap-1 px-2 py-1.5 text-sm rounded-lg border",
          "bg-primary-900/60 text-slate-200 border-primary-700 hover:bg-primary-800",
          "disabled:opacity-50 disabled:cursor-not-allowed",
        ].join(" ")}
        title="Upload your own background image"
      >
        <Upload size={14} />
        Upload
      </button>

      {err && (
        <span className="inline-flex items-center gap-1 text-xs text-amber-400" title={err}>
          <ImageOff size={14} /> {err.length > 36 ? err.slice(0, 36) + "…" : err}
        </span>
      )}
    </div>
  );
}

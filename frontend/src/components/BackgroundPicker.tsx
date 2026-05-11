import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { useLocalParticipant } from "@livekit/components-react";
import { BackgroundBlur, VirtualBackground } from "@livekit/track-processors";
import { LocalVideoTrack, Track } from "livekit-client";
import { Check, ImageOff, Sparkles, Upload, Wand2 } from "lucide-react";
import {
  ANIMATED_BACKGROUNDS,
  driveAnimatedBackground,
  getAnimatedById,
  type AnimatedBackgroundDef,
} from "../lib/animatedBackgrounds";

/**
 * Background effect picker.
 *
 * Three kinds of presets:
 *   - "off" / "blur" — meta options, no image.
 *   - "/backgrounds/<file>.jpg" — static image driven by the LiveKit
 *     `VirtualBackground` processor.
 *   - "anim:<id>" — original canvas-rendered animation; we render frames
 *     locally and swap `processor.transformer.backgroundImage` at ~15fps
 *     (see lib/animatedBackgrounds.ts).
 *
 * The dropdown is a portal-rendered popover so it isn't clipped by the
 * meeting stage's stacking context.
 */

const STATIC_PRESETS = [
  { key: "/backgrounds/office.jpg", i18nKey: "background.office" },
  { key: "/backgrounds/park.jpg", i18nKey: "background.park" },
  { key: "/backgrounds/isometric.jpg", i18nKey: "background.isometric" },
  { key: "/backgrounds/stacked.jpg", i18nKey: "background.stacked" },
  { key: "/backgrounds/cubist.jpg", i18nKey: "background.cubist" },
  { key: "/backgrounds/orbs.jpg", i18nKey: "background.orbs" },
  { key: "/backgrounds/waves.jpg", i18nKey: "background.waves" },
] as const;

const MAX_CUSTOM_BYTES = 4 * 1024 * 1024;

// 16x16 transparent PNG so the VirtualBackground processor has a tiny
// stand-in to load while we override `backgroundImage` from the rAF loop.
const PLACEHOLDER_BG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQAQMAAAAlPW0iAAAABlBMVEUAAAD///+l2Z/dAAAAEUlEQVR42mNgAANqANgABRsAAAB6+1xWAAAAAElFTkSuQmCC";

function getCameraTrack(
  local: ReturnType<typeof useLocalParticipant>["localParticipant"],
): LocalVideoTrack | null {
  if (!local) return null;
  const pub = local.getTrackPublication(Track.Source.Camera);
  if (!pub || pub.kind !== Track.Kind.Video) return null;
  return (pub.track as LocalVideoTrack | undefined) ?? null;
}

export default function BackgroundPicker() {
  const { t } = useTranslation();
  const { localParticipant, cameraTrack } = useLocalParticipant();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const customUrlRef = useRef<string | null>(null);
  const [customLabel, setCustomLabel] = useState<string | null>(null);
  const [active, setActive] = useState<string>("off");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);

  // Handle of the running animation loop (if any). We stop it before
  // applying a different background.
  const animHandleRef = useRef<{ stop: () => void } | null>(null);

  // Re-apply the active processor whenever the camera track changes (the
  // track reference is replaced when the user switches camera devices).
  useEffect(() => {
    if (active === "off") return;
    const track = getCameraTrack(localParticipant);
    if (!track) return;
    let cancelled = false;
    (async () => {
      try {
        await applyToTrack(track, active);
      } catch (e) {
        if (!cancelled) setErr((e as Error).message || "processor failed");
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraTrack]);

  // Release the blob URL + stop any animation on unmount.
  useEffect(() => {
    return () => {
      animHandleRef.current?.stop();
      if (customUrlRef.current) {
        URL.revokeObjectURL(customUrlRef.current);
        customUrlRef.current = null;
      }
    };
  }, []);

  function reposition() {
    const btn = buttonRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    setAnchor({ top: r.bottom + 4, right: window.innerWidth - r.right });
  }

  useLayoutEffect(() => {
    if (!open) return;
    reposition();
    const onScroll = () => reposition();
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", reposition);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      const tgt = e.target as Node;
      if (buttonRef.current?.contains(tgt)) return;
      if (popRef.current?.contains(tgt)) return;
      setOpen(false);
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

  const cameraPresent = !!cameraTrack || !!getCameraTrack(localParticipant);

  async function applyToTrack(track: LocalVideoTrack, key: string) {
    // Stop any previous animation before changing processor.
    animHandleRef.current?.stop();
    animHandleRef.current = null;
    if (key === "off") {
      await track.stopProcessor();
      return;
    }
    if (key === "blur") {
      await track.setProcessor(BackgroundBlur(10));
      return;
    }
    if (key.startsWith("anim:")) {
      const def = getAnimatedById(key);
      if (!def) throw new Error("unknown animation: " + key);
      // Use a placeholder image while the processor initialises; the rAF
      // loop will start swapping in real frames as soon as the processor is
      // attached.
      const processor = VirtualBackground(PLACEHOLDER_BG);
      await track.setProcessor(processor);
      animHandleRef.current = driveAnimatedBackground(
        processor as unknown as { transformer: { backgroundImage: ImageBitmap | null } },
        def,
      );
      return;
    }
    await track.setProcessor(VirtualBackground(key));
  }

  async function apply(key: string) {
    setErr(null);
    setOpen(false);
    const track = getCameraTrack(localParticipant);
    if (!track) {
      setErr(t("background.needCameraFirst"));
      return;
    }
    setBusy(true);
    try {
      await applyToTrack(track, key);
      setActive(key);
    } catch (e) {
      setErr((e as Error).message || t("background.processorFailed"));
      setActive("off");
    } finally {
      setBusy(false);
    }
  }

  function pickCustom(file: File | null) {
    if (!file) return;
    if (!/^image\//.test(file.type)) {
      setErr(t("background.notImage", { type: file.type || "unknown" }));
      return;
    }
    if (file.size > MAX_CUSTOM_BYTES) {
      setErr(t("background.tooLarge", { mb: (file.size / 1_048_576).toFixed(1) }));
      return;
    }
    if (customUrlRef.current) URL.revokeObjectURL(customUrlRef.current);
    const url = URL.createObjectURL(file);
    customUrlRef.current = url;
    setCustomLabel(file.name.length > 24 ? file.name.slice(0, 22) + "…" : file.name);
    void apply(url);
  }

  const customUrl = customUrlRef.current;
  const activeLabel = labelForActive(t, active, customLabel);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={!cameraPresent || busy}
        data-testid="background-picker"
        aria-haspopup="dialog"
        aria-expanded={open ? "true" : "false"}
        title={cameraPresent ? t("background.label") : t("background.needCamera")}
        aria-label={t("background.label")}
        className={[
          "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
          open ? "bg-primary-500 text-white" : "bg-primary-700 text-slate-100 hover:bg-primary-600",
          "disabled:opacity-50 disabled:cursor-not-allowed",
        ].join(" ")}
      >
        <Sparkles size={16} />
        <span className="hidden md:inline truncate max-w-[140px]">{activeLabel}</span>
      </button>

      {err && (
        <span className="inline-flex items-center gap-1 text-xs text-amber-400 ml-2" title={err}>
          <ImageOff size={14} /> {err.length > 36 ? err.slice(0, 36) + "…" : err}
        </span>
      )}

      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        aria-label={t("background.uploadAria")}
        title={t("background.uploadAria")}
        data-testid="background-upload-input"
        onChange={(e) => pickCustom(e.target.files?.[0] ?? null)}
        className="hidden"
      />

      {open && anchor && typeof document !== "undefined" &&
        createPortal(
          <div
            ref={popRef}
            data-testid="background-popover"
            className="fixed z-[1000] w-[320px] max-h-[70vh] overflow-y-auto rounded-xl bg-primary-900 border border-primary-700 shadow-xl p-2"
            style={{ top: anchor.top, right: anchor.right }}
          >
            <div className="grid grid-cols-3 gap-2">
              <MetaTile
                label={t("background.off")}
                active={active === "off"}
                onClick={() => void apply("off")}
                kind="off"
              />
              <MetaTile
                label={t("background.blur")}
                active={active === "blur"}
                onClick={() => void apply("blur")}
                kind="blur"
              />
              {customUrl && (
                <ImageTile
                  label={customLabel ?? t("background.customDefault")}
                  src={customUrl}
                  active={active === customUrl}
                  onClick={() => void apply(customUrl)}
                />
              )}
              <UploadTile
                label={t("background.upload")}
                onClick={() => fileInputRef.current?.click()}
              />
            </div>

            <div className="text-xs uppercase tracking-wide text-slate-400 mt-3 mb-1 px-1 flex items-center gap-1">
              <Wand2 size={11} />
              {t("background.animatedHeading", { defaultValue: "Animated" })}
            </div>
            <div className="grid grid-cols-3 gap-2">
              {ANIMATED_BACKGROUNDS.map((def) => (
                <AnimatedTile
                  key={def.id}
                  def={def}
                  label={t(def.i18nKey)}
                  active={active === def.id}
                  onClick={() => void apply(def.id)}
                />
              ))}
            </div>

            <div className="text-xs uppercase tracking-wide text-slate-400 mt-3 mb-1 px-1">
              {t("background.staticHeading", { defaultValue: "Photos" })}
            </div>
            <div className="grid grid-cols-3 gap-2">
              {STATIC_PRESETS.map((p) => (
                <ImageTile
                  key={p.key}
                  label={t(p.i18nKey)}
                  src={p.key}
                  active={active === p.key}
                  onClick={() => void apply(p.key)}
                />
              ))}
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}

function labelForActive(
  t: ReturnType<typeof useTranslation>["t"],
  active: string,
  customLabel: string | null,
): string {
  if (active === "off") return t("background.off");
  if (active === "blur") return t("background.blur");
  if (active.startsWith("anim:")) {
    const d = getAnimatedById(active);
    return d ? t(d.i18nKey) : t("background.label");
  }
  if (active.startsWith("blob:")) return customLabel ?? t("background.customDefault");
  const preset = STATIC_PRESETS.find((p) => p.key === active);
  return preset ? t(preset.i18nKey) : t("background.label");
}

interface TileProps {
  label: string;
  active: boolean;
  onClick: () => void;
}

function MetaTile({ label, active, onClick, kind }: TileProps & { kind: "off" | "blur" }) {
  return (
    <Tile label={label} active={active} onClick={onClick}>
      <div className="absolute inset-0 flex items-center justify-center">
        {kind === "off" ? (
          <span className="text-xs text-slate-400 font-medium">—</span>
        ) : (
          <span className="text-xs text-slate-200 font-medium tracking-wide" style={{ filter: "blur(2px)" }}>
            blur
          </span>
        )}
      </div>
    </Tile>
  );
}

function ImageTile({ src, label, active, onClick }: TileProps & { src: string }) {
  return (
    <Tile label={label} active={active} onClick={onClick}>
      <img src={src} alt="" className="absolute inset-0 w-full h-full object-cover" loading="lazy" />
    </Tile>
  );
}

function UploadTile({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="aspect-video relative rounded-lg overflow-hidden border border-dashed border-primary-600 hover:border-accent-500 hover:bg-primary-800 flex items-center justify-center text-slate-300"
      data-testid="background-upload-tile"
      title={label}
    >
      <span className="flex flex-col items-center gap-1 text-[10px]">
        <Upload size={16} /> {label}
      </span>
    </button>
  );
}

function AnimatedTile({
  def,
  label,
  active,
  onClick,
}: TileProps & { def: AnimatedBackgroundDef }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // Run a small rAF loop for the duration the tile is mounted (the picker
  // popover). 12fps is plenty for a preview.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let stopped = false;
    let last = 0;
    const start = performance.now();
    const frame = () => {
      if (stopped) return;
      const now = performance.now();
      if (now - last >= 1000 / 12) {
        last = now;
        def.draw(ctx, (now - start) / 1000, canvas.width, canvas.height);
      }
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
    return () => {
      stopped = true;
    };
  }, [def]);
  return (
    <Tile label={label} active={active} onClick={onClick}>
      <canvas
        ref={canvasRef}
        width={160}
        height={90}
        className="absolute inset-0 w-full h-full object-cover"
      />
    </Tile>
  );
}

function Tile({
  label,
  active,
  onClick,
  children,
}: TileProps & { children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={`bg-tile-${label}`}
      title={label}
      className={[
        "aspect-video relative rounded-lg overflow-hidden border bg-primary-800 hover:ring-2 hover:ring-accent-500 transition",
        active ? "border-accent-500 ring-2 ring-accent-500" : "border-primary-700",
      ].join(" ")}
    >
      {children}
      <div className="absolute bottom-0 inset-x-0 bg-gradient-to-t from-black/80 to-transparent px-1.5 py-1 text-[10px] text-white truncate">
        {label}
      </div>
      {active && (
        <div className="absolute top-1 right-1 bg-accent-500 text-white rounded-full p-0.5">
          <Check size={10} />
        </div>
      )}
    </button>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useEnsureTrackRef, type TrackReference } from "@livekit/components-react";
import { Track } from "livekit-client";
import { usePreferences } from "../lib/preferences";
import { CADENCE, statsCollector } from "../lib/cq/connectionStats";
import { useCqStore } from "../lib/cq/connectionQualityStore";
import { reapplyVideoOverride } from "../lib/cq/connectionActions";
import type { CqBand } from "../lib/cq/types";
import ConnectionQualityOverlay from "./ConnectionQualityOverlay";

const BAR_COLORS: Record<CqBand | "idle", string> = {
  high: "#10b981",
  medium: "#f59e0b",
  low: "#ef4444",
  idle: "#94a3b8",
};

function barsFor(band: CqBand | "idle"): number[] {
  if (band === "high") return [4, 7, 10, 13];
  if (band === "medium") return [4, 7, 10, 4];
  if (band === "low") return [4, 7, 3, 3];
  return [3, 5, 7, 9];
}

export default function ConnectionQualityButton() {
  const ensured = useEnsureTrackRef();
  const ref = ensured?.publication ? (ensured as TrackReference) : undefined;
  const { t } = useTranslation();
  const show = usePreferences((s) => s.display.showConnectionQuality);
  const trackKey = ref ? `${ref.participant.identity}-${ref.source ?? ""}` : "";
  const band = useCqStore((s) => (trackKey ? s.verdicts[trackKey]?.band : undefined)) ?? "idle";
  const lost = useCqStore((s) => (trackKey ? s.verdicts[trackKey]?.lost : false)) ?? false;
  const isOpen = useCqStore((s) => s.openTrackKey === trackKey && trackKey !== "");
  const openPanel = useCqStore((s) => s.open);
  const closePanel = useCqStore((s) => s.close);
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const refRef = useRef(ref);
  refRef.current = ref;
  const [tileSize, setTileSize] = useState({ width: 320, height: 260 });

  const source = ref?.source;
  const eligible =
    !!ref && (source === Track.Source.Camera || source === Track.Source.ScreenShare);

  const participant = ref?.participant;
  const publicationTrack = ref?.publication?.track;

  useEffect(() => {
    if (!eligible || !trackKey) return;
    const current = refRef.current;
    if (!current) return;
    const anchor = anchorRef.current;
    const observer =
      typeof IntersectionObserver !== "undefined" && anchor
        ? new IntersectionObserver(
            (entries) => {
              const visible = entries.some((e) => e.isIntersecting);
              if (visible) statsCollector.watch(trackKey, refRef.current ?? current, CADENCE.idle);
              else statsCollector.unwatch(trackKey);
            },
            { threshold: 0.05 },
          )
        : null;
    statsCollector.watch(trackKey, current, CADENCE.idle);
    reapplyVideoOverride(trackKey, current);
    if (observer && anchor) observer.observe(anchor);
    return () => {
      observer?.disconnect();
      statsCollector.unwatch(trackKey);
    };
  }, [eligible, trackKey, participant, publicationTrack]);

  useEffect(() => {
    if (!trackKey) return;
    statsCollector.setCadence(trackKey, isOpen ? CADENCE.watch : CADENCE.idle);
  }, [isOpen, trackKey]);


  useEffect(() => {
    const host = anchorRef.current?.parentElement;
    if (!host || typeof ResizeObserver === "undefined") return;
    const update = () => setTileSize({ width: host.clientWidth || 320, height: host.clientHeight || 260 });
    update();
    const ro = new ResizeObserver(update);
    ro.observe(host);
    return () => ro.disconnect();
  }, []);

  const toggle = useCallback(() => {
    if (!trackKey) return;
    if (isOpen) closePanel();
    else openPanel(trackKey);
  }, [closePanel, isOpen, openPanel, trackKey]);

  if (!show || !eligible || !ref || !trackKey) return null;

  const color = lost ? BAR_COLORS.low : BAR_COLORS[band as CqBand | "idle"];
  const panelWidth = Math.max(220, Math.min(320, tileSize.width - 16));
  const panelHeight = Math.max(220, tileSize.height - 12);
  const icon = (
    <svg viewBox="0 0 18 18" width="16" height="16" aria-hidden="true">
      {barsFor(band as CqBand | "idle").map((h, i) => (
        <rect key={i} x={1 + i * 4} y={14 - h} width="3" height={h} rx="1" fill={color} />
      ))}
    </svg>
  );

  return (
    <div ref={anchorRef} className="absolute top-2 left-2 z-30">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
        aria-expanded={isOpen}
        aria-haspopup="dialog"
        aria-label={t("cq.button.ariaLabel", {
          name: ref.participant.name || ref.participant.identity,
          band: t(`cq.band.${lost ? "lost" : band}`),
          defaultValue: `Connection quality — ${t(`cq.band.${lost ? "lost" : band}`)}`,
        })}
        title={t("cq.button.title", { defaultValue: "Connection quality" })}
        data-testid={`tile-cq-${ref.participant.identity}`}
        className={[
          "relative inline-flex items-center justify-center w-7 h-7 rounded-lg",
          "bg-black/70 hover:bg-black/85 border border-white/25 text-white",
          "opacity-80 hover:opacity-100 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity",
          isOpen ? "ring-2 ring-accent-500 opacity-100" : "",
        ].join(" ")}
      >
        {icon}
        <span
          aria-hidden
          className="absolute -bottom-0.5 -right-0.5 w-2 h-2 rounded-full ring-2 ring-primary-900"
          style={{ backgroundColor: color }}
        />
      </button>
      {isOpen && (
        <ConnectionQualityOverlay
          trackKey={trackKey}
          trackRef={ref}
          width={panelWidth}
          height={panelHeight}
          onClose={closePanel}
        />
      )}
    </div>
  );
}

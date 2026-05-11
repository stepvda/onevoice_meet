import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ParticipantTile,
  TrackRefContext,
  useEnsureTrackRef,
} from "@livekit/components-react";
import { FlipHorizontal2 } from "lucide-react";
import { usePreferences } from "../lib/preferences";

/**
 * A drop-in replacement for `<ParticipantTile />` that overlays a small
 * "flip horizontally" button in the top-right corner. The button toggles a
 * `transform: scaleX(-1)` on every <video> element rendered inside the tile.
 *
 * Implementation note: we don't replace LiveKit's tile, just wrap it. The
 * inner <video> is rendered by LiveKit; we apply the transform via inline
 * style on the matching DOM nodes when the flip flag is true.
 */
export default function FlippableTile() {
  const { t } = useTranslation();
  const ref = useEnsureTrackRef();
  const isLocal = ref?.participant?.isLocal ?? false;
  const mirrorOwnPref = usePreferences((s) => s.display.mirrorOwnVideo);
  // Local tile defaults to mirrored when the preference is on; the button
  // below can still override it for this session, and the preference re-syncs
  // whenever it changes.
  const [flipped, setFlipped] = useState(isLocal && mirrorOwnPref);
  useEffect(() => {
    if (isLocal) setFlipped(mirrorOwnPref);
  }, [isLocal, mirrorOwnPref]);
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  // Apply / clear `transform: scaleX(-1)` on every <video> inside the tile.
  useEffect(() => {
    const root = wrapperRef.current;
    if (!root) return;
    const apply = () => {
      const videos = root.querySelectorAll<HTMLVideoElement>("video");
      videos.forEach((v) => {
        v.style.transform = flipped ? "scaleX(-1)" : "";
        v.style.transition = "transform 100ms";
      });
    };
    apply();
    // LiveKit may swap the underlying <video> element on track replacement;
    // re-apply on DOM mutations.
    const obs = new MutationObserver(apply);
    obs.observe(root, { childList: true, subtree: true });
    return () => obs.disconnect();
  }, [flipped]);

  // Identity for the data-testid so per-tile assertions are possible.
  const identity = ref?.participant?.identity ?? "unknown";

  return (
    <TrackRefContext.Provider value={ref}>
      <div
        ref={wrapperRef}
        className="relative w-full h-full group"
        data-testid={`tile-${identity}`}
      >
        <ParticipantTile />
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setFlipped((v) => !v);
          }}
          aria-pressed={flipped}
          aria-label={flipped ? t("tile.stopFlip") : t("tile.flip")}
          data-testid={`tile-flip-${identity}`}
          className={[
            "absolute top-2 right-2 z-10 p-1.5 rounded-md",
            "bg-black/55 hover:bg-black/75 text-white",
            "opacity-50 hover:opacity-100 group-hover:opacity-100 focus-visible:opacity-100",
            "transition-opacity",
            flipped ? "ring-2 ring-accent-500 opacity-100" : "",
          ].join(" ")}
          title={flipped ? t("tile.unflip") : t("tile.flip")}
        >
          <FlipHorizontal2 size={14} />
        </button>
      </div>
    </TrackRefContext.Provider>
  );
}

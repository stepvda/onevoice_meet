import { useContext, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ParticipantTile,
  TrackRefContext,
  useEnsureTrackRef,
} from "@livekit/components-react";
import { FlipHorizontal2, Hand } from "lucide-react";
import { usePreferences } from "../lib/preferences";
import { GridStageContext, GridFocusContext } from "../lib/gridStage";
import { useHandRaiseState } from "../lib/handRaise";

/**
 * A drop-in replacement for `<ParticipantTile />` that overlays a small
 * "flip horizontally" button in the top-right corner. The button toggles a
 * `transform: scaleX(-1)` on every <video> element rendered inside the tile.
 *
 * The wrapper also resizes itself to match the source video's intrinsic
 * aspect ratio so a portrait stream in a landscape slot shrinks to a
 * narrow centered tile (no cropping) and vice versa. We compute exact
 * pixel dimensions via ResizeObserver rather than using CSS `aspect-ratio`
 * — that property combined with `width: 100%` makes the box's height
 * depend only on the parent's width, so height-only viewport resizes
 * don't re-fit the tile.
 */
export default function FlippableTile() {
  const { t } = useTranslation();
  const ref = useEnsureTrackRef();
  const isLocal = ref?.participant?.isLocal ?? false;
  // Identity for the data-testid and for the flip re-sync below.
  const identity = ref?.participant?.identity ?? "unknown";
  // The playback ingress publishes uploaded videos — mirroring a movie is
  // never right, so this tile is exempt from the mirror preference and the
  // manual flip button.
  const isPlayback = identity === "playback";
  const mirrorOwnPref = usePreferences((s) => s.display.mirrorOwnVideo);
  // Grid-mode tile-shape standardization. The value lives in prefs; the
  // boolean context is true only inside the grid stage, so non-grid tiles
  // (full-bleed, speaker thumbnails, PiP overlay) always keep native aspect.
  // `?? "off"` guards persisted prefs written before this key existed.
  const inGridStage = useContext(GridStageContext);
  const gridAspectPref = usePreferences((s) => s.display.gridAspect ?? "off");
  const std = inGridStage ? gridAspectPref : "off";
  // Double-tap / double-click to zoom this tile to the full stage, and again
  // to return to the grid. Honoured only inside a grid focus scope (grid mode
  // + the zoomed single-tile view); a no-op everywhere else. `trackKey` must
  // match PresenterSpotlight's keying (identity + source).
  const focusCtx = useContext(GridFocusContext);
  const trackKey = `${identity}-${ref?.source ?? ""}`;
  // Grid-stage-only: the accent ring marks the selected tile inside the grid.
  // The zoomed full-bleed view renders outside GridStageContext, so gating on
  // `inGridStage` keeps a stray frame off the full-screen zoom.
  const isFocused = inGridStage && focusCtx?.focusedKey === trackKey;
  const lastTapRef = useRef(0);
  const handleTileClick = () => {
    if (!focusCtx) return;
    // One handler covers mouse double-click and touch double-tap: two
    // activations within 350 ms toggle focus. `touch-action: manipulation`
    // (below) stops mobile double-tap-to-zoom from stealing the gesture.
    const now = Date.now();
    if (now - lastTapRef.current < 350) {
      lastTapRef.current = 0;
      focusCtx.toggle(trackKey);
    } else {
      lastTapRef.current = now;
    }
  };
  const hand = useHandRaiseState(ref?.participant);
  // Local tile defaults to mirrored when the preference is on; the button
  // below can still override it for this session, and the preference re-syncs
  // whenever it changes.
  const [flipped, setFlipped] = useState(!isPlayback && isLocal && mirrorOwnPref);
  const prevIdentityRef = useRef(identity);
  useEffect(() => {
    // Two triggers, only these:
    //   1. Participant swap — layouts swap the track ref via context without
    //      remounting (e.g. the full-bleed slot going from the host's mirrored
    //      camera to the playback ingress), so a flip set for one participant
    //      must not leak onto the next.
    //   2. The LOCAL mirror preference changing — keep the own tile in sync.
    // We must NOT reset on pref change for a REMOTE tile: the viewer may have
    // manually flipped a colleague's tile, and toggling their own mirror
    // preference should leave that manual flip alone.
    const identityChanged = prevIdentityRef.current !== identity;
    prevIdentityRef.current = identity;
    if (identityChanged || isLocal) {
      setFlipped(!isPlayback && isLocal && mirrorOwnPref);
    }
  }, [identity, isPlayback, isLocal, mirrorOwnPref]);
  // Outer slot (fills the layout-allocated cell) and inner box (sized to
  // the video's aspect, centered inside the slot).
  const slotRef = useRef<HTMLDivElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  // Source video aspect = videoWidth / videoHeight. `null` until the
  // first frame's metadata arrives. While null we fill the slot — same
  // pre-aspect-fit behaviour, so the placeholder face icon still works.
  const [videoAspect, setVideoAspect] = useState<number | null>(null);

  // Apply / clear the mirror transform on every <video>, force
  // object-fit: contain as a belt-and-braces against LiveKit's stylesheet,
  // and read videoWidth/videoHeight as soon as metadata arrives.
  useEffect(() => {
    const root = wrapperRef.current;
    if (!root) return;

    // Owns the listeners we attach to each <video> so they can be cleaned
    // up when the element is swapped out by LiveKit on track replacement.
    const tracked = new WeakMap<HTMLVideoElement, () => void>();

    const readAspect = (v: HTMLVideoElement) => {
      const w = v.videoWidth;
      const h = v.videoHeight;
      if (w > 0 && h > 0) {
        setVideoAspect((prev) => {
          const next = w / h;
          // Avoid pointless re-renders when the same aspect re-fires.
          if (prev != null && Math.abs(prev - next) < 1e-4) return prev;
          return next;
        });
      }
    };

    const apply = () => {
      const videos = root.querySelectorAll<HTMLVideoElement>("video");
      videos.forEach((v) => {
        v.style.transform = flipped && !isPlayback ? "scaleX(-1)" : "";
        v.style.transition = "transform 100ms";
        // Standardized mode crops EVERY stream (camera, playback, screenshare)
        // to cover-fill its uniform tile so the grid tiles up cleanly with no
        // gaps — the user opts into cropping via the toggle. Native mode
        // letterboxes to preserve the whole frame. setProperty(..., "important")
        // is required to beat the broad `object-fit: contain !important` rule
        // in global.css.
        v.style.setProperty(
          "object-fit",
          std !== "off" ? "cover" : "contain",
          "important",
        );
        v.style.background = "#000";

        if (!tracked.has(v)) {
          const onMeta = () => readAspect(v);
          // `resize` fires when intrinsic dimensions change mid-track
          // (publisher rotating their phone, simulcast layer switch).
          v.addEventListener("loadedmetadata", onMeta);
          v.addEventListener("resize", onMeta);
          tracked.set(v, () => {
            v.removeEventListener("loadedmetadata", onMeta);
            v.removeEventListener("resize", onMeta);
          });
        }
        readAspect(v);
      });
    };

    apply();
    const obs = new MutationObserver(apply);
    obs.observe(root, { childList: true, subtree: true });
    return () => {
      obs.disconnect();
      root.querySelectorAll<HTMLVideoElement>("video").forEach((v) => {
        tracked.get(v)?.();
      });
    };
  }, [flipped, isPlayback, std]);

  // Resize-fit loop. Whenever the slot's box changes or the source's
  // aspect changes, recompute the wrapper's pixel width/height so that:
  //   - both axes fit inside the slot (no overflow on either dimension)
  //   - the wrapper's own aspect matches the source video
  // The two together mean a portrait source in a landscape slot becomes
  // a narrow tall tile; a landscape source in a portrait slot becomes a
  // wide short tile. CSS `aspect-ratio` can't express this constraint
  // (it leaves one axis underspecified, hence the user-reported bug
  // where height-only viewport resizes failed to re-fit).
  useEffect(() => {
    const slot = slotRef.current;
    const wrap = wrapperRef.current;
    if (!slot || !wrap) return;

    const fit = () => {
      if (std !== "off") {
        // Standardized: the grid sizes the CELL to the target aspect, so the
        // tile fills it edge-to-edge and the video covers/crops. No inner
        // aspect box — that is what left gaps between differently-shaped cells.
        wrap.style.width = "100%";
        wrap.style.height = "100%";
        return;
      }
      const va = videoAspect;
      if (!va) {
        // No metadata yet — fill the slot so the placeholder renders.
        wrap.style.width = "100%";
        wrap.style.height = "100%";
        return;
      }
      const pw = slot.clientWidth;
      const ph = slot.clientHeight;
      if (pw <= 0 || ph <= 0) return;
      const parentAspect = pw / ph;
      let w: number;
      let h: number;
      if (parentAspect > va) {
        // Slot is wider than the video's aspect → height-bound. The
        // wrapper takes the full height, width derived from aspect.
        h = ph;
        w = ph * va;
      } else {
        // Slot is taller (or equal) → width-bound.
        w = pw;
        h = pw / va;
      }
      wrap.style.width = `${w}px`;
      wrap.style.height = `${h}px`;
    };

    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(slot);
    return () => ro.disconnect();
  }, [videoAspect, std]);

  return (
    <TrackRefContext.Provider value={ref}>
      {/* Outer slot — fills whatever the parent layout (GridLayout cell,
          focus pane, playback stage) allocated. Centers the wrapper so
          an aspect-fitted box sits in the middle of the slot. */}
      <div
        ref={slotRef}
        onClick={focusCtx ? handleTileClick : undefined}
        className={[
          "w-full h-full flex items-center justify-center overflow-hidden",
          // Only interactive inside a grid focus scope; disables the mobile
          // double-tap-zoom so our double-tap gesture is delivered.
          focusCtx ? "cursor-pointer select-none [touch-action:manipulation]" : "",
        ].join(" ")}
      >
        <div
          ref={wrapperRef}
          data-testid={`tile-${identity}`}
          className={[
            "relative group",
            isFocused ? "ring-2 ring-accent-500 rounded-md" : "",
          ].join(" ")}
        >
          <ParticipantTile />
          {hand.raised && (
            <div
              data-testid={`tile-hand-${identity}`}
              className="absolute top-2 left-2 z-10 inline-flex items-center justify-center w-7 h-7 rounded-full bg-amber-500 text-white shadow-md ring-2 ring-amber-300/50"
              title={t("hand.tileBadge")}
              aria-label={t("hand.tileBadge")}
            >
              <Hand size={14} />
            </div>
          )}
          {!isPlayback && (
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
          )}
        </div>
      </div>
    </TrackRefContext.Provider>
  );
}

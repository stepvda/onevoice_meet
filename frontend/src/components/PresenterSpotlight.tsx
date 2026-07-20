import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  GridLayout,
  TrackRefContext,
  useRoomContext,
  useTracks,
} from "@livekit/components-react";
import { RoomEvent, Track } from "livekit-client";
import type { TrackReferenceOrPlaceholder } from "@livekit/components-react";
import FlippableTile from "./FlippableTile";
import { usePreferences } from "../lib/preferences";
import { useIsMobile } from "../lib/useIsMobile";
import { GridStageContext, GridFocusContext } from "../lib/gridStage";

type RoomLayout = "single-speaker" | "speaker" | "grid";

/**
 * Room-wide composition shared between every live viewer, the recording,
 * and the livestream. The layout is the room's, not per-user:
 *
 *   - "single-speaker": one full-bleed main = screenshare > playback >
 *     presenter (Take stage) > active speaker > any cam.
 *   - "speaker": same main, plus a centered bottom thumbnail strip of
 *     every OTHER camera-publishing participant.
 *   - "grid": equal-tile grid of everyone.
 *
 * Host changes the layout via the toolbar picker, which POSTs to
 * `/meetings/{id}/layout`. That endpoint persists the choice on the
 * meeting AND pushes it to LiveKit room metadata, so every connected
 * client re-renders in lockstep on `RoomMetadataChanged`.
 *
 * Two things stay isolated from `roomLayout`:
 *   1. Server-side composite (the PiP compositor publishes a track from
 *      `composite-<room>`). When present, it's the final composite and we
 *      render it full-bleed regardless of the room layout.
 *   2. Client-side PiP (room metadata `pip_enabled` + `pip_overlay_identity`).
 *      Same fallback layout as the old PiP page — main + corner overlay,
 *      full-bleed. PiP is a separate toggle and takes precedence over the
 *      room layout when on.
 *
 * Note: the per-user `display.layout` zustand pref (auto/grid/speaker/
 * spotlight) is now ignored at composition time. `hideSelfView` and
 * `hideEmptyTiles` are still honoured.
 */
const VALID_ROOM_LAYOUTS: ReadonlySet<RoomLayout> = new Set([
  "single-speaker",
  "speaker",
  "grid",
]);

function parseRoomLayout(v: unknown): RoomLayout | null {
  return typeof v === "string" && VALID_ROOM_LAYOUTS.has(v as RoomLayout)
    ? (v as RoomLayout)
    : null;
}

export default function PresenterSpotlight() {
  const room = useRoomContext();
  const display = usePreferences((s) => s.display);
  const [roomLayout, setRoomLayout] = useState<RoomLayout>("grid");
  const [presenterId, setPresenterId] = useState<string | null>(null);
  const [activeSpeakerId, setActiveSpeakerId] = useState<string | null>(null);
  // Picture-in-Picture is independent of `roomLayout`; mirrors the
  // meeting's server-side toggle via LiveKit room metadata.
  const [pipEnabled, setPipEnabled] = useState(false);
  const [pipOverlayIdentity, setPipOverlayIdentity] = useState<string | null>(
    null,
  );

  useEffect(() => {
    const apply = () => {
      let md: Record<string, unknown> = {};
      try {
        md = JSON.parse(room.metadata || "{}");
      } catch {
        /* leave md empty */
      }
      const layout = parseRoomLayout(md.room_layout);
      if (layout) setRoomLayout(layout);
      setPresenterId(
        typeof md.presenter_identity === "string" ? md.presenter_identity : null,
      );
      setPipEnabled(!!md.pip_enabled);
      setPipOverlayIdentity(
        typeof md.pip_overlay_identity === "string"
          ? md.pip_overlay_identity
          : null,
      );
    };
    apply();
    // LiveKit only fires `RoomMetadataChanged` for *changes* after the
    // initial join — late joiners receive the room metadata silently
    // during the handshake and never see an event. The `Connected` and
    // `Reconnected` listeners cover that initial-state gap.
    room.on(RoomEvent.RoomMetadataChanged, apply);
    room.on(RoomEvent.Connected, apply);
    room.on(RoomEvent.Reconnected, apply);
    return () => {
      room.off(RoomEvent.RoomMetadataChanged, apply);
      room.off(RoomEvent.Connected, apply);
      room.off(RoomEvent.Reconnected, apply);
    };
  }, [room]);

  // Active-speaker tracking — needed in single-speaker, speaker, and PiP
  // modes. Cheaper to always subscribe than to gate by layout (the event
  // fires regardless and toggling subscribe on/off is its own footgun).
  useEffect(() => {
    const apply = () => {
      const top = room.activeSpeakers[0];
      if (top) setActiveSpeakerId(top.identity);
    };
    apply();
    room.on(RoomEvent.ActiveSpeakersChanged, apply);
    return () => {
      room.off(RoomEvent.ActiveSpeakersChanged, apply);
    };
  }, [room]);

  const rawTracks = useTracks(
    [
      { source: Track.Source.Camera, withPlaceholder: true },
      { source: Track.Source.ScreenShare, withPlaceholder: false },
    ],
    { onlySubscribed: false }
  );

  const tracks: TrackReferenceOrPlaceholder[] = useMemo(() => {
    const me = room.localParticipant.identity;
    return rawTracks.filter((t) => {
      // Hide the compositor bot from regular tiles — its screenshare
      // track is consumed by the composite branch below (full-bleed) and
      // its entry never belongs in a tile alongside humans.
      if (t.participant.identity.startsWith("composite-")) return false;
      if (display.hideSelfView && t.participant.identity === me) return false;
      if (
        display.hideEmptyTiles &&
        !(t as { publication?: unknown }).publication
      ) {
        return false;
      }
      return true;
    });
  }, [rawTracks, display.hideSelfView, display.hideEmptyTiles, room.localParticipant.identity]);

  // Video-playback hijacks the stage: when LiveKit Ingress publishes the
  // current playlist item as participant identity "playback", that tile
  // dominates in single-speaker and speaker layouts. In grid mode the
  // playback tile still wins (it's the meeting's primary content).
  const playbackTrack = useMemo(
    () => tracks.find((t) => t.participant.identity === "playback" && t.source === Track.Source.Camera) ?? null,
    [tracks],
  );

  // Server-side PiP composite. When the meeting has `pip_enabled` on,
  // the compositor service publishes a ScreenShare track from identity
  // `composite-<room>`. Every client (including the publisher who
  // contributed the source tracks) shows this composite full-bleed,
  // hiding all raw tracks — so what people see live matches the
  // recording / livestream byte-for-byte. While `pipEnabled` is true but
  // the compositor session hasn't landed its first frame yet (~3 s
  // after toggle), this is null and we fall through to the client-side
  // PiP fallback below.
  const compositeTrack = useMemo(() => {
    return (
      rawTracks.find(
        (t) =>
          t.participant.identity.startsWith("composite-") &&
          t.source === Track.Source.ScreenShare,
      ) ?? null
    );
  }, [rawTracks]);

  // Main video for single-speaker / speaker layouts AND for the
  // client-side PiP fallback. Same priority ladder as the egress page so
  // live + recording / livestream pick the same person.
  const main = useMemo<TrackReferenceOrPlaceholder | null>(() => {
    const screen = tracks.find((t) => t.source === Track.Source.ScreenShare);
    if (screen) return screen;
    if (playbackTrack) return playbackTrack;
    const pickCamFor = (identity: string | null) => {
      if (!identity) return null;
      return (
        tracks.find(
          (t) => t.participant.identity === identity && t.source === Track.Source.Camera,
        ) ?? null
      );
    };
    return (
      pickCamFor(presenterId) ??
      pickCamFor(activeSpeakerId) ??
      tracks.find((t) => t.source === Track.Source.Camera) ??
      null
    );
  }, [tracks, playbackTrack, presenterId, activeSpeakerId]);

  // Mobile gets a bespoke non-paginating grid; desktop keeps LiveKit's.
  const { isMobile, isPortrait } = useIsMobile();
  // Grid tile-shape standardization (toggled from the toolbar). "off" keeps
  // native aspect-fit; "landscape"/"portrait" render a uniform 4:3 / 3:4
  // cover-cropped grid that tiles cleanly.
  const gridAspect = usePreferences((s) => s.display.gridAspect ?? "off");

  // Grid ordering: lead with the playback ingress so the video-playlist tile
  // is always in the first visible row and never paginated / scrolled
  // off-screen — the fix for playback being invisible in grid mode on phones.
  const orderedTracks = useMemo<TrackReferenceOrPlaceholder[]>(() => {
    if (!playbackTrack) return tracks;
    return [playbackTrack, ...tracks.filter((t) => t !== playbackTrack)];
  }, [tracks, playbackTrack]);

  // Grid + active screenshare auto-promotes to "speaker" (the shared screen
  // owns the main tile). Computed here — before the early returns — so the
  // focus-reset effect can depend on it without breaking rules-of-hooks.
  const hasScreenshare = tracks.some((t) => t.source === Track.Source.ScreenShare);
  const effectiveRoomLayout: RoomLayout =
    roomLayout === "grid" && hasScreenshare ? "speaker" : roomLayout;

  // Double-tap / double-click a tile to zoom it to the full stage, and again
  // to return to the grid. Per-viewer, grid-mode only. FlippableTile toggles
  // via GridFocusContext; we own the key here.
  const [focusedKey, setFocusedKey] = useState<string | null>(null);
  const focusCtx = useMemo(
    () => ({
      focusedKey,
      toggle: (key: string) =>
        setFocusedKey((prev) => (prev === key ? null : key)),
    }),
    [focusedKey],
  );
  // Drop the zoom when we leave grid mode or the focused track disappears, so
  // the viewer is never stranded on a dead full-bleed tile.
  useEffect(() => {
    if (effectiveRoomLayout !== "grid") setFocusedKey(null);
  }, [effectiveRoomLayout]);
  useEffect(() => {
    if (focusedKey && !orderedTracks.some((t) => trackKey(t) === focusedKey)) {
      setFocusedKey(null);
    }
  }, [focusedKey, orderedTracks]);

  // ── 1. Server composite always wins ─────────────────────────────────
  if (compositeTrack) {
    return <FullBleed track={compositeTrack} />;
  }

  // ── 2. Client-side PiP fallback (active when pip_enabled but the
  //       compositor track hasn't landed yet) ─────────────────────────
  if (pipEnabled && main) {
    const pipOverlayTrack: TrackReferenceOrPlaceholder | null =
      pipOverlayIdentity
        ? tracks.find(
            (t) =>
              t.participant.identity === pipOverlayIdentity &&
              t.source === Track.Source.Camera,
          ) ?? null
        : null;
    const showOverlay =
      pipOverlayTrack &&
      !(
        pipOverlayTrack.participant.identity === main.participant.identity &&
        pipOverlayTrack.source === main.source
      );
    return (
      <div className="relative h-full bg-black overflow-hidden">
        <div className="absolute inset-0">
          <TrackRefContext.Provider value={main}>
            <FlippableTile />
          </TrackRefContext.Provider>
        </div>
        {showOverlay && pipOverlayTrack && (
          <div
            data-testid="pip-overlay"
            className="absolute right-3 bottom-3 sm:right-4 sm:bottom-4 w-[28%] sm:w-[22%] aspect-video rounded-lg overflow-hidden border-2 border-white/90 shadow-xl bg-black"
          >
            <TrackRefContext.Provider value={pipOverlayTrack}>
              <FlippableTile />
            </TrackRefContext.Provider>
          </div>
        )}
      </div>
    );
  }

  // ── 3. Playback hijack (full-bleed in single-speaker / speaker; in
  //       grid we still tile but playback is the natural "main") ─────
  if (playbackTrack && roomLayout !== "grid") {
    return <FullBleed track={playbackTrack} />;
  }

  // ── 4. Room layout ───────────────────────────────────────────────────
  // (grid + screenshare auto-promotion to "speaker" is computed above so the
  // focus-reset effect can depend on it.)
  if (effectiveRoomLayout === "grid") {
    // Double-tap zoom: render just the focused track full-bleed. Rendered
    // OUTSIDE GridStageContext so it keeps native aspect (never squished by
    // the tile-shape toggle); GridFocusContext stays so a second double-tap
    // returns to the grid.
    const focused = focusedKey
      ? orderedTracks.find((t) => trackKey(t) === focusedKey) ?? null
      : null;
    if (focused) {
      return (
        <GridFocusContext.Provider value={focusCtx}>
          <FullBleed track={focused} />
        </GridFocusContext.Provider>
      );
    }
    return (
      <GridStageContext.Provider value={true}>
        <GridFocusContext.Provider value={focusCtx}>
          {gridAspect !== "off" ? (
            <UniformGrid
              tracks={orderedTracks}
              aspect={gridAspect === "landscape" ? 4 / 3 : 3 / 4}
              isMobile={isMobile}
              isPortrait={isPortrait}
            />
          ) : isMobile ? (
            <MobileGrid tracks={orderedTracks} isPortrait={isPortrait} />
          ) : (
            <GridLayout tracks={orderedTracks} className="h-full p-2">
              <FlippableTile />
            </GridLayout>
          )}
        </GridFocusContext.Provider>
      </GridStageContext.Provider>
    );
  }

  if (!main) {
    // No video yet — render an empty grid so placeholders fill the stage
    // gracefully instead of going black.
    return (
      <GridLayout tracks={tracks} className="h-full p-2">
        <FlippableTile />
      </GridLayout>
    );
  }

  if (effectiveRoomLayout === "single-speaker") {
    return <FullBleed track={main} />;
  }

  // effective "speaker": main + bottom thumbnail strip of others.
  const others = tracks.filter(
    (t) => !(t.participant.identity === main.participant.identity && t.source === main.source),
  );
  return (
    <div className="flex h-full flex-col bg-black">
      <div className="flex-1 min-h-0 p-2">
        <div className="relative h-full">
          <div className="absolute inset-0">
            <TrackRefContext.Provider value={main}>
              <FlippableTile />
            </TrackRefContext.Provider>
          </div>
        </div>
      </div>
      {others.length > 0 && (
        <div
          data-testid="speaker-thumbnails"
          className="h-[20%] min-h-[120px] flex justify-center items-stretch gap-2 px-3 py-2 bg-black/40 overflow-x-auto"
        >
          {others.map((t) => (
            <div
              key={`${t.participant.identity}-${t.source}`}
              className="aspect-video h-full flex-shrink-0 rounded-md overflow-hidden bg-primary-900"
            >
              <TrackRefContext.Provider value={t}>
                <FlippableTile />
              </TrackRefContext.Provider>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function FullBleed({ track }: { track: TrackReferenceOrPlaceholder }) {
  return (
    <div className="relative h-full bg-black overflow-hidden">
      <div className="absolute inset-0">
        <TrackRefContext.Provider value={track}>
          <FlippableTile />
        </TrackRefContext.Provider>
      </div>
    </div>
  );
}

// Stable key for a track ref (identity + source). MUST match FlippableTile's
// own `${identity}-${ref.source}` so double-tap focus targets the right tile.
function trackKey(t: TrackReferenceOrPlaceholder): string {
  return `${t.participant.identity}-${t.source}`;
}

// Mobile grid. <=6 tracks fill the stage (no scroll); >6 scrolls, with never
// more than 6 tiles in view. While a playlist video plays it is pinned at the
// top (always mounted, never scrolled away) so it stays visible in grid mode —
// the remaining participants scroll below it, each lazy-mounted so only the
// on-screen streams (plus a small preload band) stay subscribed.
//
// The outer structure is identical in every mode so tiles reconcile in place
// across the 6<->7 boundary (only the added/removed tile mounts — no full-grid
// remount / black flash).
function MobileGrid({
  tracks,
  isPortrait,
}: {
  tracks: TrackReferenceOrPlaceholder[];
  isPortrait: boolean;
}) {
  const cols = isPortrait ? 2 : 3;
  const rows = isPortrait ? 3 : 2;
  const capacity = cols * rows; // 6 in view either way
  const scrollMode = tracks.length > capacity;

  // Pin the playback ingress (kept at index 0 by orderedTracks) ONLY when we
  // scroll — otherwise a LazyTile would unmount it as it scrolls off. In fill
  // mode nothing scrolls, so it stays a normal always-mounted grid cell.
  const pinned =
    scrollMode && tracks[0]?.participant.identity === "playback"
      ? tracks[0]
      : null;
  const rest = pinned ? tracks.slice(1) : tracks;

  return (
    <div className="flex h-full w-full flex-col gap-2 p-2">
      {pinned && (
        <div key="pinned" className="min-h-0 basis-[40%] shrink-0">
          <TrackRefContext.Provider value={pinned}>
            <FlippableTile />
          </TrackRefContext.Provider>
        </div>
      )}
      <div key="grid" className="min-h-0 flex-1">
        <AdaptiveGrid tracks={rest} isPortrait={isPortrait} reserveRow={!!pinned} />
      </div>
    </div>
  );
}

// The grid itself. Fills its container when everything fits; when it doesn't,
// measures row height so exactly `visibleRows` fill the container and the rest
// scroll — never more than 6 tiles occupy the viewport. `reserveRow` drops one
// visible row to leave headroom for a pinned playback tile above. The outer div
// and the per-tile LazyTile are the SAME element types in both modes, so
// switching between fill and scroll never remounts the tiles.
function AdaptiveGrid({
  tracks,
  isPortrait,
  reserveRow,
}: {
  tracks: TrackReferenceOrPlaceholder[];
  isPortrait: boolean;
  reserveRow: boolean;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [rowH, setRowH] = useState(0);
  const scrollCols = isPortrait ? 2 : 3;
  const visibleRows = Math.max(1, (isPortrait ? 3 : 2) - (reserveRow ? 1 : 0));
  const capacity = scrollCols * visibleRows;
  const count = tracks.length;
  const scroll = count > capacity;
  // Fill mode uses a face-friendlier column count for small groups (stack 2 on
  // a portrait phone rather than two skinny columns).
  const cols = scroll
    ? scrollCols
    : count <= 1
    ? 1
    : isPortrait
    ? count === 2
      ? 1
      : 2
    : count <= 2
    ? 2
    : 3;
  const rows = Math.max(1, Math.ceil(count / cols));
  useEffect(() => {
    const el = rootRef.current;
    if (!el || !scroll) return;
    const GAP = 8; // matches the grid gap below
    const measure = () => {
      const h = el.clientHeight;
      setRowH(Math.max(0, (h - (visibleRows - 1) * GAP) / visibleRows));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [scroll, visibleRows]);
  const style: CSSProperties = scroll
    ? {
        gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
        gridAutoRows: rowH ? `${rowH}px` : undefined,
        gap: 8,
      }
    : {
        gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
        gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
        gap: 8,
      };
  return (
    <div
      ref={rootRef}
      data-testid="mobile-grid"
      className={[
        "h-full w-full",
        scroll ? "overflow-y-auto overscroll-contain" : "overflow-hidden",
      ].join(" ")}
    >
      <div className={scroll ? "grid w-full" : "grid h-full w-full"} style={style}>
        {tracks.map((tr) => (
          <LazyTile
            key={trackKey(tr)}
            track={tr}
            root={rootRef}
            eager={!scroll}
          />
        ))}
      </div>
    </div>
  );
}

// One grid cell. In fill mode (`eager`) it always renders the live tile. In
// scroll mode it mounts the live tile only when near the viewport so off-screen
// streams unmount and LiveKit's adaptive stream pauses them; the slot keeps its
// full cell height either way so scroll geometry stays correct.
function LazyTile({
  track,
  root,
  eager,
}: {
  track: TrackReferenceOrPlaceholder;
  root: { current: HTMLDivElement | null };
  eager: boolean;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [show, setShow] = useState(eager);
  useEffect(() => {
    if (eager) {
      setShow(true);
      return;
    }
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) setShow(e.isIntersecting);
      },
      { root: root.current, rootMargin: "300px 0px", threshold: 0 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [root, eager]);
  return (
    <div ref={ref} className="h-full w-full min-w-0 min-h-0">
      {show ? (
        <TrackRefContext.Provider value={track}>
          <FlippableTile />
        </TrackRefContext.Provider>
      ) : (
        <div className="flex h-full w-full items-center justify-center rounded-md bg-primary-900/60">
          <span className="truncate px-1 text-[10px] text-slate-500">
            {track.participant.name || track.participant.identity}
          </span>
        </div>
      )}
    </div>
  );
}

// Standardized-mode grid: every stream is cropped to the SAME target aspect
// (4:3 or 3:4) and the uniform tiles are packed edge-to-edge (only a hairline
// gap between them), centered so any leftover is at the container edges, not
// between tiles. On mobile the columns are fixed (2 portrait / 3 landscape) and
// the grid scrolls when tiles overflow; on desktop the column count is chosen
// to make the tiles as large as possible while everyone stays in view.
function UniformGrid({
  tracks,
  aspect,
  isMobile,
  isPortrait,
}: {
  tracks: TrackReferenceOrPlaceholder[];
  aspect: number;
  isMobile: boolean;
  isPortrait: boolean;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [dims, setDims] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const measure = () => setDims({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const GAP = 6;
  const { cols, tileW, tileH, scroll } = computeUniform(
    dims.w,
    dims.h,
    tracks.length,
    aspect,
    GAP,
    isMobile,
    isPortrait,
  );
  const style: CSSProperties = {
    display: "grid",
    gridTemplateColumns: `repeat(${cols}, ${tileW}px)`,
    gridAutoRows: `${tileH}px`,
    gap: `${GAP}px`,
    justifyContent: "center",
    alignContent: scroll ? "start" : "center",
    // Fill mode needs a full-height grid box for alignContent:center to have
    // slack to distribute — otherwise the box collapses to the rows' height
    // and the tiles top-align with all leftover space dumped at the bottom.
    // Scroll mode must NOT be height-capped (content must exceed the container).
    height: scroll ? undefined : "100%",
  };
  return (
    <div
      ref={rootRef}
      data-testid="uniform-grid"
      className={[
        "h-full w-full",
        scroll ? "overflow-y-auto overscroll-contain" : "overflow-hidden",
      ].join(" ")}
    >
      {tileW > 0 && (
        <div style={style}>
          {tracks.map((tr) => (
            <div
              key={trackKey(tr)}
              style={{ width: tileW, height: tileH }}
              className="overflow-hidden rounded-md"
            >
              <LazyTile track={tr} root={rootRef} eager={!scroll} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Column count + uniform tile size for a target-aspect grid. Mobile: fixed
// columns, tiles fill the width and scroll vertically when they overflow.
// Desktop: pick the column count that maximizes tile size while fitting every
// tile in view (largest tiles == least wasted space).
function computeUniform(
  w: number,
  h: number,
  n: number,
  aspect: number,
  gap: number,
  isMobile: boolean,
  isPortrait: boolean,
): { cols: number; tileW: number; tileH: number; scroll: boolean } {
  if (w <= 0 || h <= 0 || n <= 0) {
    return { cols: 1, tileW: 0, tileH: 0, scroll: false };
  }
  if (isMobile) {
    const cols = Math.min(n, isPortrait ? 2 : 3);
    const tileW = (w - (cols - 1) * gap) / cols;
    const tileH = tileW / aspect;
    const rows = Math.ceil(n / cols);
    const totalH = rows * tileH + (rows - 1) * gap;
    return { cols, tileW, tileH, scroll: totalH > h + 1 };
  }
  let best = { cols: 1, tileW: 0, tileH: 0 };
  for (let cols = 1; cols <= n; cols++) {
    const rows = Math.ceil(n / cols);
    let tileW = (w - (cols - 1) * gap) / cols;
    let tileH = tileW / aspect;
    const maxTileH = (h - (rows - 1) * gap) / rows;
    if (tileH > maxTileH) {
      tileH = maxTileH;
      tileW = tileH * aspect;
    }
    if (tileW > best.tileW) best = { cols, tileW, tileH };
  }
  return { ...best, scroll: false };
}

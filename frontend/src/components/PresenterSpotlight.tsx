import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  GridLayout,
  TrackRefContext,
  useRoomContext,
  useTracks,
} from "@livekit/components-react";
import { RoomEvent, Track } from "livekit-client";
import type { TrackReferenceOrPlaceholder } from "@livekit/components-react";
import { LayoutGrid, Minimize2 } from "lucide-react";
import FlippableTile from "./FlippableTile";
import MeetppBoardWindow from "./meetpp/MeetppStage";
import { setStage, useMeetpp } from "../lib/meetpp/store";
import { usePreferences } from "../lib/preferences";
import { useIsMobile } from "../lib/useIsMobile";
import { GridStageContext, GridFocusContext } from "../lib/gridStage";
import {
  BOARD_CANDIDATE,
  BOARD_KEY,
  candidate,
  effectiveRoomLayout,
  pickMainKey,
  type RoomLayout,
  type StageCandidate,
} from "../lib/stage";
import { StageInfoContext } from "../lib/stageControls";
import {
  resetStageView,
  setFocus,
  setViewLayout,
  toggleFocus,
  useStageView,
  type ViewLayout,
} from "../lib/stageView";

/**
 * Room-wide composition shared between every live viewer, the recording,
 * and the livestream. The layout is the room's; each viewer may override it
 * for themselves (lib/stageView.ts).
 *
 * The stage is a list of stream windows: every camera and screen share
 * (LiveKit track references) and, while a Meet++ session runs, the board
 * (lib/stage.ts). One of them is the main stream: the presenter the host
 * chose (`presenter_identity` in the room metadata: a camera, a screen
 * share or the board) > a screen share > playback > the board > the active
 * speaker > any camera — the same ladder as the egress page.
 *
 *   - "single-speaker": the main stream full-bleed.
 *   - "speaker": the main stream plus a bottom strip of every other one.
 *   - "grid": equal tiles of everyone; a shared screen, or the board or a
 *     screen share as the main stream, turns it into "speaker".
 *
 * Host changes the layout via the toolbar picker, which POSTs to
 * `/meetings/{id}/layout`. That endpoint persists the choice on the
 * meeting AND pushes it to LiveKit room metadata, so every connected
 * client re-renders in lockstep on `RoomMetadataChanged`.
 *
 * Every viewer can zoom one stream window to their whole stage (double-click
 * or the tile's zoom button) and pick their own layout (the view menu at the
 * top of the stage); neither affects anyone else or the recording.
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
 * spotlight) is ignored at composition time. `hideSelfView` and
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

/** One stream window on the stage. */
type StageItem =
  | { kind: "track"; tileKey: string; stageKey: string; cand: StageCandidate; track: TrackReferenceOrPlaceholder }
  | { kind: "board"; tileKey: string; stageKey: string; cand: StageCandidate };

type BoardVariant = "stage" | "public";

export default function PresenterSpotlight({ boardForPublicOnly = false }: { boardForPublicOnly?: boolean } = {}) {
  return <SpotlightStage boardForPublicOnly={boardForPublicOnly} />;
}

function SpotlightStage({ boardForPublicOnly }: { boardForPublicOnly: boolean }) {
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
  // Meet++ (FDD §5.10): while a session runs the board is a stream window.
  const meetppActive = useMeetpp((s) => s.active);
  const meetppPublic = useMeetpp((s) => s.snap?.session.settings?.show_public === true);
  const boardWanted = meetppActive && (!boardForPublicOnly || meetppPublic);
  const boardVariant: BoardVariant = boardForPublicOnly ? "public" : "stage";
  const viewLayout = useStageView((s) => s.layout);
  const focusKey = useStageView((s) => s.focusKey);

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
      // its entry never belongs in a tile alongside humans. The Meet++
      // agent is subscribe-only, but never give it a tile either.
      if (t.participant.identity.startsWith("composite-")) return false;
      if (t.participant.identity.startsWith("meetpp-")) return false;
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

  // The stream windows. Order: the playback ingress first (so the playlist
  // tile is always in the first visible row and never paginated / scrolled
  // off-screen — the fix for playback being invisible in grid mode on
  // phones), then the board, then everyone else.
  const items = useMemo<StageItem[]>(() => {
    const trackItems: StageItem[] = tracks.map((t) => {
      const cand = candidate(t.participant.identity, t.source === Track.Source.ScreenShare);
      return { kind: "track", tileKey: trackKey(t), stageKey: cand.key, cand, track: t };
    });
    const board: StageItem[] = boardWanted
      ? [{ kind: "board", tileKey: BOARD_KEY, stageKey: BOARD_KEY, cand: BOARD_CANDIDATE }]
      : [];
    return [
      ...trackItems.filter((i) => i.cand.kind === "playback"),
      ...board,
      ...trackItems.filter((i) => i.cand.kind !== "playback"),
    ];
  }, [tracks, boardWanted]);

  // Main stream for single-speaker / speaker layouts AND for the
  // client-side PiP fallback. Same priority ladder as the egress page so
  // live + recording / livestream pick the same stream.
  const mainKey = useMemo(
    () => pickMainKey(items.map((i) => i.cand), presenterId, activeSpeakerId),
    [items, presenterId, activeSpeakerId],
  );
  const main = useMemo(() => items.find((i) => i.stageKey === mainKey) ?? null, [items, mainKey]);

  // Mobile gets a bespoke non-paginating grid; desktop keeps LiveKit's.
  const { isMobile, isPortrait } = useIsMobile();
  // Grid tile-shape standardization (toggled from the toolbar). "off" keeps
  // native aspect-fit; "landscape"/"portrait" render a uniform 4:3 / 3:4
  // cover-cropped grid that tiles cleanly.
  const gridAspect = usePreferences((s) => s.display.gridAspect ?? "off");

  // Grid + a shared screen (or the board / a screen share as the main
  // stream) auto-promotes to "speaker"; a layout the viewer picked for
  // themselves is taken as is.
  const hasScreenshare = items.some((i) => i.cand.kind === "screen");
  const layout: RoomLayout =
    viewLayout === "room"
      ? effectiveRoomLayout(roomLayout, main?.cand.kind ?? null, hasScreenshare)
      : viewLayout;

  // Per-viewer zoom: one stream window on the whole stage. Dropped when the
  // zoomed stream disappears, so the viewer is never stranded on a dead
  // full-bleed tile; the local view is reset when leaving the room.
  const focused = focusKey ? items.find((i) => i.tileKey === focusKey) ?? null : null;
  useEffect(() => {
    if (focusKey && !items.some((i) => i.tileKey === focusKey)) setFocus(null);
  }, [focusKey, items]);
  useEffect(() => () => resetStageView(), []);
  const focusCtx = useMemo(() => ({ focusedKey: focusKey, toggle: toggleFocus }), [focusKey]);
  const info = useMemo(() => ({ presenter: presenterId, mainKey }), [presenterId, mainKey]);

  // Meet++: whether this viewer sees the full board as the main stream (the
  // captions overlay steps aside) and whether the board is the presenter
  // (the chair's menu offers "Present the board" otherwise).
  const boardLarge =
    !compositeTrack &&
    (focused ? focused.kind === "board" : main?.kind === "board" && layout !== "grid");
  useEffect(() => {
    if (boardForPublicOnly) return;
    setStage({ boardOnStage: boardLarge, boardPresenter: boardWanted && presenterId === BOARD_KEY });
  }, [boardForPublicOnly, boardLarge, boardWanted, presenterId]);
  useEffect(
    () => () => {
      if (!boardForPublicOnly) setStage({ boardOnStage: false, boardPresenter: false });
    },
    [boardForPublicOnly],
  );

  const body = renderStage();
  return (
    <StageInfoContext.Provider value={info}>
      <GridFocusContext.Provider value={focusCtx}>
        {body}
        {!compositeTrack && items.length > 0 && (
          <ViewMenu layout={viewLayout} zoomed={!!focused} />
        )}
      </GridFocusContext.Provider>
    </StageInfoContext.Provider>
  );

  function renderStage(): ReactNode {
    // ── 1. Server composite always wins ─────────────────────────────────
    if (compositeTrack) {
      return <FullBleed track={compositeTrack} />;
    }

    // ── 2. This viewer zoomed one stream window ──────────────────────────
    if (focused) {
      return <MainView item={focused} boardVariant={boardVariant} />;
    }

    // ── 3. Client-side PiP fallback (active when pip_enabled but the
    //       compositor track hasn't landed yet) ─────────────────────────
    if (pipEnabled && main?.kind === "track") {
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
          pipOverlayTrack.participant.identity === main.track.participant.identity &&
          pipOverlayTrack.source === main.track.source
        );
      return (
        <div className="relative h-full bg-black overflow-hidden">
          <div className="absolute inset-0">
            <TrackRefContext.Provider value={main.track}>
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

    // ── 4. Playback as the main stream is full-bleed outside the grid ────
    if (main?.cand.kind === "playback" && layout !== "grid") {
      return <MainView item={main} boardVariant={boardVariant} />;
    }

    // ── 5. Grid ──────────────────────────────────────────────────────────
    if (layout === "grid") {
      const hasBoard = items.some((i) => i.kind === "board");
      return (
        <GridStageContext.Provider value={true}>
          {gridAspect !== "off" ? (
            <UniformGrid
              items={items}
              aspect={gridAspect === "landscape" ? 4 / 3 : 3 / 4}
              isMobile={isMobile}
              isPortrait={isPortrait}
              boardVariant={boardVariant}
            />
          ) : isMobile ? (
            <MobileGrid items={items} isPortrait={isPortrait} boardVariant={boardVariant} />
          ) : hasBoard ? (
            <ItemGrid items={items} boardVariant={boardVariant} />
          ) : (
            <GridLayout tracks={tracks} className="h-full p-2">
              <FlippableTile />
            </GridLayout>
          )}
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

    // ── 6. Single speaker: the main stream alone ─────────────────────────
    if (layout === "single-speaker") {
      return <MainView item={main} boardVariant={boardVariant} />;
    }

    // ── 7. Speaker: the main stream plus a strip of every other one ──────
    const others = items.filter((i) => i.tileKey !== main.tileKey);
    return (
      <div className={["flex h-full flex-col", main.kind === "board" ? "bg-slate-900" : "bg-black"].join(" ")}>
        <div className="flex-1 min-h-0 p-2">
          <div className="relative h-full">
            <div className="absolute inset-0">
              {main.kind === "board" ? (
                <MeetppBoardWindow size="main" variant={boardVariant} />
              ) : (
                <TrackRefContext.Provider value={main.track}>
                  <FlippableTile />
                </TrackRefContext.Provider>
              )}
            </div>
          </div>
        </div>
        {others.length > 0 && (
          <div
            data-testid="speaker-thumbnails"
            className="h-[20%] min-h-[120px] flex justify-center items-stretch gap-2 px-3 py-2 bg-black/40 overflow-x-auto"
          >
            {others.map((i) => (
              <div
                key={i.tileKey}
                className="aspect-video h-full flex-shrink-0 rounded-md overflow-hidden bg-primary-900"
              >
                <ItemTile item={i} size="thumb" boardVariant={boardVariant} />
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }
}

/** A stream window in a grid cell or the speaker strip. */
function ItemTile({ item, size, boardVariant }: { item: StageItem; size: "cell" | "thumb"; boardVariant: BoardVariant }) {
  if (item.kind === "board") return <MeetppBoardWindow size={size} variant={boardVariant} />;
  return (
    <TrackRefContext.Provider value={item.track}>
      <FlippableTile />
    </TrackRefContext.Provider>
  );
}

/** A stream window on the whole stage. */
function MainView({ item, boardVariant }: { item: StageItem; boardVariant: BoardVariant }) {
  if (item.kind === "board") {
    return (
      <div className="h-full bg-slate-900 p-2">
        <MeetppBoardWindow size="main" variant={boardVariant} />
      </div>
    );
  }
  return <FullBleed track={item.track} />;
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

const VIEW_OPTIONS: Array<{ value: ViewLayout; key: string; label: string }> = [
  { value: "room", key: "stage.viewRoom", label: "Room layout" },
  { value: "grid", key: "stage.viewGrid", label: "Grid" },
  { value: "speaker", key: "stage.viewSpeaker", label: "Speaker" },
  { value: "single-speaker", key: "stage.viewSingle", label: "Single speaker" },
];

/**
 * This viewer's own view: a layout of their choice instead of the room's, and
 * the way back from a zoomed stream window. Nothing here reaches anyone else.
 */
function ViewMenu({ layout, zoomed }: { layout: ViewLayout; zoomed: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const current = VIEW_OPTIONS.find((o) => o.value === layout) ?? VIEW_OPTIONS[0];
  return (
    <div className="absolute left-1/2 top-2 z-20 flex -translate-x-1/2 items-start gap-2" data-testid="stage-view-menu">
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className={[
            "inline-flex items-center gap-1.5 rounded-md bg-black/55 px-2 py-1 text-xs text-white hover:bg-black/75 transition-opacity",
            layout === "room" && !open ? "opacity-40 hover:opacity-100 focus-visible:opacity-100" : "opacity-100",
          ].join(" ")}
          title={t("stage.view", { defaultValue: "My view" })}
        >
          <LayoutGrid size={13} />
          {layout === "room" ? t("stage.view", { defaultValue: "My view" }) : t(current.key, { defaultValue: current.label })}
        </button>
        {open && (
          <div role="menu" className="absolute left-1/2 mt-1 w-48 -translate-x-1/2 overflow-hidden rounded-md bg-slate-900/95 py-1 text-sm text-white shadow-xl ring-1 ring-white/10">
            {VIEW_OPTIONS.map((o) => (
              <button
                key={o.value}
                type="button"
                role="menuitemradio"
                aria-checked={o.value === layout}
                onClick={() => {
                  setViewLayout(o.value);
                  setOpen(false);
                }}
                className={["block w-full px-3 py-1.5 text-left hover:bg-white/10", o.value === layout ? "font-semibold text-accent-300" : ""].join(" ")}
              >
                {t(o.key, { defaultValue: o.label })}
              </button>
            ))}
          </div>
        )}
      </div>
      {zoomed && (
        <button
          type="button"
          onClick={() => setFocus(null)}
          className="inline-flex items-center gap-1.5 rounded-md bg-accent-600/90 px-2 py-1 text-xs font-semibold text-white shadow hover:bg-accent-600"
          data-testid="stage-unzoom"
        >
          <Minimize2 size={13} />
          {t("stage.unzoom", { defaultValue: "Back to the room view" })}
        </button>
      )}
    </div>
  );
}

/** Desktop grid used while the board is on the stage (LiveKit's GridLayout
 * only takes track references). */
function ItemGrid({ items, boardVariant }: { items: StageItem[]; boardVariant: BoardVariant }) {
  const n = items.length;
  const cols = n <= 1 ? 1 : Math.ceil(Math.sqrt(n));
  const rows = Math.max(1, Math.ceil(n / cols));
  return (
    <div
      data-testid="item-grid"
      className="grid h-full w-full gap-2 p-2"
      style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))` }}
    >
      {items.map((i) => (
        <div key={i.tileKey} className="min-h-0 min-w-0 overflow-hidden rounded-md">
          <ItemTile item={i} size="cell" boardVariant={boardVariant} />
        </div>
      ))}
    </div>
  );
}

// Mobile grid. <=6 stream windows fill the stage (no scroll); >6 scrolls, with
// never more than 6 tiles in view. While a playlist video plays (or, failing
// that, the Meet++ board is on the stage) it is pinned at the top (always
// mounted, never scrolled away) so it stays visible in grid mode — the
// remaining participants scroll below it, each lazy-mounted so only the
// on-screen streams (plus a small preload band) stay subscribed.
//
// The outer structure is identical in every mode so tiles reconcile in place
// across the 6<->7 boundary (only the added/removed tile mounts — no full-grid
// remount / black flash).
function MobileGrid({
  items,
  isPortrait,
  boardVariant,
}: {
  items: StageItem[];
  isPortrait: boolean;
  boardVariant: BoardVariant;
}) {
  const cols = isPortrait ? 2 : 3;
  const rows = isPortrait ? 3 : 2;
  const capacity = cols * rows; // 6 in view either way
  const scrollMode = items.length > capacity;

  // Pin the playback ingress or the board (kept at index 0 by `items`) ONLY
  // when we scroll — otherwise a LazyTile would unmount it as it scrolls off.
  // In fill mode nothing scrolls, so it stays a normal always-mounted cell.
  const first = items[0];
  const pinned =
    scrollMode && first && (first.kind === "board" || first.cand.kind === "playback")
      ? first
      : null;
  const rest = pinned ? items.slice(1) : items;

  return (
    <div className="flex h-full w-full flex-col gap-2 p-2">
      {pinned && (
        <div key="pinned" className="min-h-0 basis-[40%] shrink-0">
          <ItemTile item={pinned} size="cell" boardVariant={boardVariant} />
        </div>
      )}
      <div key="grid" className="min-h-0 flex-1">
        <AdaptiveGrid items={rest} isPortrait={isPortrait} reserveRow={!!pinned} boardVariant={boardVariant} />
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
  items,
  isPortrait,
  reserveRow,
  boardVariant,
}: {
  items: StageItem[];
  isPortrait: boolean;
  reserveRow: boolean;
  boardVariant: BoardVariant;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [rowH, setRowH] = useState(0);
  const scrollCols = isPortrait ? 2 : 3;
  const visibleRows = Math.max(1, (isPortrait ? 3 : 2) - (reserveRow ? 1 : 0));
  const capacity = scrollCols * visibleRows;
  const count = items.length;
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
        {items.map((it) => (
          <LazyTile
            key={it.tileKey}
            item={it}
            root={rootRef}
            eager={!scroll}
            boardVariant={boardVariant}
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
  item,
  root,
  eager,
  boardVariant,
}: {
  item: StageItem;
  root: { current: HTMLDivElement | null };
  eager: boolean;
  boardVariant: BoardVariant;
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
        <ItemTile item={item} size="cell" boardVariant={boardVariant} />
      ) : (
        <div className="flex h-full w-full items-center justify-center rounded-md bg-primary-900/60">
          <span className="truncate px-1 text-[10px] text-slate-500">
            {item.kind === "board" ? "Meet++" : item.track.participant.name || item.track.participant.identity}
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
  items,
  aspect,
  isMobile,
  isPortrait,
  boardVariant,
}: {
  items: StageItem[];
  aspect: number;
  isMobile: boolean;
  isPortrait: boolean;
  boardVariant: BoardVariant;
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
    items.length,
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
          {items.map((it) => (
            <div
              key={it.tileKey}
              style={{ width: tileW, height: tileH }}
              className="overflow-hidden rounded-md"
            >
              <LazyTile item={it} root={rootRef} eager={!scroll} boardVariant={boardVariant} />
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

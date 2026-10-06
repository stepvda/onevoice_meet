import { useRef } from "react";
import { useTranslation } from "react-i18next";
import { LayoutDashboard } from "lucide-react";
import { useMeetpp } from "../../lib/meetpp/store";
import { fmtDuration, sectionElapsed, sectionLabel } from "../../lib/meetpp/state";
import { BOARD_KEY } from "../../lib/stage";
import { toggleFocus } from "../../lib/stageView";
import StageTileControls from "../StageTileControls";
import BoardErrorBoundary from "./BoardErrorBoundary";
import MeetppBoard from "./MeetppBoard";
import { cx, useElementWidth, useNow } from "./ui";

/** Below this width a grid cell shows the compact summary instead of the board. */
const FULL_BOARD_MIN_WIDTH = 560;

export type BoardWindowSize = "main" | "cell" | "thumb";

/**
 * The Meet++ board as a stream window on the stage (FDD §5.10, v3.2): it
 * takes part in the room layout like any camera or screen share. As the main
 * stream it is the full board; in a grid cell it is the full board when the
 * cell is wide enough, else — and in the speaker strip — a compact live
 * summary. Viewers zoom it in their own view (double-click or the zoom
 * button); the host and co-hosts can make it the presenter.
 */
export default function MeetppBoardWindow(props: { size: BoardWindowSize; variant?: "stage" | "public" | "egress" }) {
  return (
    <BoardErrorBoundary compact={props.size !== "main"}>
      <BoardWindow {...props} />
    </BoardErrorBoundary>
  );
}

function BoardWindow({
  size,
  variant = "stage",
}: {
  size: BoardWindowSize;
  variant?: "stage" | "public" | "egress";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const width = useElementWidth(ref, size === "main" ? 1280 : 320);
  const full = size === "main" || (size === "cell" && width >= FULL_BOARD_MIN_WIDTH);
  return (
    <div ref={ref} className="relative h-full w-full min-h-0 min-w-0" data-testid={`meetpp-window-${size}`}>
      {full ? (
        <>
          <MeetppBoard
            variant={variant}
            transcriptWidth={variant === "egress" ? 260 : undefined}
            className="h-full w-full"
          />
          {variant !== "egress" && size === "cell" && <StageTileControls tileKey={BOARD_KEY} stageKey={BOARD_KEY} className="right-2" />}
        </>
      ) : (
        <BoardTile controls={variant !== "egress"} />
      )}
    </div>
  );
}

/** Compact summary of the board: live section, its time, the latest change. */
export function MeetppBoardTile(props: { controls?: boolean }) {
  return (
    <BoardErrorBoundary compact>
      <BoardTile {...props} />
    </BoardErrorBoundary>
  );
}

function BoardTile({ controls = true }: { controls?: boolean }) {
  const { t } = useTranslation();
  const snap = useMeetpp((s) => s.snap);
  const lastChange = useMeetpp((s) => s.lastChange);
  const now = useNow(1000, !!snap);
  const live = snap?.sections.find((s) => s.id === snap.session.live_section_id) ?? null;
  const elapsed = live ? sectionElapsed(live, now) : 0;
  const paused = snap?.session.status === "paused";
  return (
    <div
      className={cx(
        "group relative flex h-full w-full select-none flex-col justify-center overflow-hidden rounded-md bg-slate-50 px-3 py-2 text-slate-800 ring-1 ring-blue-300",
        controls && "cursor-pointer [touch-action:manipulation]",
      )}
      onDoubleClick={controls ? () => toggleFocus(BOARD_KEY) : undefined}
      data-testid="meetpp-board-tile"
    >
      <div className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-blue-800">
        <LayoutDashboard size={12} /> Meet++
        {paused && (
          <span className="rounded bg-amber-100 px-1 text-[10px] font-semibold normal-case tracking-normal text-amber-800">
            {t("meetpp.header.paused", { defaultValue: "paused" })}
          </span>
        )}
      </div>
      <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[13px] font-semibold">
        <span className={cx("h-2 w-2 flex-shrink-0 rounded-full", paused ? "bg-amber-500" : "bg-red-600")} />
        <span className="min-w-0 flex-1 truncate">
          {live ? sectionLabel(live) : t("meetpp.header.notStarted", { defaultValue: "not started" })}
        </span>
        {live && <span className="text-[11px] font-normal tabular-nums text-slate-500">{fmtDuration(elapsed)}</span>}
      </div>
      {lastChange?.label && (
        <div className="mt-0.5 truncate text-[12px] text-slate-600">
          {t("meetpp.stage.latest", { defaultValue: "Latest: {{label}}", label: lastChange.label })}
        </div>
      )}
      {controls && <StageTileControls tileKey={BOARD_KEY} stageKey={BOARD_KEY} className="right-2" />}
    </div>
  );
}

/** Whether this viewer sees the full board as the main stream (or zoomed);
 * the captions overlay is shown only when not — the board has its own
 * transcript. */
export function useBoardVisible(): boolean {
  const onStage = useMeetpp((s) => s.boardOnStage);
  const active = useMeetpp((s) => s.active);
  return active && onStage;
}

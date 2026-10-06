import { useContext, useState } from "react";
import { useTranslation } from "react-i18next";
import { Maximize2, Minimize2, Presentation } from "lucide-react";
import { BOARD_KEY } from "../lib/stage";
import { StageActionsContext, StageInfoContext } from "../lib/stageControls";
import { toggleFocus, useStageView } from "../lib/stageView";

/**
 * Controls of one stream window (camera, screen share, playback or the
 * Meet++ board): zoom it on this viewer's stage, and — for the host and
 * co-hosts — make it the presenter for everyone. Sits in the tile's top-right
 * corner, left of the flip button.
 */
export default function StageTileControls({
  tileKey,
  stageKey,
  className = "",
}: {
  /** Local zoom key ("<identity>-<source>" or the board key). */
  tileKey: string;
  /** Room-wide presenter key (see lib/stage.ts). */
  stageKey: string;
  className?: string;
}) {
  const { t } = useTranslation();
  const actions = useContext(StageActionsContext);
  const { presenter } = useContext(StageInfoContext);
  const zoomed = useStageView((s) => s.focusKey === tileKey);
  const [busy, setBusy] = useState(false);
  const isPresenter = presenter === stageKey;

  const present = async (key: string | null) => {
    if (!actions) return;
    setBusy(true);
    try {
      await actions.present(key);
    } finally {
      setBusy(false);
    }
  };

  const btn =
    "p-1.5 rounded-md bg-black/55 hover:bg-black/75 text-white opacity-50 hover:opacity-100 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity disabled:opacity-30";

  return (
    <div className={`absolute top-2 z-10 flex items-center gap-1 ${className}`} data-testid={`tile-stage-${stageKey}`}>
      {isPresenter && (
        <span className="rounded-md bg-accent-600/90 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white">
          {t("stage.presenter", { defaultValue: "Presenter" })}
        </span>
      )}
      {actions?.canPresent && !isPresenter && (
        <button
          type="button"
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation();
            void present(stageKey);
          }}
          className={btn}
          title={t("stage.present", { defaultValue: "Present to everyone" })}
          aria-label={t("stage.present", { defaultValue: "Present to everyone" })}
          data-testid={`tile-present-${stageKey}`}
        >
          <Presentation size={14} />
        </button>
      )}
      {actions?.canPresent && isPresenter && stageKey !== BOARD_KEY && (
        <button
          type="button"
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation();
            void present(null);
          }}
          className={`${btn} ring-2 ring-accent-500`}
          title={t("stage.stopPresenting", { defaultValue: "Stop presenting" })}
          aria-label={t("stage.stopPresenting", { defaultValue: "Stop presenting" })}
        >
          <Presentation size={14} />
        </button>
      )}
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          toggleFocus(tileKey);
        }}
        className={btn}
        aria-pressed={zoomed}
        title={zoomed ? t("stage.unzoom", { defaultValue: "Back to the room view" }) : t("stage.zoom", { defaultValue: "Zoom in my view" })}
        aria-label={zoomed ? t("stage.unzoom", { defaultValue: "Back to the room view" }) : t("stage.zoom", { defaultValue: "Zoom in my view" })}
        data-testid={`tile-zoom-${stageKey}`}
      >
        {zoomed ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
      </button>
    </div>
  );
}

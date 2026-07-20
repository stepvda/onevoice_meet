import { useTranslation } from "react-i18next";
import { RectangleHorizontal, RectangleVertical, Proportions } from "lucide-react";
import { usePreferences } from "../lib/preferences";

/**
 * Tri-state grid tile-shape toggle, rendered in the top toolbar (grid layout
 * only). Cycles native -> 4:3 landscape -> 3:4 portrait. The value is a
 * per-viewer display preference read by every FlippableTile via zustand, so it
 * only affects the local viewer's grid. No aria-pressed — it's a 3-way control,
 * so the descriptive aria-label carries the state instead.
 */
export default function GridAspectToggle() {
  const { t } = useTranslation();
  const gridAspect = usePreferences((s) => s.display.gridAspect ?? "off");
  const setDisplay = usePreferences((s) => s.setDisplay);
  const next =
    gridAspect === "off"
      ? "landscape"
      : gridAspect === "landscape"
      ? "portrait"
      : "off";
  const Icon =
    gridAspect === "landscape"
      ? RectangleHorizontal
      : gridAspect === "portrait"
      ? RectangleVertical
      : Proportions;
  const short =
    gridAspect === "landscape"
      ? "4:3"
      : gridAspect === "portrait"
      ? "3:4"
      : t("grid.aspectOffShort", { defaultValue: "Native" });
  const label =
    gridAspect === "landscape"
      ? t("grid.aspectLandscape", { defaultValue: "Tiles: 4:3" })
      : gridAspect === "portrait"
      ? t("grid.aspectPortrait", { defaultValue: "Tiles: 3:4" })
      : t("grid.aspectOff", { defaultValue: "Tiles: native" });
  const on = gridAspect !== "off";
  return (
    <button
      type="button"
      onClick={() => setDisplay({ gridAspect: next })}
      data-testid="grid-aspect-toggle"
      title={t("grid.aspectAria", {
        defaultValue: "{{label}} — click to change tile shape",
        label,
      })}
      aria-label={t("grid.aspectAria", {
        defaultValue: "{{label}} — click to change tile shape",
        label,
      })}
      className={[
        "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
        on
          ? "bg-accent-500 text-white hover:bg-accent-600"
          : "bg-primary-700 text-slate-100 hover:bg-primary-600",
      ].join(" ")}
    >
      <Icon size={16} />
      <span className="hidden sm:inline">{short}</span>
    </button>
  );
}

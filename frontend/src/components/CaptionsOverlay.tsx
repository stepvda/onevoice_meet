import { useTranslation } from "react-i18next";
import type { FontSize } from "../lib/preferences";

/**
 * Placeholder overlay shown when `accessibility.liveCaptions` is on. A real
 * captioning pipeline (ASR or per-speaker transcripts) doesn't ship yet — the
 * overlay confirms the toggle is reactive and reserves the visual space.
 */
export default function CaptionsOverlay({ fontSize }: { fontSize: FontSize }) {
  const { t } = useTranslation();
  return (
    <div
      data-testid="captions-overlay"
      className={`captions-overlay ${fontSize}`}
    >
      {t("captions.unavailable")}
    </div>
  );
}

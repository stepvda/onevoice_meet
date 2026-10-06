import { useMeetpp } from "../lib/meetpp/store";
import { useBoardVisible } from "./meetpp/MeetppStage";
import { useNow } from "./meetpp/ui";
import type { FontSize } from "../lib/preferences";

const CAPTION_TTL_MS = 8000;

/**
 * Accessibility captions fed by the Meet++ caption messages (tier 1, then
 * replaced by tier-2 text). Shown only when the Meet++ board is NOT on screen
 * (e.g. during a screen share) — the board's transcript column is the
 * transcript otherwise (FDD §5.6). The last two recent lines are shown.
 */
export default function CaptionsOverlay({ fontSize }: { fontSize: FontSize }) {
  const captions = useMeetpp((s) => s.captions);
  const active = useMeetpp((s) => s.active);
  const boardVisible = useBoardVisible();
  const now = useNow(1000, active && !boardVisible && captions.length > 0);
  if (!active || boardVisible) return null;
  const visible = captions.filter((c) => now - c.at < CAPTION_TTL_MS).slice(-2);
  if (visible.length === 0) return null;
  return (
    <div data-testid="captions-overlay" className={`captions-overlay ${fontSize}`} aria-live="off">
      {visible.map((c) => (
        <div key={c.seq} className="caption-line">
          <span className="caption-speaker">{c.name}: </span>
          <span>{c.text}</span>
        </div>
      ))}
    </div>
  );
}

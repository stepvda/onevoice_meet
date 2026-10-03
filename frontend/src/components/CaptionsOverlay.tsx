import { useMeetpp } from "../lib/meetpp/store";
import type { FontSize } from "../lib/preferences";

/**
 * Live captions fed by the Meet++ caption messages. The last two lines per
 * speaker are shown and fade after a few seconds. When no Meet++ session is
 * active the overlay renders nothing (the old placeholder is gone now that a
 * real STT pipeline exists).
 */
export default function CaptionsOverlay({ fontSize }: { fontSize: FontSize }) {
  const captions = useMeetpp((s) => s.captions);
  const active = useMeetpp((s) => s.active);
  if (!active || captions.length === 0) return null;
  const visible = captions.slice(-2);
  return (
    <div
      data-testid="captions-overlay"
      className={`captions-overlay ${fontSize}`}
      aria-live="off"
    >
      {visible.map((c) => (
        <div key={`${c.seq}`} className="caption-line">
          <span className="caption-speaker">{c.name}: </span>
          <span>{c.text}</span>
        </div>
      ))}
    </div>
  );
}

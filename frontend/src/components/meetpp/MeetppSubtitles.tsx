import { useEffect, useRef, useState } from "react";
import { useMeetpp } from "../../lib/meetpp/store";
import { usePreferences } from "../../lib/preferences";

interface Item {
  id: string;
  name: string;
  text: string;
  durationMs: number;
}

const GAP = 28;
const MAX_ITEMS = 12;
const DEFAULT_CHARS_PER_SEC = 14;

/**
 * Live subtitles: recognized utterances enter from the right and scroll left
 * at a speed matched to the spoken rate. The speed for the newest utterance
 * is `textWidth / utteranceDuration`, so its characters pass at roughly the
 * pace they were spoken. Respects prefers-reduced-motion (static last line).
 */
export default function MeetppSubtitles() {
  const active = useMeetpp((s) => s.active);
  // Use the live-only stream so a full-state backfill never replays history.
  const transcript = useMeetpp((s) => s.liveTranscript);
  const reduced = usePreferences((s) => s.accessibility.reducedMotion);
  const [items, setItems] = useState<Item[]>([]);
  const stripRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const xRef = useRef(0);
  const speedRef = useRef(140);
  const lastSeqRef = useRef(0);
  const startedRef = useRef(false);

  // Append new transcript segments as ticker items.
  useEffect(() => {
    if (!active) return;
    const fresh = transcript.filter((s) => s.seq > lastSeqRef.current && s.text?.trim());
    if (fresh.length === 0) return;
    lastSeqRef.current = transcript[transcript.length - 1]?.seq ?? lastSeqRef.current;
    setItems((prev) => {
      const combined = [
        ...prev,
        ...fresh.map((s) => ({
          id: `s${s.seq}`,
          name: s.name ?? s.identity,
          text: s.text,
          durationMs:
            s.duration_ms && s.duration_ms > 300
              ? s.duration_ms
              : Math.max(1200, (s.text.length / DEFAULT_CHARS_PER_SEC) * 1000),
        })),
      ];
      if (combined.length > MAX_ITEMS) {
        const removed = combined.length - MAX_ITEMS;
        const strip = stripRef.current;
        if (strip) {
          let w = 0;
          for (let i = 0; i < removed && i < strip.children.length; i++) {
            w += (strip.children[i] as HTMLElement).offsetWidth + GAP;
          }
          // Keep the remaining items visually stable after pruning.
          xRef.current += w;
        }
        return combined.slice(-MAX_ITEMS);
      }
      return combined;
    });
  }, [transcript, active]);

  // Set the scroll speed from the newest utterance (speech-paced).
  useEffect(() => {
    const strip = stripRef.current;
    const container = containerRef.current;
    if (!strip || !container || items.length === 0) return;
    if (!startedRef.current) {
      xRef.current = container.clientWidth;
      startedRef.current = true;
    }
    const last = strip.lastElementChild as HTMLElement | null;
    if (last) {
      const w = last.offsetWidth + GAP;
      const durationS = Math.max(0.4, (items[items.length - 1].durationMs || 2000) / 1000);
      speedRef.current = Math.min(600, Math.max(60, w / durationS));
    }
  }, [items]);

  // Reset when the session is not active.
  useEffect(() => {
    if (!active) {
      setItems([]);
      startedRef.current = false;
      xRef.current = 0;
      lastSeqRef.current = 0;
    }
  }, [active]);

  // Conveyor animation.
  useEffect(() => {
    if (!active || reduced) return;
    let raf = 0;
    let prev = performance.now();
    const step = (t: number) => {
      const dt = Math.min(0.1, (t - prev) / 1000);
      prev = t;
      xRef.current -= speedRef.current * dt;
      const strip = stripRef.current;
      if (strip) strip.style.transform = `translateX(${xRef.current}px)`;
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [active, reduced]);

  if (!active || items.length === 0) return null;

  if (reduced) {
    const last = items[items.length - 1];
    return (
      <div className="pointer-events-none fixed inset-x-0 bottom-24 z-30 flex justify-center px-4">
        <div className="max-w-3xl rounded-lg bg-black/70 px-4 py-2 text-center text-lg text-white">
          <span className="mr-2 text-sm text-accent-400">{last.name}</span>
          {last.text}
        </div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      data-testid="meetpp-subtitles"
      className="pointer-events-none fixed inset-x-0 bottom-24 z-30 h-12 overflow-hidden"
    >
      <div
        ref={stripRef}
        className="absolute left-0 top-0 flex h-full items-center whitespace-nowrap will-change-transform"
        style={{ transform: `translateX(${xRef.current}px)` }}
      >
        {items.map((it) => (
          <div key={it.id} className="flex h-full items-center" style={{ marginRight: GAP }}>
            <span className="mr-2 text-sm font-semibold text-accent-400">{it.name}</span>
            <span className="text-2xl text-white drop-shadow-[0_2px_4px_rgba(0,0,0,0.9)]">{it.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

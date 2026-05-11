import { useEffect } from "react";
import { usePreferences } from "./preferences";

/**
 * When `accessibility.monoAudio` is on, force every <audio> and <video>
 * element on the page to render mono by routing through a single-channel
 * MergerNode via Web Audio. Tracks captureStream from LiveKit are <audio>
 * elements injected by `RoomAudioRenderer`.
 */
export function useMonoAudio() {
  const enabled = usePreferences((s) => s.accessibility.monoAudio);
  useEffect(() => {
    if (!enabled) return;
    const w = window as unknown as {
      AudioContext?: typeof AudioContext;
      webkitAudioContext?: typeof AudioContext;
    };
    const Ctx = w.AudioContext ?? w.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const observed = new WeakSet<HTMLMediaElement>();
    const teardown: Array<() => void> = [];

    const wire = (el: HTMLMediaElement) => {
      if (observed.has(el)) return;
      observed.add(el);
      try {
        const src = ctx.createMediaElementSource(el);
        const merger = ctx.createChannelMerger(2);
        // Send the same mono mix to both output channels.
        src.connect(merger, 0, 0);
        src.connect(merger, 0, 1);
        merger.connect(ctx.destination);
        teardown.push(() => {
          try {
            src.disconnect();
            merger.disconnect();
          } catch {
            /* ignore */
          }
        });
      } catch {
        /* element may already be wired or cross-origin — ignore */
      }
    };

    const scan = () => document.querySelectorAll<HTMLMediaElement>("audio, video").forEach(wire);
    scan();
    const obs = new MutationObserver(scan);
    obs.observe(document.body, { subtree: true, childList: true });
    teardown.push(() => obs.disconnect());
    teardown.push(() => {
      void ctx.close();
    });

    return () => {
      for (const fn of teardown) fn();
    };
  }, [enabled]);
}

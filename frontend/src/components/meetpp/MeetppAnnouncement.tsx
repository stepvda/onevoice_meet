import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMeetpp } from "../../lib/meetpp/store";

/**
 * Big-text phase announcement (FDD Figure 4): a blue phase badge, the title,
 * a subtitle, a progress bar and "spoken via TTS · closes in N s", over a
 * dimmed stage. Click or Esc dismisses it locally.
 */
export default function MeetppAnnouncement() {
  const { t } = useTranslation();
  const announcement = useMeetpp((s) => s.announcement);
  const setAnnouncement = useMeetpp((s) => s.setAnnouncement);
  const phaseIndex = useMeetpp((s) => s.session?.phase_index ?? 1);
  const [progress, setProgress] = useState(0);

  const durationMs = announcement ? Math.max(2500, (announcement.duration_ms || 2500) + 4000) : 0;

  useEffect(() => {
    if (!announcement) return;
    setProgress(0);
    const raf = requestAnimationFrame(() => setProgress(100));
    const timer = window.setTimeout(() => setAnnouncement(null), durationMs);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setAnnouncement(null);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(timer);
      window.removeEventListener("keydown", onKey);
    };
  }, [announcement, durationMs, setAnnouncement]);

  if (!announcement) return null;

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/70"
      role="status"
      aria-live="polite"
      onClick={() => setAnnouncement(null)}
    >
      <div className="flex max-w-3xl flex-col items-center px-6 text-center">
        <div className="mb-5 grid h-16 w-16 place-items-center rounded-full bg-blue-600 text-3xl font-bold text-white shadow-lg">
          {phaseIndex}
        </div>
        <div className="text-4xl font-bold text-white sm:text-5xl">{announcement.title}</div>
        {announcement.subtitle && <div className="mt-3 text-lg text-slate-300">{announcement.subtitle}</div>}
        <div className="mt-6 h-1.5 w-72 overflow-hidden rounded-full bg-white/20">
          <div
            className="h-full rounded-full bg-blue-500 transition-[width] ease-linear"
            style={{ width: `${progress}%`, transitionDuration: `${durationMs}ms` }}
          />
        </div>
        <div className="mt-2 text-sm text-slate-400">
          {t("meetpp.announce.spoken", { defaultValue: "spoken via TTS · closes in {{s}} s", s: Math.round(durationMs / 1000) })}
        </div>
      </div>
    </div>
  );
}

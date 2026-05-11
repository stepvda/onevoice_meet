import { useRef, useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Smile } from "lucide-react";
import { useRoomContext } from "@livekit/components-react";
import { broadcastReaction } from "./FloatingReactions";
import { usePreferences } from "../lib/preferences";

const EMOJIS = ["👏", "❤️", "😂", "🎉", "👍", "👎", "🤔", "😮", "👋", "🙏"];

export default function ReactionsButton() {
  const { t } = useTranslation();
  const room = useRoomContext();
  const enabled = usePreferences((s) => s.meetingDefaults.enableReactions);
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // Close popover on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function send(emoji: string) {
    setOpen(false);
    // Show our own reaction locally even though we won't receive it on the
    // data channel.
    window.dispatchEvent(
      new CustomEvent("meet:local-reaction", {
        detail: {
          emoji,
          name: room.localParticipant.name || room.localParticipant.identity || "",
        },
      }),
    );
    try {
      await broadcastReaction(room, emoji);
    } catch {
      /* offline / reliability=lossy — fine to drop */
    }
  }

  if (!enabled) return null;
  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="btn-reactions"
        aria-haspopup="menu"
        aria-expanded={open ? "true" : "false"}
        aria-label={t("reactions.toolbar")}
        title={t("reactions.toolbarTitle")}
        className={[
          "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
          open
            ? "bg-primary-500 text-white"
            : "bg-primary-700 text-slate-100 hover:bg-primary-600",
        ].join(" ")}
      >
        <Smile size={16} />
        <span className="hidden md:inline">{t("reactions.toolbar")}</span>
      </button>
      {open && (
        <div
          role="menu"
          data-testid="reactions-popover"
          className="absolute top-full mt-1 right-0 z-40 flex gap-1 px-2 py-1.5 rounded-lg bg-primary-900 border border-primary-700 shadow-lg"
        >
          {EMOJIS.map((e) => (
            <button
              key={e}
              type="button"
              onClick={() => void send(e)}
              data-testid={`reaction-${e}`}
              className="text-2xl hover:scale-125 transition-transform px-1"
              title={t("reactions.send", { emoji: e })}
              aria-label={t("reactions.send", { emoji: e })}
            >
              {e}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

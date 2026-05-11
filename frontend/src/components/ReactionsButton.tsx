import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  // Anchor coordinates for the portal popover. Set on open and on
  // scroll/resize so the popover follows the button.
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);

  function reposition() {
    const btn = buttonRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    setAnchor({
      top: r.bottom + 4,
      right: window.innerWidth - r.right,
    });
  }

  useLayoutEffect(() => {
    if (!open) return;
    reposition();
    const onScroll = () => reposition();
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", reposition);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open]);

  // Outside-click / Escape close. Now uses both refs because the popover
  // is rendered via a portal (not a DOM child of the button wrapper).
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (buttonRef.current?.contains(t)) return;
      if (popRef.current?.contains(t)) return;
      setOpen(false);
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
      /* lossy delivery — fine to drop */
    }
  }

  if (!enabled) return null;

  return (
    <>
      <button
        ref={buttonRef}
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
      {open && anchor && typeof document !== "undefined" &&
        createPortal(
          <div
            ref={popRef}
            data-testid="reactions-popover"
            className="fixed z-[1000] flex gap-1 px-2 py-1.5 rounded-lg bg-primary-900 border border-primary-700 shadow-xl"
            style={{ top: anchor.top, right: anchor.right }}
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
          </div>,
          document.body,
        )}
    </>
  );
}

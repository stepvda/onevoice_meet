import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Mic, MicOff } from "lucide-react";
import { usePreferences } from "../lib/preferences";

/**
 * Bottom-of-stage indicator visible only when `av.pushToTalk` is enabled.
 * Reflects whether the PTT key is currently held (mic open) or released.
 */
export default function PushToTalkIndicator() {
  const { t } = useTranslation();
  const enabled = usePreferences((s) => s.av.pushToTalk);
  const key = usePreferences((s) => s.av.pushToTalkKey);
  const [holding, setHolding] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    const matches = (e: KeyboardEvent) =>
      key === "Space" || key === " " ? e.code === "Space" : e.key.toLowerCase() === key.toLowerCase();
    const down = (e: KeyboardEvent) => matches(e) && setHolding(true);
    const up = (e: KeyboardEvent) => matches(e) && setHolding(false);
    const blur = () => setHolding(false);
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", blur);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", blur);
    };
  }, [enabled, key]);

  if (!enabled) return null;
  return (
    <div
      data-testid="ptt-indicator"
      data-holding={holding ? "true" : "false"}
      className={[
        "absolute bottom-3 left-1/2 -translate-x-1/2 z-20 px-3 py-1.5 rounded-full text-xs font-medium pointer-events-none",
        "flex items-center gap-2 border",
        holding
          ? "bg-accent-500 text-white border-accent-500"
          : "bg-black/55 text-slate-100 border-slate-700",
      ].join(" ")}
    >
      {holding ? <Mic size={14} /> : <MicOff size={14} />}
      {holding ? t("ptt.live") : t("ptt.holdToTalk", { key })}
    </div>
  );
}

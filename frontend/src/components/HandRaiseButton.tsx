import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Hand } from "lucide-react";
import { useToggleHandRaise } from "../lib/handRaise";

export default function HandRaiseButton() {
  const { t } = useTranslation();
  const { raised, toggle } = useToggleHandRaise();
  const [busy, setBusy] = useState(false);
  const onClick = async () => {
    setBusy(true);
    try {
      await toggle();
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      data-testid="btn-hand-raise"
      aria-pressed={raised ? "true" : "false"}
      aria-label={raised ? t("hand.lower") : t("hand.raise")}
      title={raised ? t("hand.lowerTitle") : t("hand.raiseTitle")}
      className={[
        "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
        raised
          ? "bg-amber-500 text-white hover:bg-amber-600"
          : "bg-primary-700 text-slate-100 hover:bg-primary-600",
        "disabled:opacity-50",
      ].join(" ")}
    >
      <Hand size={16} />
      <span className="hidden md:inline">
        {raised ? t("hand.raised") : t("hand.raise")}
      </span>
    </button>
  );
}

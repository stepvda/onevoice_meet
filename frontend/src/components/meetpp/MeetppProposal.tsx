import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMeetpp } from "../../lib/meetpp/store";

const LABELS: Record<string, string> = {
  opening: "1 · Opening",
  previous_actions: "2 · Previous actions",
  agenda: "3 · Agenda",
  discussion: "4 · Discussion",
  aob: "5 · Other business",
  new_actions: "6 · New actions",
  closing: "7 · Closing",
};

/**
 * Chair-only proposal card (FDD Figure 4): "AI suggests: move to …", the
 * reason and confidence, Move now / Not yet, and the auto-advance countdown.
 */
export default function MeetppProposal({ onAccept, onReject }: { onAccept: (p: { to: string; item_id: string | null }) => void; onReject: (p: { to: string; item_id: string | null }) => void }) {
  const { t } = useTranslation();
  const proposal = useMeetpp((s) => s.proposal);
  const [secs, setSecs] = useState<number | null>(null);

  useEffect(() => {
    if (!proposal?.auto_at) {
      setSecs(null);
      return;
    }
    const deadline = new Date(proposal.auto_at).getTime();
    const tick = () => setSecs(Math.max(0, Math.round((deadline - Date.now()) / 1000)));
    tick();
    const timer = window.setInterval(tick, 500);
    return () => window.clearInterval(timer);
  }, [proposal]);

  if (!proposal || proposal.to === "discussion:timebox") return null;

  const target = proposal.to === "discussion:next_item" ? "4 · Discussion — next item" : LABELS[proposal.to] ?? proposal.to;

  return (
    <div className="fixed right-4 top-20 z-40 w-[22rem] rounded-xl border border-slate-200 bg-white p-4 shadow-2xl">
      <div className="text-base font-semibold text-slate-900">
        {t("meetpp.proposal.title", { defaultValue: "AI suggests: move to" })} “{target}”
      </div>
      <div className="mt-1 text-sm text-slate-500">
        {proposal.reason}
        {proposal.confidence ? ` · ${t("meetpp.proposal.confidence", { defaultValue: "Confidence" })} ${proposal.confidence.toFixed(2)}` : ""}
      </div>
      <div className="mt-3 flex items-center gap-2">
        <button
          className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700"
          onClick={() => onAccept({ to: proposal.to, item_id: proposal.item_id })}
        >
          {t("meetpp.proposal.moveNow", { defaultValue: "Move now" })}
        </button>
        <button
          className="rounded-lg border border-slate-300 bg-slate-100 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-200"
          onClick={() => onReject({ to: proposal.to, item_id: proposal.item_id })}
        >
          {t("meetpp.proposal.notYet", { defaultValue: "Not yet" })}
        </button>
        {secs !== null && (
          <span className="ml-auto text-sm font-medium text-amber-600">
            {t("meetpp.proposal.autoIn", { defaultValue: "auto in {{s}} s", s: secs })}
          </span>
        )}
      </div>
      <div className="mt-2 text-xs italic text-slate-400">
        {t("meetpp.proposal.visible", { defaultValue: "Visible to chair and co-hosts only" })}
      </div>
    </div>
  );
}

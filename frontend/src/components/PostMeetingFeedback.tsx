import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Star, X } from "lucide-react";
import { api } from "../lib/api";

interface Props {
  roomName: string;
  participantIdentity?: string | null;
  participantName?: string | null;
  onClose: () => void;
}

/**
 * Lightweight 0–10 NPS-style feedback prompt shown once after the user
 * disconnects from a meeting. Submission is fire-and-forget; the user can
 * skip without rating. The dedupe flag in sessionStorage prevents the modal
 * coming back if the user reconnects within the same browser session.
 */
export default function PostMeetingFeedback({
  roomName,
  participantIdentity,
  participantName,
  onClose,
}: Props) {
  const { t } = useTranslation();
  const [rating, setRating] = useState<number | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function submit() {
    if (rating === null) return;
    setBusy(true);
    try {
      await api.postFeedback(roomName, {
        rating,
        comment: comment.trim() || undefined,
        participant_identity: participantIdentity ?? null,
        participant_name: participantName ?? null,
      });
      setDone(true);
      window.setTimeout(onClose, 900);
    } catch {
      /* fire and forget — closing is still fine */
      onClose();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      data-testid="feedback-modal"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
    >
      <div className="bg-primary-900 border border-primary-700 rounded-2xl shadow-xl max-w-md w-full p-5">
        <div className="flex items-start justify-between">
          <h2 className="text-lg font-semibold text-slate-50">
            {t("feedback.title", { defaultValue: "How was the meeting?" })}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("feedback.skip", { defaultValue: "Skip" })}
            className="p-1 rounded hover:bg-primary-800 text-slate-400"
          >
            <X size={18} />
          </button>
        </div>
        {!done && (
          <>
            <p className="text-sm text-slate-400 mt-1">
              {t("feedback.body", {
                defaultValue: "On a scale of 0 (worst) to 10 (best), how would you rate this meeting?",
              })}
            </p>
            <div className="mt-3 grid grid-cols-11 gap-1">
              {Array.from({ length: 11 }).map((_, i) => (
                <button
                  key={i}
                  type="button"
                  onClick={() => setRating(i)}
                  data-testid={`feedback-rating-${i}`}
                  aria-pressed={rating === i ? "true" : "false"}
                  className={[
                    "h-9 rounded font-medium text-sm",
                    rating === i
                      ? "bg-accent-500 text-white"
                      : "bg-primary-800 text-slate-200 hover:bg-primary-700",
                  ].join(" ")}
                >
                  {i}
                </button>
              ))}
            </div>
            <label htmlFor="feedback-comment" className="block text-xs text-slate-400 mt-3 mb-1">
              {t("feedback.commentLabel", { defaultValue: "Anything else? (optional)" })}
            </label>
            <textarea
              id="feedback-comment"
              data-testid="feedback-comment"
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              maxLength={2000}
              rows={3}
              className="w-full px-3 py-2 rounded-lg bg-primary-800 text-slate-100 border border-primary-700 text-sm resize-y focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
            <div className="mt-4 flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-300 hover:bg-primary-800"
              >
                {t("feedback.skip", { defaultValue: "Skip" })}
              </button>
              <button
                type="button"
                onClick={() => void submit()}
                disabled={rating === null || busy}
                data-testid="feedback-submit"
                className="px-3 py-1.5 rounded-lg text-sm font-medium bg-accent-500 hover:bg-accent-600 text-white disabled:opacity-50"
              >
                {busy
                  ? t("feedback.sending", { defaultValue: "Sending…" })
                  : t("feedback.submit", { defaultValue: "Submit" })}
              </button>
            </div>
          </>
        )}
        {done && (
          <p className="mt-3 text-sm text-accent-400 flex items-center gap-2">
            <Star size={16} /> {t("feedback.thanks", { defaultValue: "Thanks for your feedback!" })}
          </p>
        )}
      </div>
    </div>
  );
}

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Mail, Send, X } from "lucide-react";
import { api } from "../lib/api";
import { fetchOneWityskName } from "../lib/auth";
import { Button, Card, Field } from "./ui";

interface Props {
  meetingId: string;
  meetingTitle?: string;
  open: boolean;
  onClose: () => void;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function InviteModal({ meetingId, meetingTitle, open, onClose }: Props) {
  const { t } = useTranslation();
  const [emailsInput, setEmailsInput] = useState("");
  const [personal, setPersonal] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<
    | { ok: boolean; sent: number; failed: string[]; join_url: string }
    | null
  >(null);
  const [err, setErr] = useState<string | null>(null);

  if (!open) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setResult(null);
    const list = emailsInput
      .split(/[\s,;]+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    const valid = list.filter((s) => EMAIL_RE.test(s));
    const invalid = list.filter((s) => !EMAIL_RE.test(s));
    if (valid.length === 0) {
      setErr(t("invite.validNeeded"));
      return;
    }
    if (invalid.length > 0) {
      setErr(t("invite.invalidList", { list: invalid.join(", ") }));
      return;
    }
    if (valid.length > 20) {
      setErr(t("invite.tooMany"));
      return;
    }
    setBusy(true);
    try {
      const r = await api.invite(meetingId, {
        emails: valid,
        personal_message: personal.trim() || undefined,
        display_name: await fetchOneWityskName(),
      });
      setResult(r);
      if (r.sent === valid.length) {
        setEmailsInput("");
        setPersonal("");
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      data-testid="invite-modal"
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 backdrop-blur-sm pt-16 px-4"
      role="dialog"
      aria-modal="true"
      aria-label={t("invite.title")}
    >
      <Card className="w-full max-w-lg relative">
        <button
          type="button"
          onClick={onClose}
          aria-label={t("invite.close")}
          data-testid="invite-close"
          className="absolute top-3 right-3 p-1 rounded-md text-slate-300 hover:bg-primary-700"
        >
          <X size={18} />
        </button>
        <h2 className="text-lg font-semibold mb-2 flex items-center gap-2 text-slate-50">
          <Mail size={18} className="text-accent-500" />
          {t("invite.title")}
        </h2>
        {meetingTitle && (
          <p className="text-sm text-slate-400 mb-4">
            {t("invite.meetingLabel")} <span className="text-slate-200">{meetingTitle}</span>
          </p>
        )}

        <form onSubmit={submit} className="flex flex-col gap-4">
          <Field
            id="invite-emails"
            label={t("invite.emailsLabel")}
            hint={t("invite.emailsHint")}
          >
            <textarea
              id="invite-emails"
              data-testid="invite-emails"
              value={emailsInput}
              onChange={(e) => setEmailsInput(e.target.value)}
              rows={3}
              required
              placeholder={t("invite.emailsPlaceholder")}
              className="w-full px-3 py-2 rounded-lg bg-primary-900/60 text-slate-100 placeholder:text-slate-500 border border-primary-700 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-primary-400"
            />
          </Field>
          <Field
            id="invite-message"
            label={t("invite.messageLabel")}
            hint={t("invite.messageHint")}
          >
            <textarea
              id="invite-message"
              data-testid="invite-message"
              value={personal}
              onChange={(e) => setPersonal(e.target.value)}
              rows={3}
              maxLength={1000}
              placeholder={t("invite.messagePlaceholder")}
              className="w-full px-3 py-2 rounded-lg bg-primary-900/60 text-slate-100 placeholder:text-slate-500 border border-primary-700 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-primary-400"
            />
          </Field>

          <div className="flex items-center gap-3">
            <Button type="submit" disabled={busy} data-testid="invite-submit">
              <Send size={16} />
              {busy ? t("invite.sending") : t("invite.submit")}
            </Button>
            <Button type="button" variant="ghost" onClick={onClose}>
              {t("invite.cancel")}
            </Button>
          </div>

          {err && (
            <div data-testid="invite-error" className="text-red-400 text-sm">
              {err}
            </div>
          )}
          {result && (
            <div
              data-testid="invite-result"
              className={
                result.failed.length === 0
                  ? "text-accent-500 text-sm"
                  : "text-amber-400 text-sm"
              }
            >
              {t("invite.sentN", { count: result.sent })}
              {result.failed.length > 0 && (
                <>{t("invite.failedSuffix", { list: "" })}<code>{result.failed.join(", ")}</code></>
              )}
            </div>
          )}
        </form>
      </Card>
    </div>
  );
}

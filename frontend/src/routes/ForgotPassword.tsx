import { useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Mail } from "lucide-react";
import { api } from "../lib/api";
import { Button, Card, Field, Input } from "../components/ui";

export default function ForgotPassword() {
  const { t } = useTranslation();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.requestPasswordReset(email.trim());
      setDone(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-4 lg:p-8 max-w-md mx-auto">
      <Card>
        <h1 className="text-2xl font-bold text-slate-50 mb-1 flex items-center gap-2">
          <Mail size={22} className="text-accent-500" />
          {t("forgot.title", { defaultValue: "Reset your password" })}
        </h1>
        <p className="text-sm text-slate-400 mb-4">
          {t("forgot.intro", {
            defaultValue:
              "Enter the email address you used to sign up. If we find a matching account we'll send you a password-reset link valid for 30 minutes.",
          })}
        </p>

        {done ? (
          <div data-testid="forgot-done">
            <p className="text-sm text-accent-500">
              {t("forgot.done", {
                defaultValue:
                  "If that email is registered, a reset link is on its way. Check your inbox (and spam folder).",
              })}
            </p>
            <p className="text-xs text-slate-500 mt-3">
              <Link to="/login" className="text-accent-500 hover:underline">
                {t("forgot.backToLogin", { defaultValue: "Back to sign in" })}
              </Link>
            </p>
          </div>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-3">
            <Field id="fp-email" label={t("auth.email", { defaultValue: "Email" })}>
              <Input
                id="fp-email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                data-testid="forgot-email"
              />
            </Field>
            {err && <div className="text-sm text-red-400">{err}</div>}
            <div className="flex items-center gap-3">
              <Button type="submit" disabled={busy} data-testid="forgot-submit">
                {busy ? t("forgot.sending", { defaultValue: "Sending…" }) : t("forgot.submit", { defaultValue: "Send reset link" })}
              </Button>
              <Link to="/login" className="text-sm text-accent-500 hover:underline">
                {t("forgot.cancel", { defaultValue: "Cancel" })}
              </Link>
            </div>
          </form>
        )}
      </Card>
    </div>
  );
}

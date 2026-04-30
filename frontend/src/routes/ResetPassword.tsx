import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { KeyRound } from "lucide-react";
import { api } from "../lib/api";
import { Button, Card, Field, Input } from "../components/ui";
import PasswordStrengthIndicator from "../components/PasswordStrengthIndicator";

/**
 * /reset-password#token=… — the email link drops the token in the fragment
 * (so it never lands in server logs) and we read it from window.location
 * once on mount.
 */
export default function ResetPassword() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [token, setToken] = useState<string | null>(null);
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const hash = window.location.hash.replace(/^#/, "");
    const params = new URLSearchParams(hash);
    const t = params.get("token");
    setToken(t);
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setErr(null);
    try {
      await api.confirmPasswordReset(token, pw);
      setDone(true);
      // Send the user to the login page after a short read.
      setTimeout(() => navigate("/login", { replace: true }), 1500);
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
          <KeyRound size={22} className="text-accent-500" />
          {t("reset.title", { defaultValue: "Choose a new password" })}
        </h1>
        {!token ? (
          <p className="text-sm text-red-400 mt-2" data-testid="reset-no-token">
            {t("reset.missingToken", {
              defaultValue: "This page must be opened from the link in the password-reset email.",
            })}
          </p>
        ) : done ? (
          <p className="text-sm text-accent-500 mt-2" data-testid="reset-done">
            {t("reset.done", { defaultValue: "Password updated. Redirecting to sign-in…" })}
          </p>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-3 mt-3">
            <Field id="rp-pw" label={t("reset.newPassword", { defaultValue: "New password" })}>
              <Input
                id="rp-pw"
                type="password"
                autoComplete="new-password"
                minLength={8}
                required
                value={pw}
                onChange={(e) => setPw(e.target.value)}
                data-testid="reset-password"
              />
              <PasswordStrengthIndicator password={pw} />
            </Field>
            {err && <div className="text-sm text-red-400">{err}</div>}
            <div className="flex items-center gap-3">
              <Button type="submit" disabled={busy || !pw} data-testid="reset-submit">
                {busy ? t("reset.updating", { defaultValue: "Updating…" }) : t("reset.submit", { defaultValue: "Update password" })}
              </Button>
              <Link to="/login" className="text-sm text-accent-500 hover:underline">
                {t("reset.cancel", { defaultValue: "Cancel" })}
              </Link>
            </div>
          </form>
        )}
      </Card>
    </div>
  );
}

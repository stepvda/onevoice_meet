import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { LogIn } from "lucide-react";
import { api } from "../lib/api";
import { setAccessToken } from "../lib/auth";
import { refreshMe } from "../lib/me";
import { Button, Card, Field, Input } from "../components/ui";

/**
 * Native account login (email or username + password). For SSO users coming
 * from one.witysk.org, the existing iframe-based bootstrap still runs on app
 * mount; this page is for users who created an account directly on meet.
 */
export default function Login() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [handle, setHandle] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // 2FA second-step state. When set, we hide the password form and show
  // the code prompt; the challenge token is short-lived (~5 min).
  const [challenge, setChallenge] = useState<{
    token: string;
    totp: boolean;
    email: boolean;
    sent_to: string | null;
  } | null>(null);
  const [code, setCode] = useState("");
  const [sendingEmail, setSendingEmail] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const res = await api.login({ handle: handle.trim(), password });
      if (res.kind === "2fa") {
        setChallenge({
          token: res.challenge_token,
          totp: res.totp_enabled,
          email: res.email_otp_enabled,
          sent_to: res.email_otp_sent_to,
        });
      } else {
        setAccessToken(res.access_token);
        await refreshMe();
        navigate("/", { replace: true });
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function submit2fa(e: React.FormEvent) {
    e.preventDefault();
    if (!challenge) return;
    setErr(null);
    setBusy(true);
    try {
      const res = await api.loginVerify2fa({ challenge_token: challenge.token, code: code.trim() });
      setAccessToken(res.access_token);
      await refreshMe();
      navigate("/", { replace: true });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function requestEmailCode() {
    if (!challenge) return;
    setErr(null);
    setSendingEmail(true);
    try {
      const r = await api.loginSendEmailOtp(challenge.token);
      setChallenge({ ...challenge, sent_to: r.sent_to });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSendingEmail(false);
    }
  }

  return (
    <div className="p-4 lg:p-8 max-w-md mx-auto" data-testid="login-page">
      <Card>
        <h1 className="text-2xl font-bold text-slate-50 mb-1 flex items-center gap-2">
          <LogIn size={22} className="text-accent-500" />
          {t("auth.loginTitle", { defaultValue: "Sign in" })}
        </h1>
        <p className="text-sm text-slate-400 mb-4">
          {t("auth.loginNote", {
            defaultValue:
              "If you have a one.witysk.org account, that's used automatically — no login form needed there.",
          })}
        </p>

        {challenge ? (
          <form onSubmit={submit2fa} className="flex flex-col gap-4" data-testid="login-2fa-form">
            <p className="text-sm text-slate-300">
              {challenge.totp && challenge.email
                ? t("auth.twoFactorPromptBoth", {
                    defaultValue: "Enter the 6-digit code from your authenticator app, an emailed code, or a recovery code.",
                  })
                : challenge.totp
                ? t("auth.twoFactorPrompt", {
                    defaultValue: "Enter the 6-digit code from your authenticator app — or one of your recovery codes.",
                  })
                : t("auth.twoFactorPromptEmail", {
                    defaultValue: "Enter the 6-digit code we just emailed you.",
                  })}
            </p>
            {challenge.sent_to && (
              <p className="text-xs text-slate-400" data-testid="login-2fa-sent-to">
                {t("auth.twoFactorEmailSentTo", {
                  to: challenge.sent_to,
                  defaultValue: "Code sent to {{to}}",
                })}
              </p>
            )}
            <Field id="li-code" label={t("auth.codeLabel", { defaultValue: "Code" })}>
              <Input
                id="li-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                autoFocus
                required
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className="font-mono tracking-widest"
                placeholder="123456"
                data-testid="login-2fa-code"
              />
            </Field>
            {err && <div className="text-sm text-red-400" data-testid="login-error">{err}</div>}
            <div className="flex items-center gap-3 flex-wrap">
              <Button type="submit" disabled={busy} data-testid="login-2fa-submit">
                {busy ? t("auth.verifying", { defaultValue: "Verifying…" }) : t("auth.verify", { defaultValue: "Verify" })}
              </Button>
              {challenge.email && (
                <button
                  type="button"
                  className="text-sm text-accent-500 hover:underline disabled:opacity-50"
                  onClick={requestEmailCode}
                  disabled={sendingEmail}
                  data-testid="login-2fa-email-send"
                >
                  {sendingEmail
                    ? t("auth.sendingEmail", { defaultValue: "Sending…" })
                    : challenge.sent_to
                    ? t("auth.resendEmail", { defaultValue: "Resend email code" })
                    : t("auth.useEmail", { defaultValue: "Email me a code instead" })}
                </button>
              )}
              <button
                type="button"
                className="text-sm text-slate-400 hover:text-accent-500 hover:underline ml-auto"
                onClick={() => {
                  setChallenge(null);
                  setCode("");
                  setErr(null);
                }}
              >
                {t("auth.back", { defaultValue: "Back" })}
              </button>
            </div>
          </form>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-4">
            <Field id="li-handle" label={t("auth.emailOrUsername", { defaultValue: "Email or username" })}>
              <Input
                id="li-handle"
                autoComplete="username"
                required
                value={handle}
                onChange={(e) => setHandle(e.target.value)}
                data-testid="login-handle"
              />
            </Field>
            <Field id="li-password" label={t("auth.password", { defaultValue: "Password" })}>
              <Input
                id="li-password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                data-testid="login-password"
              />
            </Field>

            {err && <div className="text-sm text-red-400" data-testid="login-error">{err}</div>}

            <div className="flex items-center gap-3 flex-wrap">
              <Button type="submit" disabled={busy} data-testid="login-submit">
                {busy ? t("auth.signingIn", { defaultValue: "Signing in…" }) : t("auth.loginSubmit", { defaultValue: "Sign in" })}
              </Button>
              <Link to="/signup" className="text-sm text-accent-500 hover:underline">
                {t("auth.noAccount", { defaultValue: "Need an account? Sign up" })}
              </Link>
              <Link to="/forgot-password" className="text-sm text-slate-400 hover:text-accent-500 hover:underline ml-auto">
                {t("auth.forgotPassword", { defaultValue: "Forgot password?" })}
              </Link>
            </div>
          </form>
        )}
      </Card>
    </div>
  );
}

import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { UserPlus } from "lucide-react";
import { api } from "../lib/api";
import { setAccessToken } from "../lib/auth";
import { refreshMe } from "../lib/me";
import { Button, Card, Field, Input } from "../components/ui";
import PasswordStrengthIndicator from "../components/PasswordStrengthIndicator";

/**
 * Native account sign-up. Creates a meet-side account, starts the one-time
 * 10-day trial, returns a JWT — same flow shape as one.witysk.org SSO so the
 * rest of the app sees a normal authenticated user immediately afterwards.
 */
export default function SignUp() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const res = await api.signup({
        email: email.trim(),
        username: username.trim(),
        password,
        name: name.trim() || null,
      });
      setAccessToken(res.access_token);
      await refreshMe();
      navigate("/account", { replace: true });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-4 lg:p-8 max-w-md mx-auto" data-testid="signup-page">
      <Card>
        <h1 className="text-2xl font-bold text-slate-50 mb-1 flex items-center gap-2">
          <UserPlus size={22} className="text-accent-500" />
          {t("auth.signupTitle", { defaultValue: "Create an account" })}
        </h1>
        <p className="text-sm text-slate-400 mb-4">
          {t("auth.signupTrialNote", {
            defaultValue:
              "You'll get a 10-day free trial — meeting creation works immediately. After the trial, redeem a voucher or subscribe to keep creating meetings.",
          })}
        </p>

        <form onSubmit={submit} className="flex flex-col gap-4">
          <Field id="su-email" label={t("auth.email", { defaultValue: "Email" })}>
            <Input
              id="su-email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              data-testid="signup-email"
            />
          </Field>
          <Field id="su-username" label={t("auth.username", { defaultValue: "Username" })} hint={t("auth.usernameHint", { defaultValue: "3–32 chars: letters, digits, . _ -" })}>
            <Input
              id="su-username"
              autoComplete="username"
              required
              minLength={3}
              maxLength={32}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              data-testid="signup-username"
            />
          </Field>
          <Field id="su-name" label={t("auth.displayName", { defaultValue: "Display name (optional)" })}>
            <Input
              id="su-name"
              autoComplete="name"
              maxLength={120}
              value={name}
              onChange={(e) => setName(e.target.value)}
              data-testid="signup-name"
            />
          </Field>
          <Field id="su-password" label={t("auth.password", { defaultValue: "Password" })} hint={t("auth.passwordHint", { defaultValue: "At least 8 characters" })}>
            <Input
              id="su-password"
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              data-testid="signup-password"
            />
            <PasswordStrengthIndicator password={password} />
          </Field>

          {err && <div className="text-sm text-red-400" data-testid="signup-error">{err}</div>}

          <div className="flex items-center gap-3">
            <Button type="submit" disabled={busy} data-testid="signup-submit">
              {busy ? t("auth.signingUp", { defaultValue: "Creating…" }) : t("auth.signupSubmit", { defaultValue: "Create account" })}
            </Button>
            <Link to="/login" className="text-sm text-accent-500 hover:underline">
              {t("auth.haveAccount", { defaultValue: "Already have an account? Sign in" })}
            </Link>
          </div>
        </form>
      </Card>
    </div>
  );
}

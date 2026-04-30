/**
 * Two-factor authentication settings for native accounts.
 *
 * Two methods, set independently:
 *   - Authenticator app (TOTP): scan QR / paste secret, then verify a
 *     6-digit code to enable. Recovery codes shown once on enable.
 *   - Email OTP: a 6-digit code mailed at login time. Enable confirms
 *     the user's email by verifying the first code we send.
 *
 * The component reads the latest user state via api.me() rather than
 * relying solely on a parent prop, so refreshing after a state change
 * is local — no need to plumb refreshMe everywhere.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ShieldCheck, Smartphone, Mail } from "lucide-react";
import QRCode from "qrcode";
import { api, MeOut } from "../lib/api";
import { refreshMe } from "../lib/me";
import { Button, Card, Field, Input } from "./ui";

interface Props {
  me: MeOut;
  onChanged: (m: MeOut) => void;
}

type TotpMode = "idle" | "setup" | "show-recovery" | "disabling";
type EmailMode = "idle" | "confirming" | "disabling";

export default function TwoFactorSettings({ me, onChanged }: Props) {
  const { t } = useTranslation();

  // ─── TOTP state ────────────────────────────────────────────────────
  const [totpMode, setTotpMode] = useState<TotpMode>("idle");
  const [totpSecret, setTotpSecret] = useState<string | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const [totpCode, setTotpCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [disablePw, setDisablePw] = useState("");
  const [disableCode, setDisableCode] = useState("");
  const [totpBusy, setTotpBusy] = useState(false);
  const [totpErr, setTotpErr] = useState<string | null>(null);

  // ─── Email-OTP state ───────────────────────────────────────────────
  const [emailMode, setEmailMode] = useState<EmailMode>("idle");
  const [emailSentTo, setEmailSentTo] = useState<string | null>(null);
  const [emailCode, setEmailCode] = useState("");
  const [emailDisablePw, setEmailDisablePw] = useState("");
  const [emailBusy, setEmailBusy] = useState(false);
  const [emailErr, setEmailErr] = useState<string | null>(null);

  async function reload(): Promise<MeOut> {
    const fresh = await api.me();
    await refreshMe();
    onChanged(fresh);
    return fresh;
  }

  // ─── TOTP actions ──────────────────────────────────────────────────
  async function startTotpSetup() {
    setTotpErr(null);
    setTotpBusy(true);
    try {
      const r = await api.totpSetup();
      setTotpSecret(r.secret);
      const data = await QRCode.toDataURL(r.otpauth_uri, { errorCorrectionLevel: "M", margin: 1, scale: 6 });
      setQrDataUrl(data);
      setTotpMode("setup");
    } catch (e) {
      setTotpErr((e as Error).message);
    } finally {
      setTotpBusy(false);
    }
  }

  async function confirmTotp() {
    if (totpCode.trim().length < 6) return;
    setTotpErr(null);
    setTotpBusy(true);
    try {
      const r = await api.totpEnable(totpCode.trim());
      setRecoveryCodes(r.recovery_codes);
      setTotpMode("show-recovery");
      setTotpCode("");
      setTotpSecret(null);
      setQrDataUrl(null);
      await reload();
    } catch (e) {
      setTotpErr((e as Error).message);
    } finally {
      setTotpBusy(false);
    }
  }

  async function disableTotp() {
    setTotpErr(null);
    setTotpBusy(true);
    try {
      await api.totpDisable({ password: disablePw, code: disableCode.trim() });
      setDisablePw("");
      setDisableCode("");
      setTotpMode("idle");
      await reload();
    } catch (e) {
      setTotpErr((e as Error).message);
    } finally {
      setTotpBusy(false);
    }
  }

  async function regenerateRecovery() {
    const code = window.prompt(
      t("twofa.regenerateConfirm", { defaultValue: "Enter a current 6-digit code from your app to issue new recovery codes:" }) ?? "",
    );
    if (!code) return;
    setTotpErr(null);
    setTotpBusy(true);
    try {
      const r = await api.totpRegenerateRecovery(code.trim());
      setRecoveryCodes(r.recovery_codes);
      setTotpMode("show-recovery");
      await reload();
    } catch (e) {
      setTotpErr((e as Error).message);
    } finally {
      setTotpBusy(false);
    }
  }

  // ─── Email OTP actions ─────────────────────────────────────────────
  async function startEmail() {
    setEmailErr(null);
    setEmailBusy(true);
    try {
      const r = await api.emailOtpStart();
      setEmailSentTo(r.sent_to);
      setEmailMode("confirming");
    } catch (e) {
      setEmailErr((e as Error).message);
    } finally {
      setEmailBusy(false);
    }
  }

  async function confirmEmail() {
    if (emailCode.trim().length < 6) return;
    setEmailErr(null);
    setEmailBusy(true);
    try {
      await api.emailOtpConfirm(emailCode.trim());
      setEmailMode("idle");
      setEmailCode("");
      setEmailSentTo(null);
      await reload();
    } catch (e) {
      setEmailErr((e as Error).message);
    } finally {
      setEmailBusy(false);
    }
  }

  async function disableEmail() {
    setEmailErr(null);
    setEmailBusy(true);
    try {
      await api.emailOtpDisable(emailDisablePw);
      setEmailDisablePw("");
      setEmailMode("idle");
      await reload();
    } catch (e) {
      setEmailErr((e as Error).message);
    } finally {
      setEmailBusy(false);
    }
  }

  // ─── Render ────────────────────────────────────────────────────────
  return (
    <Card data-testid="twofa-settings">
      <h2 className="text-lg font-semibold text-slate-100 flex items-center gap-2 mb-1">
        <ShieldCheck size={18} className="text-accent-500" />
        {t("twofa.title", { defaultValue: "Two-factor authentication" })}
      </h2>
      <p className="text-sm text-slate-400 mb-4">
        {t("twofa.intro", {
          defaultValue: "Add a second step at sign-in. Use an authenticator app, an emailed code, or both.",
        })}
      </p>

      {/* ────── Authenticator app ────── */}
      <div className="border border-slate-700/40 rounded-lg p-3 mb-3">
        <div className="flex items-center gap-2">
          <Smartphone size={16} className="text-slate-300" />
          <h3 className="text-base font-semibold text-slate-100">
            {t("twofa.totpTitle", { defaultValue: "Authenticator app" })}
          </h3>
          <span
            className={`ml-auto text-xs px-2 py-0.5 rounded-full ${
              me.totp_enabled
                ? "bg-accent-500/20 text-accent-500 border border-accent-500/40"
                : "bg-slate-700/40 text-slate-400 border border-slate-600/40"
            }`}
            data-testid="twofa-totp-status"
          >
            {me.totp_enabled
              ? t("twofa.enabled", { defaultValue: "Enabled" })
              : t("twofa.disabled", { defaultValue: "Off" })}
          </span>
        </div>

        {!me.totp_enabled && totpMode === "idle" && (
          <div className="mt-3">
            <Button type="button" onClick={startTotpSetup} disabled={totpBusy} data-testid="twofa-totp-setup">
              {totpBusy ? t("twofa.loading", { defaultValue: "Loading…" }) : t("twofa.setup", { defaultValue: "Set up authenticator" })}
            </Button>
          </div>
        )}

        {!me.totp_enabled && totpMode === "setup" && qrDataUrl && totpSecret && (
          <div className="mt-3">
            <p className="text-sm text-slate-400 mb-2">
              {t("twofa.scanQr", {
                defaultValue: "Scan with Google Authenticator, 1Password, Authy, or any TOTP app:",
              })}
            </p>
            <img src={qrDataUrl} alt="QR code" className="bg-white p-2 rounded" width={180} height={180} />
            <p className="text-xs text-slate-500 mt-2">
              {t("twofa.manualKey", { defaultValue: "Or paste this secret:" })}{" "}
              <code className="text-slate-300 select-all">{totpSecret}</code>
            </p>
            <div className="mt-2">
              <Field
                id="totp-confirm"
                label={t("twofa.codeLabel", { defaultValue: "Enter the 6-digit code" })}
              >
                <Input
                  id="totp-confirm"
                  inputMode="numeric"
                  value={totpCode}
                  onChange={(e) => setTotpCode(e.target.value)}
                  className="font-mono tracking-widest"
                  placeholder="123456"
                  maxLength={6}
                  data-testid="twofa-totp-confirm"
                />
              </Field>
            </div>
            <div className="mt-3 flex gap-2 flex-wrap">
              <Button type="button" onClick={confirmTotp} disabled={totpBusy} data-testid="twofa-totp-enable">
                {totpBusy ? t("twofa.verifying", { defaultValue: "Verifying…" }) : t("twofa.enable", { defaultValue: "Enable" })}
              </Button>
              <button
                type="button"
                className="text-sm text-slate-400 hover:text-accent-500 hover:underline"
                onClick={() => {
                  setTotpMode("idle");
                  setTotpSecret(null);
                  setQrDataUrl(null);
                  setTotpCode("");
                  setTotpErr(null);
                }}
              >
                {t("twofa.cancel", { defaultValue: "Cancel" })}
              </button>
            </div>
          </div>
        )}

        {totpMode === "show-recovery" && (
          <div className="mt-3 bg-slate-900/40 rounded p-3 border border-amber-500/30">
            <p className="text-sm text-amber-300 font-semibold">
              {t("twofa.recoveryHeading", { defaultValue: "Save these recovery codes" })}
            </p>
            <p className="text-xs text-slate-400 mb-2">
              {t("twofa.recoveryWarning", {
                defaultValue: "Each code works once if you lose access to your authenticator. They will not be shown again.",
              })}
            </p>
            <ul className="grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-sm text-slate-100" data-testid="twofa-recovery-list">
              {recoveryCodes.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
            <div className="mt-3 flex gap-2 flex-wrap">
              <button
                type="button"
                className="text-sm text-accent-500 hover:underline"
                onClick={async () => {
                  await navigator.clipboard.writeText(recoveryCodes.join("\n"));
                }}
              >
                {t("twofa.copy", { defaultValue: "Copy to clipboard" })}
              </button>
              <button
                type="button"
                className="text-sm text-slate-400 hover:text-accent-500 hover:underline ml-auto"
                onClick={() => {
                  setTotpMode("idle");
                  setRecoveryCodes([]);
                }}
              >
                {t("twofa.dismiss", { defaultValue: "I've saved them" })}
              </button>
            </div>
          </div>
        )}

        {me.totp_enabled && totpMode === "idle" && (
          <div className="mt-3 flex flex-col gap-2">
            <p className="text-xs text-slate-400">
              {t("twofa.recoveryRemaining", {
                count: me.totp_recovery_remaining,
                defaultValue: "{{count}} recovery code(s) remaining.",
              })}
            </p>
            <div className="flex gap-2 flex-wrap">
              <button
                type="button"
                className="text-sm text-accent-500 hover:underline"
                onClick={regenerateRecovery}
                disabled={totpBusy}
              >
                {t("twofa.regenerate", { defaultValue: "Regenerate recovery codes" })}
              </button>
              <button
                type="button"
                className="text-sm text-red-400 hover:underline ml-auto"
                onClick={() => setTotpMode("disabling")}
              >
                {t("twofa.disable", { defaultValue: "Disable" })}
              </button>
            </div>
          </div>
        )}

        {me.totp_enabled && totpMode === "disabling" && (
          <div className="mt-3 flex flex-col gap-2" data-testid="twofa-totp-disable-form">
            <Field id="totp-disable-pw" label={t("twofa.passwordLabel", { defaultValue: "Password" })}>
              <Input
                id="totp-disable-pw"
                type="password"
                autoComplete="current-password"
                value={disablePw}
                onChange={(e) => setDisablePw(e.target.value)}
              />
            </Field>
            <Field id="totp-disable-code" label={t("twofa.disableCodeLabel", { defaultValue: "Current code or recovery code" })}>
              <Input
                id="totp-disable-code"
                inputMode="text"
                value={disableCode}
                onChange={(e) => setDisableCode(e.target.value)}
                className="font-mono tracking-widest"
              />
            </Field>
            <div className="flex gap-2 flex-wrap">
              <Button type="button" onClick={disableTotp} disabled={totpBusy}>
                {totpBusy ? t("twofa.disabling", { defaultValue: "Disabling…" }) : t("twofa.confirmDisable", { defaultValue: "Disable" })}
              </Button>
              <button
                type="button"
                className="text-sm text-slate-400 hover:text-accent-500 hover:underline"
                onClick={() => {
                  setTotpMode("idle");
                  setDisablePw("");
                  setDisableCode("");
                }}
              >
                {t("twofa.cancel", { defaultValue: "Cancel" })}
              </button>
            </div>
          </div>
        )}
        {totpErr && <p className="text-sm text-red-400 mt-2">{totpErr}</p>}
      </div>

      {/* ────── Email OTP ────── */}
      <div className="border border-slate-700/40 rounded-lg p-3">
        <div className="flex items-center gap-2">
          <Mail size={16} className="text-slate-300" />
          <h3 className="text-base font-semibold text-slate-100">
            {t("twofa.emailTitle", { defaultValue: "Email code" })}
          </h3>
          <span
            className={`ml-auto text-xs px-2 py-0.5 rounded-full ${
              me.email_otp_enabled
                ? "bg-accent-500/20 text-accent-500 border border-accent-500/40"
                : "bg-slate-700/40 text-slate-400 border border-slate-600/40"
            }`}
            data-testid="twofa-email-status"
          >
            {me.email_otp_enabled
              ? t("twofa.enabled", { defaultValue: "Enabled" })
              : t("twofa.disabled", { defaultValue: "Off" })}
          </span>
        </div>
        <p className="text-sm text-slate-400 mt-1">
          {t("twofa.emailIntro", {
            defaultValue: "We email a 6-digit code to your account address each time you sign in.",
          })}
        </p>

        {!me.email_otp_enabled && emailMode === "idle" && (
          <div className="mt-3">
            <Button type="button" onClick={startEmail} disabled={emailBusy} data-testid="twofa-email-start">
              {emailBusy ? t("twofa.sending", { defaultValue: "Sending…" }) : t("twofa.sendTestCode", { defaultValue: "Send a test code" })}
            </Button>
          </div>
        )}

        {!me.email_otp_enabled && emailMode === "confirming" && (
          <div className="mt-3 flex flex-col gap-2">
            <p className="text-sm text-slate-400">
              {t("twofa.emailSentTo", {
                to: emailSentTo ?? "",
                defaultValue: "Code sent to {{to}}. Enter it below to enable email codes.",
              })}
            </p>
            <Field id="email-otp-code" label={t("twofa.codeLabel", { defaultValue: "Enter the 6-digit code" })}>
              <Input
                id="email-otp-code"
                inputMode="numeric"
                value={emailCode}
                onChange={(e) => setEmailCode(e.target.value)}
                className="font-mono tracking-widest"
                maxLength={6}
                data-testid="twofa-email-confirm"
              />
            </Field>
            <div className="flex gap-2 flex-wrap">
              <Button type="button" onClick={confirmEmail} disabled={emailBusy}>
                {emailBusy ? t("twofa.verifying", { defaultValue: "Verifying…" }) : t("twofa.enable", { defaultValue: "Enable" })}
              </Button>
              <button
                type="button"
                className="text-sm text-accent-500 hover:underline"
                onClick={startEmail}
                disabled={emailBusy}
              >
                {t("twofa.resend", { defaultValue: "Resend" })}
              </button>
              <button
                type="button"
                className="text-sm text-slate-400 hover:text-accent-500 hover:underline ml-auto"
                onClick={() => {
                  setEmailMode("idle");
                  setEmailCode("");
                  setEmailSentTo(null);
                }}
              >
                {t("twofa.cancel", { defaultValue: "Cancel" })}
              </button>
            </div>
          </div>
        )}

        {me.email_otp_enabled && emailMode === "idle" && (
          <div className="mt-3">
            <button
              type="button"
              className="text-sm text-red-400 hover:underline"
              onClick={() => setEmailMode("disabling")}
            >
              {t("twofa.disable", { defaultValue: "Disable" })}
            </button>
          </div>
        )}

        {me.email_otp_enabled && emailMode === "disabling" && (
          <div className="mt-3 flex flex-col gap-2" data-testid="twofa-email-disable-form">
            <Field id="email-disable-pw" label={t("twofa.passwordLabel", { defaultValue: "Password" })}>
              <Input
                id="email-disable-pw"
                type="password"
                autoComplete="current-password"
                value={emailDisablePw}
                onChange={(e) => setEmailDisablePw(e.target.value)}
              />
            </Field>
            <div className="flex gap-2 flex-wrap">
              <Button type="button" onClick={disableEmail} disabled={emailBusy}>
                {emailBusy ? t("twofa.disabling", { defaultValue: "Disabling…" }) : t("twofa.confirmDisable", { defaultValue: "Disable" })}
              </Button>
              <button
                type="button"
                className="text-sm text-slate-400 hover:text-accent-500 hover:underline"
                onClick={() => {
                  setEmailMode("idle");
                  setEmailDisablePw("");
                }}
              >
                {t("twofa.cancel", { defaultValue: "Cancel" })}
              </button>
            </div>
          </div>
        )}
        {emailErr && <p className="text-sm text-red-400 mt-2">{emailErr}</p>}
      </div>
    </Card>
  );
}

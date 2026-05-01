import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AlertTriangle, ImagePlus, Trash2, User as UserIcon } from "lucide-react";
import { api, MeOut } from "../lib/api";
import { bootstrapFromOneWitysk, clearAccessToken, isAuthenticated } from "../lib/auth";
import { clearMe, refreshMe } from "../lib/me";
import { Button, Card, Field, Input } from "../components/ui";
import SignInPrompt from "../components/SignInPrompt";
import PasswordStrengthIndicator from "../components/PasswordStrengthIndicator";
import TwoFactorSettings from "../components/TwoFactorSettings";
import SubscriptionStatus from "../components/SubscriptionStatus";

/**
 * Account / profile editing. Native users can change name/email/username,
 * password, and upload a facepic. SSO users see a read-only view because
 * one.witysk.org is the source of truth for their profile + facepic.
 */
export default function Account() {
  const { t } = useTranslation();
  const [me, setMe] = useState<MeOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Edit form (native only)
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");

  // Password change
  const [curPw, setCurPw] = useState("");
  const [newPw, setNewPw] = useState("");

  // Delete account
  const [deletePw, setDeletePw] = useState("");
  const [deleting, setDeleting] = useState(false);

  // Transient success indicator for save / password / facepic actions —
  // clears itself after 2.5s. Without it the user can't tell whether the
  // PATCH succeeded.
  const [savedFlash, setSavedFlash] = useState<string | null>(null);
  useEffect(() => {
    if (!savedFlash) return;
    const id = window.setTimeout(() => setSavedFlash(null), 2500);
    return () => window.clearTimeout(id);
  }, [savedFlash]);

  const navigate = useNavigate();
  const fileRef = useRef<HTMLInputElement | null>(null);

  async function deleteAccount(e: React.FormEvent) {
    e.preventDefault();
    if (!confirm(t("account.deleteConfirm", { defaultValue: "Permanently delete your account? This cannot be undone." }))) return;
    setDeleting(true);
    setErr(null);
    try {
      await api.deleteMyAccount(deletePw);
      clearAccessToken();
      clearMe();
      navigate("/", { replace: true });
      window.location.reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setDeleting(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!isAuthenticated()) {
        const tok = await bootstrapFromOneWitysk();
        if (!tok) {
          if (!cancelled) {
            setNeedsSignIn(true);
            setLoading(false);
          }
          return;
        }
      }
      try {
        const u = await api.me();
        if (!cancelled) {
          setMe(u);
          setName(u.name ?? "");
          setEmail(u.email ?? "");
          setUsername(u.username ?? "");
        }
      } catch (e) {
        if (!cancelled) setErr((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function saveProfile(e: React.FormEvent) {
    e.preventDefault();
    if (!me || me.kind !== "native") return;
    setBusy(true);
    setErr(null);
    try {
      // Only send the fields that actually changed — the backend treats
      // every non-null field as an update target, and re-sending the user's
      // own existing email/username could spuriously trip the uniqueness
      // checks if the comparison ever drifted from case-insensitive.
      const body: { name?: string | null; email?: string | null; username?: string | null } = {};
      const trimmedName = name.trim() || null;
      if (trimmedName !== (me.name ?? null)) body.name = trimmedName;
      const trimmedEmail = email.trim() || null;
      if (trimmedEmail && trimmedEmail !== me.email) body.email = trimmedEmail;
      const trimmedUsername = username.trim() || null;
      if (trimmedUsername && trimmedUsername !== me.username) body.username = trimmedUsername;
      if (Object.keys(body).length === 0) {
        setSavedFlash(t("account.noChanges", { defaultValue: "Nothing to save" }));
        return;
      }
      const u = await api.updateMe(body);
      setMe(u);
      // Sync the global cache so the Sidebar / other consumers see the
      // updated profile (incl. is_voucher_admin re-evaluation if email changed).
      await refreshMe();
      setSavedFlash(t("account.saved", { defaultValue: "Saved" }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    if (!me || me.kind !== "native") return;
    setBusy(true);
    setErr(null);
    try {
      await api.changePassword({ current_password: curPw, new_password: newPw });
      setCurPw("");
      setNewPw("");
      setSavedFlash(t("account.passwordChanged", { defaultValue: "Password updated" }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function pickFacepic(file: File) {
    setBusy(true);
    setErr(null);
    try {
      const u = await api.uploadFacepic(file);
      setMe(u);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function removeFacepic() {
    setBusy(true);
    setErr(null);
    try {
      const u = await api.deleteFacepic();
      setMe(u);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-4 lg:p-8 max-w-2xl mx-auto" data-testid="account-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-4 flex items-center gap-2">
        <UserIcon size={22} className="text-accent-500" />
        {t("account.title", { defaultValue: "Your account" })}
      </h1>

      {needsSignIn && (
        <SignInPrompt
          icon={UserIcon}
          title={t("account.signInTitle", { defaultValue: "Sign in to manage your account" })}
          body={t("account.signInBody", { defaultValue: "Sign in on one.witysk.org or create a meet account first." })}
          testId="account-signin"
        />
      )}

      {loading && !needsSignIn && (
        <Card><p className="text-slate-300">{t("account.loading", { defaultValue: "Loading…" })}</p></Card>
      )}

      {!loading && me && (
        <div className="flex flex-col gap-4">
          {/* Status / entitlement summary */}
          <Card>
            <div className="flex items-center justify-between gap-4 flex-wrap">
              <div>
                <div className="text-sm text-slate-400">
                  {me.kind === "sso"
                    ? t("account.kindSso", { defaultValue: "Signed in via one.witysk.org" })
                    : t("account.kindNative", { defaultValue: "Native meet account" })}
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  {me.is_admin
                    ? t("account.canCreate", { defaultValue: "Meeting creation: enabled" })
                    : t("account.cannotCreate", { defaultValue: "Meeting creation: locked — needs voucher or subscription" })}
                  {me.trial_days_remaining != null && (
                    <span className="ml-2 text-amber-400">
                      {t("account.trialDays", {
                        count: me.trial_days_remaining,
                        defaultValue: "{{count}} trial day(s) remaining",
                      })}
                    </span>
                  )}
                </div>
              </div>
            </div>
          </Card>

          {err && <Card><p className="text-red-400" data-testid="account-error">{err}</p></Card>}
          {savedFlash && (
            <Card>
              <p className="text-accent-500 text-sm" data-testid="account-saved">{savedFlash}</p>
            </Card>
          )}

          {/* Facepic */}
          <Card>
            <h2 className="text-lg font-semibold text-slate-100 mb-3">
              {t("account.facepic", { defaultValue: "Profile picture" })}
            </h2>
            {me.kind === "sso" ? (
              <p className="text-sm text-slate-400">
                {t("account.facepicSsoNote", {
                  defaultValue: "Your picture comes from one.witysk.org and can only be changed there.",
                })}
              </p>
            ) : (
              <div className="flex items-center gap-4">
                {me.facepic_path ? (
                  <img
                    src={`/api/v1/users/${me.id}/facepic`}
                    alt=""
                    className="h-20 w-20 object-cover rounded-full border border-primary-700"
                  />
                ) : (
                  <div className="h-20 w-20 rounded-full bg-primary-600 flex items-center justify-center text-2xl font-semibold text-slate-50">
                    {(me.name || me.username || "?").slice(0, 1).toUpperCase()}
                  </div>
                )}
                <div className="flex flex-col gap-2">
                  <input
                    ref={fileRef}
                    type="file"
                    accept="image/jpeg,image/png,image/webp,image/gif"
                    aria-label={t("account.facepicUpload", { defaultValue: "Upload picture" })}
                    title={t("account.facepicUpload", { defaultValue: "Upload picture" })}
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) pickFacepic(f);
                    }}
                    className="hidden"
                  />
                  <Button
                    type="button"
                    onClick={() => fileRef.current?.click()}
                    disabled={busy}
                    data-testid="account-upload-facepic"
                  >
                    <ImagePlus size={16} />
                    {t("account.facepicUpload", { defaultValue: "Upload picture" })}
                  </Button>
                  {me.facepic_path && (
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={removeFacepic}
                      disabled={busy}
                      data-testid="account-delete-facepic"
                    >
                      <Trash2 size={16} />
                      {t("account.facepicRemove", { defaultValue: "Remove" })}
                    </Button>
                  )}
                </div>
              </div>
            )}
          </Card>

          <SubscriptionStatus me={me} />

          {/* Profile fields */}
          <Card>
            <h2 className="text-lg font-semibold text-slate-100 mb-3">
              {t("account.profile", { defaultValue: "Profile" })}
            </h2>
            {me.kind === "sso" ? (
              <p className="text-sm text-slate-400">
                {t("account.profileSsoNote", {
                  defaultValue: "Your profile is managed on one.witysk.org. Changes there will appear here automatically.",
                })}
              </p>
            ) : (
              <form onSubmit={saveProfile} className="flex flex-col gap-3">
                <Field id="acct-name" label={t("auth.displayName", { defaultValue: "Display name" })}>
                  <Input
                    id="acct-name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    maxLength={120}
                  />
                </Field>
                <Field id="acct-email" label={t("auth.email", { defaultValue: "Email" })}>
                  <Input
                    id="acct-email"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                  />
                </Field>
                <Field id="acct-username" label={t("auth.username", { defaultValue: "Username" })}>
                  <Input
                    id="acct-username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    minLength={3}
                    maxLength={32}
                    required
                  />
                </Field>
                <div>
                  <Button type="submit" disabled={busy} data-testid="account-save-profile">
                    {t("account.save", { defaultValue: "Save" })}
                  </Button>
                </div>
              </form>
            )}
          </Card>

          {/* Password change (native only) */}
          {me.kind === "native" && (
            <>
            <Card>
              <h2 className="text-lg font-semibold text-slate-100 mb-3">
                {t("account.password", { defaultValue: "Change password" })}
              </h2>
              <form onSubmit={changePassword} className="flex flex-col gap-3">
                <Field id="acct-curpw" label={t("account.currentPassword", { defaultValue: "Current password" })}>
                  <Input
                    id="acct-curpw"
                    type="password"
                    autoComplete="current-password"
                    value={curPw}
                    onChange={(e) => setCurPw(e.target.value)}
                    required
                  />
                </Field>
                <Field id="acct-newpw" label={t("account.newPassword", { defaultValue: "New password" })}>
                  <Input
                    id="acct-newpw"
                    type="password"
                    autoComplete="new-password"
                    value={newPw}
                    onChange={(e) => setNewPw(e.target.value)}
                    minLength={8}
                    required
                  />
                  <PasswordStrengthIndicator password={newPw} />
                </Field>
                <div>
                  <Button type="submit" disabled={busy} data-testid="account-change-password">
                    {t("account.changePassword", { defaultValue: "Change password" })}
                  </Button>
                </div>
              </form>
            </Card>

            <TwoFactorSettings me={me} onChanged={setMe} />

            {/* Danger zone — account deletion */}
            <Card className="border-red-700/50">
              <h2 className="text-lg font-semibold text-red-400 mb-1 flex items-center gap-2">
                <AlertTriangle size={18} /> {t("account.dangerZone", { defaultValue: "Danger zone" })}
              </h2>
              <p className="text-sm text-slate-400 mb-3">
                {t("account.deleteWarning", {
                  defaultValue:
                    "Permanently delete your account, profile picture and password-reset tokens. Meetings you've created remain accessible to participants who already had the link, but you'll no longer be associated with them. This cannot be undone.",
                })}
              </p>
              <form onSubmit={deleteAccount} className="flex flex-col gap-3 sm:flex-row sm:items-end">
                <Field id="acct-delpw" label={t("account.confirmPassword", { defaultValue: "Confirm with your password" })}>
                  <Input
                    id="acct-delpw"
                    type="password"
                    autoComplete="current-password"
                    value={deletePw}
                    onChange={(e) => setDeletePw(e.target.value)}
                    required
                  />
                </Field>
                <Button
                  type="submit"
                  variant="danger"
                  disabled={deleting || !deletePw}
                  data-testid="account-delete"
                >
                  <Trash2 size={16} />
                  {deleting
                    ? t("account.deleting", { defaultValue: "Deleting…" })
                    : t("account.deleteSubmit", { defaultValue: "Delete my account" })}
                </Button>
              </form>
            </Card>
            </>
          )}
        </div>
      )}
    </div>
  );
}

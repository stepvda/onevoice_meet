import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ImagePlus, Video, X } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { bootstrapFromOneWitysk, fetchOneWityskName, isAuthenticated } from "../lib/auth";
import { useMe } from "../lib/me";
import { usePreferences } from "../lib/preferences";
import { Button, Card, Field, Input, Label, Toggle } from "../components/ui";
import MyMeetings from "../components/MyMeetings";
import DiscoverableMeetings from "../components/DiscoverableMeetings";

const MAX_BRANDING_BYTES = 2 * 1024 * 1024;
const ALLOWED_BRANDING_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif"];

type AuthState = "bootstrapping" | "authenticated" | "anonymous";

/**
 * Reusable home-page description card. Same copy regardless of auth state
 * — we want the value proposition visible to anonymous, bootstrapping, and
 * signed-in viewers alike. Kept ≤ 50 words per the operator brief.
 */
function HomeDescription() {
  const { t } = useTranslation();
  return (
    <Card data-testid="home-intro">
      <h1 className="text-xl font-bold text-slate-50 mb-1">
        {t("home.tagline", { defaultValue: "meet.witysk.org" })}
      </h1>
      <p className="text-sm text-slate-300 leading-relaxed">
        {t("home.description", {
          defaultValue:
            "Free, browser-based video meetings for the witysk.org community. Audio Café, screen-share, persistent chat with images, and 30-day recordings — up to 50 participants per room, no install. Sign in with one.witysk.org or create a meet account for a 10-day free trial.",
        })}
      </p>
    </Card>
  );
}

export default function CreateMeeting() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const prefs = usePreferences((s) => s.meetingDefaults);
  const modPrefs = usePreferences((s) => s.moderation);
  const { me } = useMe();
  const [title, setTitle] = useState("");
  const [password, setPassword] = useState("");
  const [usePassword, setUsePassword] = useState(prefs.requirePassword);
  const [listForAuth, setListForAuth] = useState(false);
  const [listForAnon, setListForAnon] = useState(false);
  const [autoAdmitAuthenticated, setAutoAdmitAuthenticated] = useState(modPrefs.autoAdmitAuthenticated);
  const [requireNameOnJoin, setRequireNameOnJoin] = useState(modPrefs.requireNameOnJoin);
  const [autoMuteNewJoiners, setAutoMuteNewJoiners] = useState(modPrefs.autoMuteNewJoiners);
  const [autoDisableCameraForNew, setAutoDisableCameraForNew] = useState(modPrefs.autoDisableCameraForNew);
  const [waitingRoomEnabled, setWaitingRoomEnabled] = useState(modPrefs.waitingRoomEnabled);
  const [lockRoomAfterStart, setLockRoomAfterStart] = useState(modPrefs.lockRoomAfterStart);
  const [allowParticipantScreenshare, setAllowParticipantScreenshare] = useState(modPrefs.allowParticipantScreenshare);
  const [allowParticipantChat, setAllowParticipantChat] = useState(modPrefs.allowParticipantChat);
  const [lobbyGreeting, setLobbyGreeting] = useState(prefs.greeting || "");
  const [recurrenceRule, setRecurrenceRule] = useState<string>("");
  const [durationMinutes, setDurationMinutes] = useState<number>(60);
  const [branding, setBranding] = useState<File | null>(null);
  const [brandingPreview, setBrandingPreview] = useState<string | null>(null);
  const brandingInputRef = useRef<HTMLInputElement | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function pickBranding(file: File | null) {
    if (!file) {
      setBranding(null);
      if (brandingPreview) URL.revokeObjectURL(brandingPreview);
      setBrandingPreview(null);
      return;
    }
    if (!ALLOWED_BRANDING_TYPES.includes(file.type)) {
      setErr(t("createMeeting.unsupportedImageType", { type: file.type }));
      return;
    }
    if (file.size > MAX_BRANDING_BYTES) {
      setErr(t("createMeeting.imageTooLarge", { mb: (file.size / 1_048_576).toFixed(1) }));
      return;
    }
    setErr(null);
    setBranding(file);
    if (brandingPreview) URL.revokeObjectURL(brandingPreview);
    setBrandingPreview(URL.createObjectURL(file));
  }
  const [authState, setAuthState] = useState<AuthState>(
    isAuthenticated() ? "authenticated" : "bootstrapping"
  );

  useEffect(() => {
    if (authState !== "bootstrapping") return;
    let cancelled = false;
    bootstrapFromOneWitysk().then((token) => {
      if (cancelled) return;
      setAuthState(token ? "authenticated" : "anonymous");
    });
    return () => {
      cancelled = true;
    };
  }, [authState]);

  if (authState === "bootstrapping") {
    return (
      <div className="p-4 lg:p-8 max-w-2xl mx-auto flex flex-col gap-6">
        <HomeDescription />
        <Card>
          <p className="text-slate-300">{t("createMeeting.checkingSession")}</p>
        </Card>
      </div>
    );
  }

  if (authState === "anonymous") {
    return (
      <div className="p-4 lg:p-8 max-w-2xl mx-auto flex flex-col gap-6">
        <HomeDescription />
        <Card>
          <p className="text-slate-200">{t("createMeeting.needSignIn")}</p>
          <p className="mt-2 text-slate-400">{t("createMeeting.haveJoinLink")}</p>
          <div className="mt-3 flex flex-wrap gap-3 text-sm">
            <Link to="/signup" className="text-accent-500 hover:underline">
              {t("home.cta.signup", { defaultValue: "Create an account" })}
            </Link>
            <Link to="/login" className="text-accent-500 hover:underline">
              {t("home.cta.login", { defaultValue: "Sign in" })}
            </Link>
          </div>
        </Card>
        <DiscoverableMeetings />
      </div>
    );
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const display_name = await fetchOneWityskName();
      const res = await api.createMeeting({
        display_title: title,
        password: usePassword && password ? password : undefined,
        // Anonymous discovery implies authenticated.
        list_for_authenticated: listForAuth || listForAnon,
        list_for_anonymous: listForAnon,
        display_name,
        auto_admit_authenticated: autoAdmitAuthenticated,
        require_name_on_join: requireNameOnJoin,
        auto_mute_new_joiners: autoMuteNewJoiners,
        auto_disable_camera_for_new: autoDisableCameraForNew,
        waiting_room_enabled: waitingRoomEnabled,
        lock_room_after_start: lockRoomAfterStart,
        allow_participant_screenshare: allowParticipantScreenshare,
        allow_participant_chat: allowParticipantChat,
        lobby_greeting: lobbyGreeting.trim() || null,
        recurrence_rule: recurrenceRule || null,
        duration_minutes: durationMinutes,
      });
      // Upload branding (if chosen) before navigation. Non-fatal if it fails.
      if (branding) {
        try {
          await api.uploadBranding(res.meeting.id, branding);
        } catch (e) {
          setErr(t("createMeeting.uploadFailed", { message: (e as Error).message }));
        }
      }
      sessionStorage.setItem(`owner:${res.meeting.room_name}`, res.meeting.id);
      navigate(`/${res.meeting.room_name}`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // Native users without an active entitlement (trial expired, no voucher, no
  // paid sub) shouldn't see the create-meeting form at all — show an upsell
  // pointing to /upgrade instead. SSO and trial-active / paid users see the
  // full form. While `me` is still loading we keep the form hidden so we
  // don't briefly flash it for a no-rights user.
  const canCreate = me?.is_admin === true;

  return (
    <div className="p-4 lg:p-8 max-w-2xl mx-auto flex flex-col gap-6">
      <HomeDescription />
      <MyMeetings refreshKey={busy ? 0 : 1} />
      <DiscoverableMeetings />
      {!canCreate && me && me.kind === "native" && (
        <Card data-testid="home-upsell">
          <h2 className="text-lg font-semibold text-slate-100">
            {t("home.upsellTitle", { defaultValue: "Meeting creation is locked" })}
          </h2>
          <p className="text-sm text-slate-400 mt-1">
            {t("home.upsellBody", {
              defaultValue:
                "Your free trial has ended. Redeem a voucher or subscribe to keep creating meetings. Joining meetings, audio Café, and chat stay free.",
            })}
          </p>
          <div className="mt-3">
            <Link to="/upgrade" className="inline-flex items-center px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white text-sm font-semibold">
              {t("home.upsellCta", { defaultValue: "View options" })}
            </Link>
          </div>
        </Card>
      )}
      {canCreate && (
      <Card>
        <div className="flex items-center gap-3 mb-4">
          <div className="p-2 rounded-lg bg-accent-500/20 text-accent-500">
            <Video size={22} />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-slate-50">{t("createMeeting.title")}</h1>
            <p className="text-sm text-slate-400">
              {t("createMeeting.usingDefaults", {
                max: prefs.maxParticipants,
                recording:
                  prefs.recordingMode === "off"
                    ? t("createMeeting.recordingOff")
                    : prefs.recordingMode === "auto_on_start"
                    ? t("createMeeting.recordingAuto")
                    : t("createMeeting.recordingManual"),
              })}{" "}
              <a className="text-primary-200 underline" href="/settings">
                {t("createMeeting.changeDefaults")}
              </a>
            </p>
          </div>
        </div>

        <form onSubmit={submit} className="flex flex-col gap-4">
          <Field id="meeting-title" label={t("createMeeting.fieldTitle")}>
            <Input
              id="meeting-title"
              data-testid="meeting-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
              maxLength={200}
              placeholder={t("createMeeting.fieldTitlePlaceholder")}
            />
          </Field>

          <Toggle
            id="meeting-use-password"
            label={t("createMeeting.fieldRequirePassword")}
            checked={usePassword}
            onChange={setUsePassword}
          />

          {usePassword && (
            <Field id="meeting-password" label={t("createMeeting.fieldPassword")}>
              <Input
                id="meeting-password"
                data-testid="meeting-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required={usePassword}
              />
            </Field>
          )}

          <details className="group border-t border-primary-700 pt-3">
            <summary
              data-testid="visibility-summary"
              className="flex items-center justify-between cursor-pointer list-none select-none"
            >
              <span className="text-sm text-slate-300 font-medium">{t("createMeeting.visibility")}</span>
              <span className="text-xs text-slate-500 group-open:rotate-180 transition-transform">▾</span>
            </summary>
            <div className="space-y-2 mt-2">
              <p className="text-xs text-slate-400">{t("createMeeting.visibilityHint")}</p>
              <Toggle
                id="meeting-list-auth"
                label={t("createMeeting.listForAuth")}
                description={t("createMeeting.listForAuthDesc")}
                checked={listForAuth || listForAnon}
                onChange={(v) => {
                  setListForAuth(v);
                  if (!v) setListForAnon(false);
                }}
              />
              <Toggle
                id="meeting-list-anon"
                label={t("createMeeting.listForAnon")}
                description={t("createMeeting.listForAnonDesc")}
                checked={listForAnon}
                onChange={(v) => {
                  setListForAnon(v);
                  if (v) setListForAuth(true);
                }}
              />
            </div>
          </details>

          <details className="group border-t border-primary-700 pt-3">
            <summary
              data-testid="schedule-summary"
              className="flex items-center justify-between cursor-pointer list-none select-none"
            >
              <span className="text-sm text-slate-300 font-medium">{t("createMeeting.schedule", { defaultValue: "Schedule" })}</span>
              <span className="text-xs text-slate-500 group-open:rotate-180 transition-transform">▾</span>
            </summary>
            <div className="space-y-2 mt-2">
              <p className="text-xs text-slate-400">
                {t("createMeeting.scheduleHint", { defaultValue: "Set a duration and (optionally) a recurrence so calendar invites repeat automatically." })}
              </p>
              <div>
                <Label htmlFor="meeting-duration">{t("createMeeting.duration", { defaultValue: "Duration (minutes)" })}</Label>
                <input
                  id="meeting-duration"
                  data-testid="meeting-duration"
                  type="number"
                  min={5}
                  max={480}
                  value={durationMinutes}
                  onChange={(e) => setDurationMinutes(Math.max(5, Math.min(480, parseInt(e.target.value || "60", 10))))}
                  className="w-full px-3 py-2 rounded-lg bg-primary-800 text-slate-100 border border-primary-700 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
                />
              </div>
              <div>
                <Label htmlFor="meeting-recurrence">{t("createMeeting.recurrence", { defaultValue: "Recurrence" })}</Label>
                <select
                  id="meeting-recurrence"
                  data-testid="meeting-recurrence"
                  value={recurrenceRule}
                  onChange={(e) => setRecurrenceRule(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-primary-800 text-slate-100 border border-primary-700 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
                >
                  <option value="">{t("createMeeting.recurrenceNone", { defaultValue: "One-off (no recurrence)" })}</option>
                  <option value="FREQ=DAILY">{t("createMeeting.recurrenceDaily", { defaultValue: "Daily" })}</option>
                  <option value="FREQ=WEEKLY">{t("createMeeting.recurrenceWeekly", { defaultValue: "Weekly" })}</option>
                  <option value="FREQ=WEEKLY;INTERVAL=2">{t("createMeeting.recurrenceBiweekly", { defaultValue: "Every 2 weeks" })}</option>
                  <option value="FREQ=MONTHLY">{t("createMeeting.recurrenceMonthly", { defaultValue: "Monthly" })}</option>
                </select>
              </div>
            </div>
          </details>

          <details className="group border-t border-primary-700 pt-3">
            <summary
              data-testid="moderation-summary"
              className="flex items-center justify-between cursor-pointer list-none select-none"
            >
              <span className="text-sm text-slate-300 font-medium">{t("createMeeting.moderation")}</span>
              <span className="text-xs text-slate-500 group-open:rotate-180 transition-transform">▾</span>
            </summary>
            <div className="space-y-2 mt-2">
              <p className="text-xs text-slate-400">{t("createMeeting.moderationHint")}</p>
              <Toggle
                id="mod-require-name"
                label={t("createMeeting.requireNameOnJoin")}
                checked={requireNameOnJoin}
                onChange={setRequireNameOnJoin}
              />
              <Toggle
                id="mod-auto-mute"
                label={t("createMeeting.autoMuteNewJoiners")}
                checked={autoMuteNewJoiners}
                onChange={setAutoMuteNewJoiners}
              />
              <Toggle
                id="mod-auto-cam-off"
                label={t("createMeeting.autoDisableCameraForNew")}
                checked={autoDisableCameraForNew}
                onChange={setAutoDisableCameraForNew}
              />
              <Toggle
                id="mod-lock"
                label={t("createMeeting.lockRoomAfterStart")}
                description={t("createMeeting.lockRoomAfterStartDesc")}
                checked={lockRoomAfterStart}
                onChange={setLockRoomAfterStart}
              />
              <Toggle
                id="mod-allow-screenshare"
                label={t("createMeeting.allowParticipantScreenshare")}
                checked={allowParticipantScreenshare}
                onChange={setAllowParticipantScreenshare}
              />
              <Toggle
                id="mod-allow-chat"
                label={t("createMeeting.allowParticipantChat")}
                checked={allowParticipantChat}
                onChange={setAllowParticipantChat}
              />
              <Toggle
                id="mod-waiting-room"
                label={t("createMeeting.waitingRoomEnabled")}
                description={t("createMeeting.waitingRoomDesc")}
                checked={waitingRoomEnabled}
                onChange={setWaitingRoomEnabled}
              />
              <Toggle
                id="mod-auto-admit-auth"
                label={t("createMeeting.autoAdmitAuthenticated")}
                description={t("createMeeting.autoAdmitAuthenticatedDesc")}
                checked={autoAdmitAuthenticated}
                onChange={setAutoAdmitAuthenticated}
              />
              <div className="pt-2">
                <Label htmlFor="meeting-lobby-greeting">{t("createMeeting.lobbyGreeting")}</Label>
                <p className="text-xs text-slate-400 mb-1">{t("createMeeting.lobbyGreetingHint")}</p>
                <textarea
                  id="meeting-lobby-greeting"
                  data-testid="meeting-lobby-greeting"
                  value={lobbyGreeting}
                  onChange={(e) => setLobbyGreeting(e.target.value)}
                  maxLength={2000}
                  rows={3}
                  placeholder={t("createMeeting.lobbyGreetingPlaceholder")}
                  className="w-full px-3 py-2 rounded-lg bg-primary-800 text-slate-100 border border-primary-700 focus:outline-none focus:ring-2 focus:ring-primary-500 text-sm resize-y"
                />
              </div>
            </div>
          </details>

          <div className="border-t border-primary-700 pt-3">
            <Label htmlFor="meeting-branding">{t("createMeeting.fieldBranding")}</Label>
            <p className="text-xs text-slate-400 mb-2">{t("createMeeting.fieldBrandingHint")}</p>
            <input
              ref={brandingInputRef}
              id="meeting-branding"
              data-testid="meeting-branding"
              type="file"
              aria-label={t("createMeeting.brandingFieldAlt")}
              title={t("createMeeting.brandingFieldAlt")}
              accept={ALLOWED_BRANDING_TYPES.join(",")}
              onChange={(e) => pickBranding(e.target.files?.[0] ?? null)}
              className="block text-sm text-slate-300 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:text-sm file:font-medium file:bg-primary-700 file:text-slate-100 hover:file:bg-primary-600"
            />
            {brandingPreview && (
              <div className="mt-3 flex items-center gap-3">
                <img
                  src={brandingPreview}
                  alt={t("createMeeting.brandingPreviewAlt")}
                  data-testid="meeting-branding-preview"
                  className="h-16 w-16 object-cover rounded-md border border-primary-700"
                />
                <button
                  type="button"
                  onClick={() => {
                    pickBranding(null);
                    if (brandingInputRef.current) brandingInputRef.current.value = "";
                  }}
                  className="inline-flex items-center gap-1 text-sm text-slate-300 hover:text-slate-100"
                >
                  <X size={14} /> {t("common.remove")}
                </button>
              </div>
            )}
            {!brandingPreview && (
              <span className="inline-flex items-center gap-1 text-xs text-slate-500 mt-2">
                <ImagePlus size={14} /> {t("createMeeting.noImage")}
              </span>
            )}
          </div>

          <div>
            <Button type="submit" disabled={busy || !title} data-testid="create-submit">
              {busy ? t("createMeeting.submitting") : t("createMeeting.submit")}
            </Button>
            {err && <div className="text-red-400 text-sm mt-2">{err}</div>}
          </div>
        </form>
      </Card>
      )}
    </div>
  );
}

import { useEffect, useState } from "react";
import {
  Mic,
  Monitor,
  Bell,
  Accessibility,
  Wifi,
  Palette,
  Radio,
  Globe,
  Copy,
  Check,
  Pencil,
  PictureInPicture2,
  FileVideo,
  X,
} from "lucide-react";
import { Trans, useTranslation } from "react-i18next";
import { usePreferences } from "../lib/preferences";
import type {
  Layout as LayoutT,
  VideoQuality,
  Theme,
} from "../lib/preferences";
import { Toggle } from "./ui";
import { api, MeetingOut, CameraPublisher } from "../lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  // Owner-only host entry to open the LivestreamSettingsModal. Hidden for
  // non-owners and when undefined (which is how guests/cohosts get rendered).
  onConfigureLivestream?: () => void;
  // Owner-only entry to open the VideoPlaybackPanel. Same gating rules as
  // livestream: hidden for non-owners, hidden when undefined.

  // Owner-only entry to open the MeetingRecordingsModal — lists the
  // recordings of the current meeting with a per-row Download button.
  onViewRecordings?: () => void;

  // Owner-only Public stream group. When `meeting` is provided we render
  // the toggle + slug editor; otherwise the group stays hidden. Mutations
  // hit PATCH /api/v1/meetings/{id} and lift the updated row via
  // `onMeetingUpdated` so the toolbar / panels can react.
  meeting?: MeetingOut | null;
  onMeetingUpdated?: (m: MeetingOut) => void;
}

/**
 * In-meeting settings drawer — a curated subset of the full Settings page,
 * focused on the things you might actually want to flip without leaving the
 * meeting. Lives next to ParticipantsPanel and ChatPanel as an inline drawer
 * so the stage shrinks to fit and the bottom control bar stays visible.
 *
 * Writes go straight into the same `meet-preferences-v1` zustand store as
 * the full Settings page; persistence is automatic.
 */
export default function InMeetingSettings({
  open,
  onClose,
  onConfigureLivestream,
  onViewRecordings,
  meeting,
  onMeetingUpdated,
}: Props) {
  const { t } = useTranslation();
  const prefs = usePreferences();

  if (!open) return null;

  return (
    <aside
      data-testid="in-meeting-settings"
      role="complementary"
      aria-label={t("inMeetingSettings.title")}
      className="h-full w-full sm:w-80 flex-shrink-0 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col"
    >
      <header className="flex items-center justify-between px-4 py-3 border-b border-primary-700">
        <h2 className="text-sm font-semibold text-slate-100">{t("inMeetingSettings.title")}</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("inMeetingSettings.close")}
          data-testid="in-meeting-settings-close"
          className="p-1 rounded hover:bg-primary-700 text-slate-300"
        >
          <X size={18} />
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5 text-sm">
        {meeting && onMeetingUpdated && (
          <RenameTitleGroup meeting={meeting} onMeetingUpdated={onMeetingUpdated} />
        )}

        <Group icon={<Mic size={14} />} title={t("inMeetingSettings.groupAv")}>
          <Toggle
            id="im-cam-default"
            label={t("inMeetingSettings.camOnJoin")}
            checked={prefs.av.cameraOnByDefault}
            onChange={(v) => prefs.setAv({ cameraOnByDefault: v })}
          />
          <Toggle
            id="im-mic-default"
            label={t("inMeetingSettings.micOnJoin")}
            checked={prefs.av.micOnByDefault}
            onChange={(v) => prefs.setAv({ micOnByDefault: v })}
          />
          <Toggle
            id="im-noise"
            label={t("inMeetingSettings.noiseSuppression")}
            checked={prefs.av.noiseSuppression}
            onChange={(v) => prefs.setAv({ noiseSuppression: v })}
          />
          <Toggle
            id="im-echo"
            label={t("inMeetingSettings.echoCancellation")}
            checked={prefs.av.echoCancellation}
            onChange={(v) => prefs.setAv({ echoCancellation: v })}
          />
          <Toggle
            id="im-mirror-preview"
            label={t("inMeetingSettings.mirrorPreview")}
            checked={prefs.av.mirrorPreview}
            onChange={(v) => prefs.setAv({ mirrorPreview: v })}
          />
          <p className="text-xs text-slate-500">{t("inMeetingSettings.rejoinNote")}</p>
        </Group>

        <Group icon={<Monitor size={14} />} title={t("inMeetingSettings.groupDisplay")}>
          <Field label={t("inMeetingSettings.layout")} htmlFor="im-layout">
            <SelectInline
              id="im-layout"
              ariaLabel={t("inMeetingSettings.layout")}
              value={prefs.display.layout}
              onChange={(v) => prefs.setDisplay({ layout: v as LayoutT })}
              options={[
                ["auto", t("inMeetingSettings.layoutAuto")],
                ["grid", t("inMeetingSettings.layoutGrid")],
                ["speaker", t("inMeetingSettings.layoutSpeaker")],
                ["spotlight", t("inMeetingSettings.layoutSpotlight")],
              ]}
            />
          </Field>
          <Toggle
            id="im-mirror-own"
            label={t("inMeetingSettings.mirrorOwn")}
            checked={prefs.display.mirrorOwnVideo}
            onChange={(v) => prefs.setDisplay({ mirrorOwnVideo: v })}
          />
          <Toggle
            id="im-hide-self"
            label={t("inMeetingSettings.hideSelf")}
            checked={prefs.display.hideSelfView}
            onChange={(v) => prefs.setDisplay({ hideSelfView: v })}
          />
          <Toggle
            id="im-show-names"
            label={t("inMeetingSettings.showNames")}
            checked={prefs.display.showParticipantNames}
            onChange={(v) => prefs.setDisplay({ showParticipantNames: v })}
          />
          <Toggle
            id="im-conn-quality"
            label={t("inMeetingSettings.connQuality")}
            checked={prefs.display.showConnectionQuality}
            onChange={(v) => prefs.setDisplay({ showConnectionQuality: v })}
          />
          <Toggle
            id="im-meeting-clock"
            label={t("inMeetingSettings.meetingClock")}
            checked={prefs.display.showMeetingClock}
            onChange={(v) => prefs.setDisplay({ showMeetingClock: v })}
          />
          <Toggle
            id="im-highlight-speaker"
            label={t("inMeetingSettings.highlightSpeaker")}
            checked={prefs.display.highlightSpeaker}
            onChange={(v) => prefs.setDisplay({ highlightSpeaker: v })}
          />
        </Group>

        <Group icon={<Bell size={14} />} title={t("inMeetingSettings.groupNotifications")}>
          <Toggle
            id="im-sound-join"
            label={t("inMeetingSettings.soundOnJoin")}
            checked={prefs.notifications.soundOnJoin}
            onChange={(v) => prefs.setNotifications({ soundOnJoin: v })}
          />
          <Toggle
            id="im-chat-sound"
            label={t("inMeetingSettings.chatSound")}
            checked={prefs.notifications.chatMessageSound}
            onChange={(v) => prefs.setNotifications({ chatMessageSound: v })}
          />
          <Toggle
            id="im-ignore-own"
            label={t("inMeetingSettings.ignoreOwnJoins")}
            checked={prefs.notifications.ignoreOwnJoins}
            onChange={(v) => prefs.setNotifications({ ignoreOwnJoins: v })}
          />
        </Group>

        <Group icon={<Accessibility size={14} />} title={t("inMeetingSettings.groupAccessibility")}>
          <Toggle
            id="im-reduced-motion"
            label={t("inMeetingSettings.reducedMotion")}
            checked={prefs.accessibility.reducedMotion}
            onChange={(v) => prefs.setAccessibility({ reducedMotion: v })}
          />
          <Toggle
            id="im-mono"
            label={t("inMeetingSettings.monoAudio")}
            checked={prefs.accessibility.monoAudio}
            onChange={(v) => prefs.setAccessibility({ monoAudio: v })}
          />
        </Group>

        <Group icon={<Wifi size={14} />} title={t("inMeetingSettings.groupNetwork")}>
          <Field label={t("inMeetingSettings.quality")} htmlFor="im-quality">
            <SelectInline
              id="im-quality"
              ariaLabel={t("inMeetingSettings.quality")}
              value={prefs.network.preferredVideoQuality}
              onChange={(v) =>
                prefs.setNetwork({ preferredVideoQuality: v as VideoQuality })
              }
              options={[
                ["auto", t("inMeetingSettings.qualityAuto")],
                ["low", t("inMeetingSettings.qualityLow")],
                ["medium", t("inMeetingSettings.qualityMedium")],
                ["high", t("inMeetingSettings.qualityHigh")],
              ]}
            />
          </Field>
          <Toggle
            id="im-simulcast"
            label={t("inMeetingSettings.simulcast")}
            checked={prefs.network.simulcastEnabled}
            onChange={(v) => prefs.setNetwork({ simulcastEnabled: v })}
          />
          <Toggle
            id="im-force-relay"
            label={t("inMeetingSettings.forceRelay")}
            checked={prefs.network.forceRelay}
            onChange={(v) => prefs.setNetwork({ forceRelay: v })}
          />
          <p className="text-xs text-slate-500">{t("inMeetingSettings.rejoinNote")}</p>
        </Group>

        <Group icon={<Palette size={14} />} title={t("inMeetingSettings.groupAppearance")}>
          <Field label={t("inMeetingSettings.theme")} htmlFor="im-theme">
            <SelectInline
              id="im-theme"
              ariaLabel={t("inMeetingSettings.theme")}
              value={prefs.appearance.theme}
              onChange={(v) => prefs.setAppearance({ theme: v as Theme })}
              options={[
                ["system", t("inMeetingSettings.themeSystem")],
                ["dark", t("inMeetingSettings.themeDark")],
                ["light", t("inMeetingSettings.themeLight")],
              ]}
            />
          </Field>
          <Toggle
            id="im-compact"
            label={t("inMeetingSettings.compact")}
            checked={prefs.appearance.compactMode}
            onChange={(v) => prefs.setAppearance({ compactMode: v })}
          />
        </Group>

        {meeting && onMeetingUpdated && (
          <PublicGroup meeting={meeting} onMeetingUpdated={onMeetingUpdated} />
        )}

        {onViewRecordings && (
          <Group
            icon={<FileVideo size={14} />}
            title={t("inMeetingSettings.groupRecordings", { defaultValue: "Recordings" })}
          >
            <p className="text-xs text-slate-400">
              {t("inMeetingSettings.recordingsHint", {
                defaultValue:
                  "List the recordings of the current meeting and download them directly.",
              })}
            </p>
            <button
              type="button"
              onClick={onViewRecordings}
              data-testid="im-view-recordings"
              className="text-left text-sm px-3 py-1.5 rounded-md bg-primary-800 hover:bg-primary-700 text-slate-100 border border-primary-700"
            >
              {t("inMeetingSettings.viewRecordings", {
                defaultValue: "View recordings of this meeting…",
              })}
            </button>
          </Group>
        )}

        {onConfigureLivestream && (
          <Group icon={<Radio size={14} />} title={t("inMeetingSettings.groupLivestream", { defaultValue: "Live stream" })}>
            <p className="text-xs text-slate-400">
              {t("inMeetingSettings.livestreamHint", {
                defaultValue:
                  "Configure RTMPS URL and stream key for live streaming this meeting to X.com or another RTMP destination.",
              })}
            </p>
            <button
              type="button"
              onClick={onConfigureLivestream}
              data-testid="im-livestream-configure"
              className="text-left text-sm px-3 py-1.5 rounded-md bg-primary-800 hover:bg-primary-700 text-slate-100 border border-primary-700"
            >
              {t("inMeetingSettings.livestreamConfigure", { defaultValue: "Configure live stream…" })}
            </button>
          </Group>
        )}

        {meeting && onMeetingUpdated && (
          <PiPGroup meeting={meeting} onMeetingUpdated={onMeetingUpdated} />
        )}


        <p className="text-xs text-slate-500 pt-1">
          <Trans
            i18nKey="inMeetingSettings.moreSettings"
            components={{ 1: <a href="/settings" className="underline" title={t("nav.settings")} /> }}
          />
        </p>
      </div>
    </aside>
  );
}

function RenameTitleGroup({
  meeting,
  onMeetingUpdated,
}: {
  meeting: MeetingOut;
  onMeetingUpdated: (m: MeetingOut) => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState(meeting.display_title);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setTitle(meeting.display_title);
  }, [meeting.display_title]);

  const trimmed = title.trim();
  const dirty = trimmed.length > 0 && trimmed !== meeting.display_title;

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const updated = await api.updateMeeting(meeting.id, {
        display_title: trimmed,
      });
      onMeetingUpdated(updated);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
        <span className="text-slate-500">
          <Pencil size={14} />
        </span>
        {t("inMeetingSettings.groupMeeting", { defaultValue: "Meeting" })}
      </h3>
      <div className="flex flex-col gap-2">
        <Field
          label={t("inMeetingSettings.meetingName", { defaultValue: "Meeting name" })}
          htmlFor="im-meeting-title"
        >
          <input
            id="im-meeting-title"
            data-testid="im-meeting-title"
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={200}
            placeholder={t("inMeetingSettings.meetingNamePlaceholder", {
              defaultValue: "Meeting name",
            })}
            aria-label={t("inMeetingSettings.meetingName", {
              defaultValue: "Meeting name",
            })}
            className="w-full px-2 py-1.5 rounded-lg bg-primary-800 text-slate-100 border border-primary-700 focus:outline-none focus:ring-2 focus:ring-primary-500 text-sm"
          />
        </Field>
        {err && (
          <p data-testid="im-meeting-title-error" className="text-xs text-red-400">
            {err}
          </p>
        )}
        <div>
          <button
            type="button"
            onClick={save}
            disabled={busy || !dirty}
            data-testid="im-meeting-title-save"
            className="px-3 py-1.5 rounded-md bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium"
          >
            {busy
              ? t("common.saving", { defaultValue: "Saving…" })
              : t("inMeetingSettings.meetingRename", { defaultValue: "Rename" })}
          </button>
        </div>
      </div>
    </section>
  );
}


function PublicGroup({
  meeting,
  onMeetingUpdated,
}: {
  meeting: MeetingOut;
  onMeetingUpdated: (m: MeetingOut) => void;
}) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(!!meeting.public_enabled);
  const [slug, setSlug] = useState(meeting.public_slug ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // Keep local state in sync with the parent — re-renders happen when
  // toolbar code refreshes the row via onMeetingUpdated.
  useEffect(() => {
    setEnabled(!!meeting.public_enabled);
    setSlug(meeting.public_slug ?? "");
  }, [meeting.public_enabled, meeting.public_slug]);

  const dirty =
    enabled !== !!meeting.public_enabled ||
    slug.trim().toLowerCase() !== (meeting.public_slug ?? "");

  const publicUrl =
    meeting.public_url ??
    (meeting.public_slug
      ? `${window.location.origin}/public/${meeting.public_slug}`
      : null);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const normalised = slug.trim().toLowerCase();
      const updated = await api.updateMeeting(meeting.id, {
        public_enabled: enabled,
        public_slug: normalised || null,
      });
      onMeetingUpdated(updated);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function copyLink() {
    if (!publicUrl) return;
    try {
      await navigator.clipboard.writeText(publicUrl);
    } catch {
      window.prompt(
        t("inMeetingSettings.publicCopyPrompt", { defaultValue: "Copy this link" }),
        publicUrl,
      );
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  return (
    <section>
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
        <span className="text-slate-500">
          <Globe size={14} />
        </span>
        {t("inMeetingSettings.groupPublic", { defaultValue: "Public" })}
      </h3>
      <div className="flex flex-col gap-3">
        <Toggle
          id="im-public-enabled"
          label={t("inMeetingSettings.publicEnable", {
            defaultValue: "Public view-only stream",
          })}
          checked={enabled}
          onChange={(v) => setEnabled(v)}
        />
        <p className="text-xs text-slate-500">
          {t("inMeetingSettings.publicHint", {
            defaultValue:
              "When on, anyone can watch this meeting at meet.witysk.org/public/<name> without joining. Viewers are hidden and unlimited.",
          })}
        </p>

        {enabled && (
          <Field
            label={t("inMeetingSettings.publicSlug", { defaultValue: "Public name" })}
            htmlFor="im-public-slug"
          >
            <div className="flex items-stretch gap-1">
              <span className="px-2 py-1.5 rounded-l-lg bg-primary-800 border border-primary-700 border-r-0 text-slate-400 text-xs flex items-center">
                /public/
              </span>
              <input
                id="im-public-slug"
                type="text"
                data-testid="im-public-slug"
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                placeholder="my-stream"
                inputMode="url"
                autoCapitalize="off"
                spellCheck={false}
                className="flex-1 min-w-0 px-2 py-1.5 rounded-r-lg bg-primary-800 text-slate-100 border border-primary-700 focus:outline-none focus:ring-2 focus:ring-primary-500 text-sm"
              />
            </div>
            <p className="text-xs text-slate-500 mt-1">
              {t("inMeetingSettings.publicSlugHint", {
                defaultValue:
                  "Lowercase letters, digits and dashes only. Must be unique across all public streams.",
              })}
            </p>
          </Field>
        )}

        {err && (
          <p data-testid="im-public-error" className="text-xs text-red-400">
            {err}
          </p>
        )}

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={save}
            disabled={busy || !dirty}
            data-testid="im-public-save"
            className="px-3 py-1.5 rounded-md bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium"
          >
            {busy
              ? t("common.saving", { defaultValue: "Saving…" })
              : t("common.save", { defaultValue: "Save" })}
          </button>
          {publicUrl && meeting.public_enabled && (
            <button
              type="button"
              onClick={copyLink}
              data-testid="im-public-copy"
              title={publicUrl}
              className={[
                "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm border border-primary-700",
                copied
                  ? "bg-accent-500 text-white"
                  : "bg-primary-800 hover:bg-primary-700 text-slate-100",
              ].join(" ")}
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
              {copied
                ? t("inMeetingSettings.publicCopied", { defaultValue: "Copied!" })
                : t("inMeetingSettings.publicCopyLink", {
                    defaultValue: "Copy public link",
                  })}
            </button>
          )}
        </div>

        {publicUrl && meeting.public_enabled && (
          <p
            data-testid="im-public-url"
            className="text-xs text-slate-400 break-all"
          >
            {publicUrl}
          </p>
        )}
      </div>
    </section>
  );
}


function PiPGroup({
  meeting,
  onMeetingUpdated,
}: {
  meeting: MeetingOut;
  onMeetingUpdated: (m: MeetingOut) => void;
}) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(!!meeting.pip_enabled);
  const [overlayIdentity, setOverlayIdentity] = useState(
    meeting.pip_overlay_identity ?? "",
  );
  const [publishers, setPublishers] = useState<CameraPublisher[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Keep local state synced with parent — the host can flip these from
  // another tab and we want the panel to pick up the new value.
  useEffect(() => {
    setEnabled(!!meeting.pip_enabled);
    setOverlayIdentity(meeting.pip_overlay_identity ?? "");
  }, [meeting.pip_enabled, meeting.pip_overlay_identity]);

  // Poll the live camera-publisher list while the panel is open so
  // the dropdown reflects who's actually streaming a camera right now.
  // 5 s cadence is cheap (one LiveKit `list_participants` call) and
  // keeps the UI fresh enough that the host can pick a participant
  // immediately after they turn their camera on.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const list = await api.listCameraPublishers(meeting.id);
        if (!cancelled) setPublishers(list);
      } catch {
        // best-effort; empty list is fine
      }
    }
    void load();
    const timer = window.setInterval(load, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [meeting.id]);

  // If the currently-saved identity isn't in the live list (camera off,
  // participant disconnected), surface them as a disabled-looking entry
  // so the dropdown isn't confusingly blank.
  const options: [string, string][] = [
    [
      "",
      t("inMeetingSettings.pipOverlayNone", {
        defaultValue: "— Pick a participant —",
      }),
    ],
    ...publishers.map((p) => [p.identity, p.name] as [string, string]),
  ];
  if (
    overlayIdentity &&
    !publishers.some((p) => p.identity === overlayIdentity)
  ) {
    options.push([
      overlayIdentity,
      t("inMeetingSettings.pipOverlayOffline", {
        defaultValue: "{{name}} (camera off)",
        name: overlayIdentity,
      }),
    ]);
  }

  const dirty =
    enabled !== !!meeting.pip_enabled ||
    (overlayIdentity || null) !== (meeting.pip_overlay_identity || null);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const updated = await api.updateMeeting(meeting.id, {
        pip_enabled: enabled,
        pip_overlay_identity: overlayIdentity || null,
      });
      onMeetingUpdated(updated);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
        <span className="text-slate-500">
          <PictureInPicture2 size={14} />
        </span>
        {t("inMeetingSettings.groupPip", {
          defaultValue: "Picture-in-Picture",
        })}
      </h3>
      <div className="flex flex-col gap-3">
        <Toggle
          id="im-pip-enabled"
          label={t("inMeetingSettings.pipEnable", {
            defaultValue: "Picture-in-Picture for recording / live stream",
          })}
          checked={enabled}
          onChange={(v) => setEnabled(v)}
        />
        <p className="text-xs text-slate-500">
          {t("inMeetingSettings.pipHint", {
            defaultValue:
              "When on, recordings and the live stream show the screenshare or playback video full-bleed with a small camera overlay in the bottom-right corner. Falls back to active speaker when neither a screenshare nor a playback video is live.",
          })}
        </p>

        {enabled && (
          <Field
            label={t("inMeetingSettings.pipOverlay", {
              defaultValue: "Overlay camera",
            })}
            htmlFor="im-pip-overlay"
          >
            <SelectInline
              id="im-pip-overlay"
              ariaLabel={t("inMeetingSettings.pipOverlay", {
                defaultValue: "Overlay camera",
              })}
              value={overlayIdentity}
              onChange={(v) => setOverlayIdentity(v)}
              options={options}
            />
            <p className="text-xs text-slate-500 mt-1">
              {t("inMeetingSettings.pipOverlayHint", {
                defaultValue:
                  "Lists participants whose camera is currently on. If the chosen participant turns their camera off, the overlay hides automatically.",
              })}
            </p>
          </Field>
        )}

        {err && (
          <p data-testid="im-pip-error" className="text-xs text-red-400">
            {err}
          </p>
        )}

        <div>
          <button
            type="button"
            onClick={save}
            disabled={busy || !dirty}
            data-testid="im-pip-save"
            className="px-3 py-1.5 rounded-md bg-accent-500 hover:bg-accent-600 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium"
          >
            {busy
              ? t("common.saving", { defaultValue: "Saving…" })
              : t("common.save", { defaultValue: "Save" })}
          </button>
        </div>
      </div>
    </section>
  );
}


function Group({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
        <span className="text-slate-500">{icon}</span>
        {title}
      </h3>
      <div className="flex flex-col gap-3">{children}</div>
    </section>
  );
}

function Field({ label, htmlFor, children }: { label: string; htmlFor?: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="block text-xs text-slate-300 mb-1">
        {label}
      </label>
      {children}
    </div>
  );
}

function SelectInline({
  id,
  value,
  onChange,
  options,
  ariaLabel,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
  ariaLabel?: string;
}) {
  return (
    <select
      id={id}
      data-testid={id}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={ariaLabel ?? id}
      className="w-full px-2 py-1.5 rounded-lg bg-primary-800 text-slate-100 border border-primary-700 focus:outline-none focus:ring-2 focus:ring-primary-500 text-sm"
    >
      {options.map(([v, label]) => (
        <option key={v} value={v}>
          {label}
        </option>
      ))}
    </select>
  );
}

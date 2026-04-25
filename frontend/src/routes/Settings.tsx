import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Accessibility,
  Bell,
  Check,
  Code2,
  Globe,
  Keyboard,
  Languages,
  MessageCircle,
  Mic,
  Palette,
  Radio,
  RotateCcw,
  Shield,
  Users,
  Video,
  Wifi,
  type LucideIcon,
} from "lucide-react";
import { LANGUAGES, usePreferences } from "../lib/preferences";
import type {
  BackgroundEffect,
  DateFormat,
  FontSize,
  ForceIP,
  JoinSound,
  Language,
  Layout,
  RecordingFormat,
  RecordingMode,
  Theme,
  TimeFormat,
  VideoQuality,
} from "../lib/preferences";
import { Button, Card, CardHeader, Field, Input, Select, Toggle } from "../components/ui";

type TabId =
  | "av"
  | "display"
  | "meeting"
  | "moderation"
  | "recording"
  | "notifications"
  | "privacy"
  | "accessibility"
  | "keyboard"
  | "network"
  | "locale"
  | "chat"
  | "appearance"
  | "developer"
  | "reset";

interface TabDef {
  id: TabId;
  i18nKey: string;
  icon: LucideIcon;
}

const TABS: TabDef[] = [
  { id: "av", i18nKey: "settings.tabs.av", icon: Mic },
  { id: "display", i18nKey: "settings.tabs.display", icon: Video },
  { id: "meeting", i18nKey: "settings.tabs.meeting", icon: Users },
  { id: "moderation", i18nKey: "settings.tabs.moderation", icon: Shield },
  { id: "recording", i18nKey: "settings.tabs.recording", icon: Radio },
  { id: "notifications", i18nKey: "settings.tabs.notifications", icon: Bell },
  { id: "privacy", i18nKey: "settings.tabs.privacy", icon: Shield },
  { id: "accessibility", i18nKey: "settings.tabs.accessibility", icon: Accessibility },
  { id: "keyboard", i18nKey: "settings.tabs.keyboard", icon: Keyboard },
  { id: "network", i18nKey: "settings.tabs.network", icon: Wifi },
  { id: "locale", i18nKey: "settings.tabs.locale", icon: Languages },
  { id: "chat", i18nKey: "settings.tabs.chat", icon: MessageCircle },
  { id: "appearance", i18nKey: "settings.tabs.appearance", icon: Palette },
  { id: "developer", i18nKey: "settings.tabs.developer", icon: Code2 },
  { id: "reset", i18nKey: "settings.tabs.reset", icon: RotateCcw },
];

export default function Settings() {
  const { t } = useTranslation();
  const prefs = usePreferences();
  const [tab, setTab] = useState<TabId>("av");
  const [flash, setFlash] = useState<string | null>(null);

  function saved(msg?: string) {
    setFlash(msg ?? t("common.saved"));
    window.setTimeout(() => setFlash(null), 1200);
  }

  return (
    <div className="p-4 lg:p-8 max-w-5xl mx-auto" data-testid="settings-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-1">{t("settings.title")}</h1>
      <p className="text-slate-400 mb-6">{t("settings.subtitle")}</p>

      {flash && (
        <div
          data-testid="saved-flash"
          role="status"
          className="fixed top-4 right-4 z-50 flex items-center gap-2 px-4 py-2 rounded-lg bg-accent-500/20 text-accent-500 border border-accent-500/40 shadow-lg"
        >
          <Check size={16} /> {flash}
        </div>
      )}

      {/* Tab strip */}
      <div
        data-testid="settings-tabs"
        className="flex flex-wrap gap-2 mb-6 sticky top-0 bg-witysk-page/95 backdrop-blur py-2 z-10"
      >
        {TABS.map(({ id, i18nKey, icon: Icon }) => {
          const active = tab === id;
          return (
            <button
              type="button"
              key={id}
              data-testid={`tab-${id}`}
              onClick={() => setTab(id)}
              className={[
                "inline-flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors",
                "border",
                active
                  ? "bg-primary-500 text-white border-primary-500"
                  : "bg-primary-800/60 text-slate-200 border-primary-700 hover:bg-primary-700/60",
              ].join(" ")}
            >
              <Icon size={16} />
              {t(i18nKey)}
            </button>
          );
        })}
      </div>

      {/* Tab body */}
      {tab === "av" && (
        <Section
          icon={<Mic size={18} />}
          title={t("settings.av.section")}
          subtitle={t("settings.av.subtitle")}
          testId="section-av"
        >
          <Toggle
            id="pref-camera-default"
            label={t("settings.av.cameraDefault")}
            checked={prefs.av.cameraOnByDefault}
            onChange={(v) => {
              prefs.setAv({ cameraOnByDefault: v });
              saved();
            }}
          />
          <Toggle
            id="pref-mic-default"
            label={t("settings.av.micDefault")}
            checked={prefs.av.micOnByDefault}
            onChange={(v) => {
              prefs.setAv({ micOnByDefault: v });
              saved();
            }}
          />
          <Toggle
            id="pref-noise-suppression"
            label={t("settings.av.noiseSuppression")}
            checked={prefs.av.noiseSuppression}
            onChange={(v) => {
              prefs.setAv({ noiseSuppression: v });
              saved();
            }}
          />
          <Toggle
            id="pref-echo-cancellation"
            label={t("settings.av.echoCancellation")}
            checked={prefs.av.echoCancellation}
            onChange={(v) => {
              prefs.setAv({ echoCancellation: v });
              saved();
            }}
          />
          <Toggle
            id="pref-auto-gain"
            label={t("settings.av.autoGain")}
            checked={prefs.av.autoGainControl}
            onChange={(v) => {
              prefs.setAv({ autoGainControl: v });
              saved();
            }}
          />
          <Toggle
            id="pref-mirror-preview"
            label={t("settings.av.mirrorPreview")}
            checked={prefs.av.mirrorPreview}
            onChange={(v) => {
              prefs.setAv({ mirrorPreview: v });
              saved();
            }}
          />
          <Field id="pref-default-bg" label={t("settings.av.defaultBg")}>
            <Select
              id="pref-default-bg"
              data-testid="pref-default-bg"
              value={prefs.av.defaultBackground}
              onChange={(e) => {
                prefs.setAv({ defaultBackground: e.target.value as BackgroundEffect });
                saved();
              }}
            >
              <option value="off">{t("settings.av.bgOff")}</option>
              <option value="light-blur">{t("settings.av.bgLightBlur")}</option>
              <option value="blur">{t("settings.av.bgBlur")}</option>
              <option value="image">{t("settings.av.bgImage")}</option>
            </Select>
          </Field>
          <Field
            id="pref-default-volume"
            label={t("settings.av.defaultVolume")}
            hint={t("settings.av.defaultVolumeHint")}
          >
            <Input
              id="pref-default-volume"
              data-testid="pref-default-volume"
              type="number"
              min={0}
              max={100}
              value={prefs.av.defaultVolume}
              onChange={(e) => {
                prefs.setAv({ defaultVolume: clamp(Number(e.target.value), 0, 100) });
                saved();
              }}
            />
          </Field>
          <Toggle
            id="pref-ptt"
            label={t("settings.av.ptt")}
            description={t("settings.av.pttHint")}
            checked={prefs.av.pushToTalk}
            onChange={(v) => {
              prefs.setAv({ pushToTalk: v });
              saved();
            }}
          />
          <Field id="pref-ptt-key" label={t("settings.av.pttKey")}>
            <Input
              id="pref-ptt-key"
              data-testid="pref-ptt-key"
              value={prefs.av.pushToTalkKey}
              onChange={(e) => {
                prefs.setAv({ pushToTalkKey: e.target.value });
                saved();
              }}
            />
          </Field>
        </Section>
      )}

      {tab === "display" && (
        <Section icon={<Video size={18} />} title={t("settings.display.section")} testId="section-display">
          <Field id="pref-layout" label={t("settings.display.layout")}>
            <Select
              id="pref-layout"
              data-testid="pref-layout"
              value={prefs.display.layout}
              onChange={(e) => {
                prefs.setDisplay({ layout: e.target.value as Layout });
                saved();
              }}
            >
              <option value="auto">{t("settings.display.layoutAuto")}</option>
              <option value="grid">{t("settings.display.layoutGrid")}</option>
              <option value="speaker">{t("settings.display.layoutSpeaker")}</option>
              <option value="spotlight">{t("settings.display.layoutSpotlight")}</option>
            </Select>
          </Field>
          <Toggle id="pref-mirror-own" label={t("settings.display.mirrorOwn")} checked={prefs.display.mirrorOwnVideo} onChange={(v) => { prefs.setDisplay({ mirrorOwnVideo: v }); saved(); }} />
          <Toggle id="pref-show-names" label={t("settings.display.showNames")} checked={prefs.display.showParticipantNames} onChange={(v) => { prefs.setDisplay({ showParticipantNames: v }); saved(); }} />
          <Toggle id="pref-hide-self" label={t("settings.display.hideSelf")} checked={prefs.display.hideSelfView} onChange={(v) => { prefs.setDisplay({ hideSelfView: v }); saved(); }} />
          <Toggle id="pref-pin-ss" label={t("settings.display.pinScreenshare")} checked={prefs.display.pinFirstScreenshare} onChange={(v) => { prefs.setDisplay({ pinFirstScreenshare: v }); saved(); }} />
          <Toggle id="pref-hide-empty" label={t("settings.display.hideEmpty")} checked={prefs.display.hideEmptyTiles} onChange={(v) => { prefs.setDisplay({ hideEmptyTiles: v }); saved(); }} />
          <Toggle id="pref-conn-quality" label={t("settings.display.connQuality")} checked={prefs.display.showConnectionQuality} onChange={(v) => { prefs.setDisplay({ showConnectionQuality: v }); saved(); }} />
          <Toggle id="pref-meeting-clock" label={t("settings.display.meetingClock")} checked={prefs.display.showMeetingClock} onChange={(v) => { prefs.setDisplay({ showMeetingClock: v }); saved(); }} />
          <Toggle id="pref-highlight-speaker" label={t("settings.display.highlightSpeaker")} checked={prefs.display.highlightSpeaker} onChange={(v) => { prefs.setDisplay({ highlightSpeaker: v }); saved(); }} />
          <Field id="pref-max-tiles" label={t("settings.display.maxTiles")}>
            <Input id="pref-max-tiles" data-testid="pref-max-tiles" type="number" min={4} max={50} value={prefs.display.maxVisibleTiles} onChange={(e) => { prefs.setDisplay({ maxVisibleTiles: clamp(Number(e.target.value), 4, 50) }); saved(); }} />
          </Field>
        </Section>
      )}

      {tab === "meeting" && (
        <Section icon={<Users size={18} />} title={t("settings.meeting.section")} subtitle={t("settings.meeting.subtitle")} testId="section-meeting-defaults">
          <Field id="pref-max-participants" label={t("settings.meeting.maxParticipants")}>
            <Input id="pref-max-participants" data-testid="pref-max-participants" type="number" min={2} max={50} value={prefs.meetingDefaults.maxParticipants} onChange={(e) => { prefs.setMeetingDefaults({ maxParticipants: clamp(Number(e.target.value), 2, 50) }); saved(); }} />
          </Field>
          <Toggle id="pref-require-password" label={t("settings.meeting.requirePassword")} checked={prefs.meetingDefaults.requirePassword} onChange={(v) => { prefs.setMeetingDefaults({ requirePassword: v }); saved(); }} />
          <Field id="pref-recording-mode" label={t("settings.meeting.recordingMode")}>
            <Select id="pref-recording-mode" data-testid="pref-recording-mode" value={prefs.meetingDefaults.recordingMode} onChange={(e) => { prefs.setMeetingDefaults({ recordingMode: e.target.value as RecordingMode }); saved(); }}>
              <option value="manual">{t("settings.meeting.recordingManual")}</option>
              <option value="auto_on_start">{t("settings.meeting.recordingAuto")}</option>
              <option value="off">{t("settings.meeting.recordingOff")}</option>
            </Select>
          </Field>
          <Field id="pref-auto-end" label={t("settings.meeting.autoEnd")}>
            <Input id="pref-auto-end" data-testid="pref-auto-end" type="number" min={0} max={1440} value={prefs.meetingDefaults.autoEndMinutes ?? 0} onChange={(e) => { const n = clamp(Number(e.target.value), 0, 1440); prefs.setMeetingDefaults({ autoEndMinutes: n === 0 ? null : n }); saved(); }} />
          </Field>
          <Field id="pref-greeting" label={t("settings.meeting.greeting")} hint={t("settings.meeting.greetingHint")}>
            <Input id="pref-greeting" data-testid="pref-greeting" value={prefs.meetingDefaults.greeting} onChange={(e) => { prefs.setMeetingDefaults({ greeting: e.target.value }); saved(); }} />
          </Field>
          <Field id="pref-welcome" label={t("settings.meeting.welcome")} hint={t("settings.meeting.welcomeHint")}>
            <Input id="pref-welcome" data-testid="pref-welcome" value={prefs.meetingDefaults.welcomeMessage} onChange={(e) => { prefs.setMeetingDefaults({ welcomeMessage: e.target.value }); saved(); }} />
          </Field>
          <Toggle id="pref-enable-chat" label={t("settings.meeting.enableChat")} checked={prefs.meetingDefaults.enableChat} onChange={(v) => { prefs.setMeetingDefaults({ enableChat: v }); saved(); }} />
          <Toggle id="pref-enable-reactions" label={t("settings.meeting.enableReactions")} checked={prefs.meetingDefaults.enableReactions} onChange={(v) => { prefs.setMeetingDefaults({ enableReactions: v }); saved(); }} />
          <Toggle id="pref-enable-ss" label={t("settings.meeting.enableScreenshare")} checked={prefs.meetingDefaults.enableScreenshare} onChange={(v) => { prefs.setMeetingDefaults({ enableScreenshare: v }); saved(); }} />
        </Section>
      )}

      {tab === "moderation" && (
        <Section icon={<Shield size={18} />} title={t("settings.moderation.section")} subtitle={t("settings.moderation.subtitle")} testId="section-moderation">
          <Toggle id="pref-auto-admit" label={t("settings.moderation.autoAdmit")} checked={prefs.moderation.autoAdmitAuthenticated} onChange={(v) => { prefs.setModeration({ autoAdmitAuthenticated: v }); saved(); }} />
          <Toggle id="pref-require-name" label={t("settings.moderation.requireName")} checked={prefs.moderation.requireNameOnJoin} onChange={(v) => { prefs.setModeration({ requireNameOnJoin: v }); saved(); }} />
          <Toggle id="pref-auto-mute" label={t("settings.moderation.autoMute")} checked={prefs.moderation.autoMuteNewJoiners} onChange={(v) => { prefs.setModeration({ autoMuteNewJoiners: v }); saved(); }} />
          <Toggle id="pref-auto-cam-off" label={t("settings.moderation.autoCamOff")} checked={prefs.moderation.autoDisableCameraForNew} onChange={(v) => { prefs.setModeration({ autoDisableCameraForNew: v }); saved(); }} />
          <Toggle id="pref-waiting-room" label={t("settings.moderation.waitingRoom")} checked={prefs.moderation.waitingRoomEnabled} onChange={(v) => { prefs.setModeration({ waitingRoomEnabled: v }); saved(); }} />
          <Toggle id="pref-lock-start" label={t("settings.moderation.lockOnStart")} checked={prefs.moderation.lockRoomAfterStart} onChange={(v) => { prefs.setModeration({ lockRoomAfterStart: v }); saved(); }} />
          <Toggle id="pref-allow-ss" label={t("settings.moderation.allowSs")} checked={prefs.moderation.allowParticipantScreenshare} onChange={(v) => { prefs.setModeration({ allowParticipantScreenshare: v }); saved(); }} />
          <Toggle id="pref-allow-chat" label={t("settings.moderation.allowChat")} checked={prefs.moderation.allowParticipantChat} onChange={(v) => { prefs.setModeration({ allowParticipantChat: v }); saved(); }} />
        </Section>
      )}

      {tab === "recording" && (
        <Section icon={<Radio size={18} />} title={t("settings.recording.section")} subtitle={t("settings.recording.subtitle")} testId="section-recording">
          <Field id="pref-rec-format" label={t("settings.recording.format")}>
            <Select id="pref-rec-format" data-testid="pref-rec-format" value={prefs.recording.format} onChange={(e) => { prefs.setRecording({ format: e.target.value as RecordingFormat }); saved(); }}>
              <option value="mp4">{t("settings.recording.formatMp4")}</option>
              <option value="webm">{t("settings.recording.formatWebm")}</option>
            </Select>
          </Field>
          <Toggle id="pref-rec-audio-only" label={t("settings.recording.audioOnly")} checked={prefs.recording.audioOnly} onChange={(v) => { prefs.setRecording({ audioOnly: v }); saved(); }} />
          <Toggle id="pref-rec-chat" label={t("settings.recording.includeChat")} checked={prefs.recording.includeChat} onChange={(v) => { prefs.setRecording({ includeChat: v }); saved(); }} />
          <Toggle id="pref-rec-ss-sep" label={t("settings.recording.ssSeparate")} checked={prefs.recording.recordScreenshareSeparately} onChange={(v) => { prefs.setRecording({ recordScreenshareSeparately: v }); saved(); }} />
          <Toggle id="pref-rec-captions" label={t("settings.recording.captions")} checked={prefs.recording.captionsInRecording} onChange={(v) => { prefs.setRecording({ captionsInRecording: v }); saved(); }} />
          <Toggle id="pref-rec-notice" label={t("settings.recording.noticeOnStart")} checked={prefs.recording.noticeParticipantsOnStart} onChange={(v) => { prefs.setRecording({ noticeParticipantsOnStart: v }); saved(); }} />
        </Section>
      )}

      {tab === "notifications" && (
        <Section icon={<Bell size={18} />} title={t("settings.notifications.section")} testId="section-notifications">
          <Toggle id="pref-sound-join" label={t("settings.notifications.soundOnJoin")} checked={prefs.notifications.soundOnJoin} onChange={(v) => { prefs.setNotifications({ soundOnJoin: v }); saved(); }} />
          <Field id="pref-join-sound" label={t("settings.notifications.joinSound")}>
            <Select id="pref-join-sound" data-testid="pref-join-sound" value={prefs.notifications.joinSound} onChange={(e) => { prefs.setNotifications({ joinSound: e.target.value as JoinSound }); saved(); }}>
              <option value="none">{t("settings.notifications.joinSoundNone")}</option>
              <option value="chime">{t("settings.notifications.joinSoundChime")}</option>
              <option value="ping">{t("settings.notifications.joinSoundPing")}</option>
              <option value="doorbell">{t("settings.notifications.joinSoundDoorbell")}</option>
            </Select>
          </Field>
          <Field id="pref-notif-volume" label={t("settings.notifications.volume")}>
            <Input id="pref-notif-volume" data-testid="pref-notif-volume" type="number" min={0} max={100} value={prefs.notifications.notificationVolume} onChange={(e) => { prefs.setNotifications({ notificationVolume: clamp(Number(e.target.value), 0, 100) }); saved(); }} />
          </Field>
          <Toggle id="pref-browser-notif" label={t("settings.notifications.browserNotif")} checked={prefs.notifications.browserNotificationOnJoin} onChange={(v) => { prefs.setNotifications({ browserNotificationOnJoin: v }); saved(); }} />
          <Toggle id="pref-ignore-own" label={t("settings.notifications.ignoreOwnJoins")} checked={prefs.notifications.ignoreOwnJoins} onChange={(v) => { prefs.setNotifications({ ignoreOwnJoins: v }); saved(); }} />
          <Toggle id="pref-chat-sound" label={t("settings.notifications.chatSound")} checked={prefs.notifications.chatMessageSound} onChange={(v) => { prefs.setNotifications({ chatMessageSound: v }); saved(); }} />
          <Toggle id="pref-moderator-highlight" label={t("settings.notifications.highlightModActions")} checked={prefs.notifications.highlightModeratorActions} onChange={(v) => { prefs.setNotifications({ highlightModeratorActions: v }); saved(); }} />
          <div className="grid grid-cols-2 gap-4">
            <Field id="pref-dnd-start" label={t("settings.notifications.dndStart")}>
              <Input id="pref-dnd-start" data-testid="pref-dnd-start" type="time" value={prefs.notifications.doNotDisturbStart ?? ""} onChange={(e) => { prefs.setNotifications({ doNotDisturbStart: e.target.value || null }); saved(); }} />
            </Field>
            <Field id="pref-dnd-end" label={t("settings.notifications.dndEnd")}>
              <Input id="pref-dnd-end" data-testid="pref-dnd-end" type="time" value={prefs.notifications.doNotDisturbEnd ?? ""} onChange={(e) => { prefs.setNotifications({ doNotDisturbEnd: e.target.value || null }); saved(); }} />
            </Field>
          </div>
        </Section>
      )}

      {tab === "privacy" && (
        <Section icon={<Shield size={18} />} title={t("settings.privacy.section")} testId="section-privacy">
          <Toggle id="pref-secure-mode" label={t("settings.privacy.secureMode")} checked={prefs.privacy.secureModeByDefault} onChange={(v) => { prefs.setPrivacy({ secureModeByDefault: v }); saved(); }} />
          <Field id="pref-retention" label={t("settings.privacy.retention")}>
            <Input id="pref-retention" data-testid="pref-retention" type="number" min={0} max={365} value={prefs.privacy.recordingRetentionDaysOverride ?? 0} onChange={(e) => { const n = clamp(Number(e.target.value), 0, 365); prefs.setPrivacy({ recordingRetentionDaysOverride: n === 0 ? null : n }); saved(); }} />
          </Field>
          <Toggle id="pref-anonymise" label={t("settings.privacy.anonymise")} checked={prefs.privacy.anonymiseEmailInJoinLog} onChange={(v) => { prefs.setPrivacy({ anonymiseEmailInJoinLog: v }); saved(); }} />
          <Toggle id="pref-no-log-ip" label={t("settings.privacy.noLogIp")} checked={prefs.privacy.dontLogMyIp} onChange={(v) => { prefs.setPrivacy({ dontLogMyIp: v }); saved(); }} />
          <Toggle id="pref-no-analytics" label={t("settings.privacy.noAnalytics")} checked={prefs.privacy.disableAnalytics} onChange={(v) => { prefs.setPrivacy({ disableAnalytics: v }); saved(); }} />
          <Toggle id="pref-no-read-receipts" label={t("settings.privacy.noReadReceipts")} checked={prefs.privacy.disableReadReceipts} onChange={(v) => { prefs.setPrivacy({ disableReadReceipts: v }); saved(); }} />
          <Toggle id="pref-blur-email" label={t("settings.privacy.blurEmail")} checked={prefs.privacy.blurEmailInScreenshots} onChange={(v) => { prefs.setPrivacy({ blurEmailInScreenshots: v }); saved(); }} />
        </Section>
      )}

      {tab === "accessibility" && (
        <Section icon={<Accessibility size={18} />} title={t("settings.accessibility.section")} testId="section-accessibility">
          <Toggle id="pref-high-contrast" label={t("settings.accessibility.highContrast")} checked={prefs.accessibility.highContrast} onChange={(v) => { prefs.setAccessibility({ highContrast: v }); saved(); }} />
          <Toggle id="pref-reduced-motion" label={t("settings.accessibility.reducedMotion")} checked={prefs.accessibility.reducedMotion} onChange={(v) => { prefs.setAccessibility({ reducedMotion: v }); saved(); }} />
          <Toggle id="pref-captions" label={t("settings.accessibility.captions")} checked={prefs.accessibility.liveCaptions} onChange={(v) => { prefs.setAccessibility({ liveCaptions: v }); saved(); }} />
          <Field id="pref-captions-size" label={t("settings.accessibility.captionsSize")}>
            <Select id="pref-captions-size" data-testid="pref-captions-size" value={prefs.accessibility.captionsFontSize} onChange={(e) => { prefs.setAccessibility({ captionsFontSize: e.target.value as FontSize }); saved(); }}>
              <option value="small">{t("settings.accessibility.fontSmall")}</option>
              <option value="medium">{t("settings.accessibility.fontMedium")}</option>
              <option value="large">{t("settings.accessibility.fontLarge")}</option>
              <option value="xl">{t("settings.accessibility.fontXl")}</option>
            </Select>
          </Field>
          <Toggle id="pref-announce-events" label={t("settings.accessibility.announceEvents")} checked={prefs.accessibility.announceParticipantEvents} onChange={(v) => { prefs.setAccessibility({ announceParticipantEvents: v }); saved(); }} />
          <Toggle id="pref-focus-outlines" label={t("settings.accessibility.focusOutlines")} checked={prefs.accessibility.keyboardFocusOutlines} onChange={(v) => { prefs.setAccessibility({ keyboardFocusOutlines: v }); saved(); }} />
          <Toggle id="pref-mono-audio" label={t("settings.accessibility.monoAudio")} checked={prefs.accessibility.monoAudio} onChange={(v) => { prefs.setAccessibility({ monoAudio: v }); saved(); }} />
        </Section>
      )}

      {tab === "keyboard" && (
        <Section icon={<Keyboard size={18} />} title={t("settings.keyboard.section")} testId="section-keyboard">
          <Toggle id="pref-kb-enable" label={t("settings.keyboard.enable")} checked={prefs.keyboard.enableShortcuts} onChange={(v) => { prefs.setKeyboard({ enableShortcuts: v }); saved(); }} />
          <Field id="pref-kb-mute" label={t("settings.keyboard.muteToggle")}><Input id="pref-kb-mute" data-testid="pref-kb-mute" value={prefs.keyboard.muteToggleKey} onChange={(e) => { prefs.setKeyboard({ muteToggleKey: e.target.value }); saved(); }} /></Field>
          <Field id="pref-kb-cam" label={t("settings.keyboard.cameraToggle")}><Input id="pref-kb-cam" data-testid="pref-kb-cam" value={prefs.keyboard.cameraToggleKey} onChange={(e) => { prefs.setKeyboard({ cameraToggleKey: e.target.value }); saved(); }} /></Field>
          <Field id="pref-kb-hand" label={t("settings.keyboard.handRaise")}><Input id="pref-kb-hand" data-testid="pref-kb-hand" value={prefs.keyboard.handRaiseKey} onChange={(e) => { prefs.setKeyboard({ handRaiseKey: e.target.value }); saved(); }} /></Field>
          <Field id="pref-kb-leave" label={t("settings.keyboard.leave")}><Input id="pref-kb-leave" data-testid="pref-kb-leave" value={prefs.keyboard.leaveMeetingKey} onChange={(e) => { prefs.setKeyboard({ leaveMeetingKey: e.target.value }); saved(); }} /></Field>
          <Field id="pref-kb-ss" label={t("settings.keyboard.screenshare")}><Input id="pref-kb-ss" data-testid="pref-kb-ss" value={prefs.keyboard.screenshareKey} onChange={(e) => { prefs.setKeyboard({ screenshareKey: e.target.value }); saved(); }} /></Field>
          <Toggle id="pref-wheel-zoom" label={t("settings.keyboard.wheelZoom")} checked={prefs.keyboard.scrollWheelZoomTiles} onChange={(v) => { prefs.setKeyboard({ scrollWheelZoomTiles: v }); saved(); }} />
        </Section>
      )}

      {tab === "network" && (
        <Section icon={<Wifi size={18} />} title={t("settings.network.section")} testId="section-network">
          <Field id="pref-quality" label={t("settings.network.quality")}>
            <Select id="pref-quality" data-testid="pref-quality" value={prefs.network.preferredVideoQuality} onChange={(e) => { prefs.setNetwork({ preferredVideoQuality: e.target.value as VideoQuality }); saved(); }}>
              <option value="auto">{t("settings.network.qualityAuto")}</option>
              <option value="low">{t("settings.network.qualityLow")}</option>
              <option value="medium">{t("settings.network.qualityMedium")}</option>
              <option value="high">{t("settings.network.qualityHigh")}</option>
            </Select>
          </Field>
          <Toggle id="pref-simulcast" label={t("settings.network.simulcast")} checked={prefs.network.simulcastEnabled} onChange={(v) => { prefs.setNetwork({ simulcastEnabled: v }); saved(); }} />
          <Field id="pref-bw-limit" label={t("settings.network.bwLimit")}>
            <Input id="pref-bw-limit" data-testid="pref-bw-limit" type="number" min={0} max={50000} value={prefs.network.bandwidthLimitKbps ?? 0} onChange={(e) => { const n = clamp(Number(e.target.value), 0, 50000); prefs.setNetwork({ bandwidthLimitKbps: n === 0 ? null : n }); saved(); }} />
          </Field>
          <Toggle id="pref-force-relay" label={t("settings.network.forceRelay")} checked={prefs.network.forceRelay} onChange={(v) => { prefs.setNetwork({ forceRelay: v }); saved(); }} />
          <Field id="pref-force-ip" label={t("settings.network.forceIp")}>
            <Select id="pref-force-ip" data-testid="pref-force-ip" value={prefs.network.forceIpVersion} onChange={(e) => { prefs.setNetwork({ forceIpVersion: e.target.value as ForceIP }); saved(); }}>
              <option value="auto">{t("settings.network.ipAuto")}</option>
              <option value="v4">{t("settings.network.ipV4")}</option>
              <option value="v6">{t("settings.network.ipV6")}</option>
            </Select>
          </Field>
          <Toggle id="pref-no-hwaccel" label={t("settings.network.noHwAccel")} checked={prefs.network.disableHardwareAcceleration} onChange={(v) => { prefs.setNetwork({ disableHardwareAcceleration: v }); saved(); }} />
          <Toggle id="pref-prewarm" label={t("settings.network.prewarm")} checked={prefs.network.prewarmIceOnPageLoad} onChange={(v) => { prefs.setNetwork({ prewarmIceOnPageLoad: v }); saved(); }} />
          <Field id="pref-reconnect" label={t("settings.network.reconnect")}>
            <Input id="pref-reconnect" data-testid="pref-reconnect" type="number" min={0} max={10} value={prefs.network.reconnectAttempts} onChange={(e) => { prefs.setNetwork({ reconnectAttempts: clamp(Number(e.target.value), 0, 10) }); saved(); }} />
          </Field>
        </Section>
      )}

      {tab === "locale" && (
        <Section icon={<Languages size={18} />} title={t("settings.locale.section")} testId="section-locale">
          <Field id="pref-lang" label={t("settings.locale.language")}>
            <Select id="pref-lang" data-testid="pref-lang" value={prefs.locale.language} onChange={(e) => { prefs.setLocale({ language: e.target.value as Language }); saved(); }}>
              {LANGUAGES.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.native}
                  {l.native !== l.english ? ` (${l.english})` : ""}
                </option>
              ))}
            </Select>
          </Field>
          <Field id="pref-time-format" label={t("settings.locale.timeFormat")}>
            <Select id="pref-time-format" data-testid="pref-time-format" value={prefs.locale.timeFormat} onChange={(e) => { prefs.setLocale({ timeFormat: e.target.value as TimeFormat }); saved(); }}>
              <option value="24h">{t("settings.locale.time24")}</option>
              <option value="12h">{t("settings.locale.time12")}</option>
            </Select>
          </Field>
          <Field id="pref-date-format" label={t("settings.locale.dateFormat")}>
            <Select id="pref-date-format" data-testid="pref-date-format" value={prefs.locale.dateFormat} onChange={(e) => { prefs.setLocale({ dateFormat: e.target.value as DateFormat }); saved(); }}>
              <option value="YYYY-MM-DD">YYYY-MM-DD</option>
              <option value="DD/MM/YYYY">DD/MM/YYYY</option>
              <option value="MM/DD/YYYY">MM/DD/YYYY</option>
            </Select>
          </Field>
          <Field id="pref-tz" label={t("settings.locale.timezone")}><Input id="pref-tz" data-testid="pref-tz" value={prefs.locale.timezone} onChange={(e) => { prefs.setLocale({ timezone: e.target.value }); saved(); }} /></Field>
          <Field id="pref-first-day" label={t("settings.locale.firstDay")}>
            <Select id="pref-first-day" data-testid="pref-first-day" value={String(prefs.locale.firstDayOfWeek)} onChange={(e) => { prefs.setLocale({ firstDayOfWeek: Number(e.target.value) as 0 | 1 }); saved(); }}>
              <option value="1">{t("settings.locale.monday")}</option>
              <option value="0">{t("settings.locale.sunday")}</option>
            </Select>
          </Field>
          <Field id="pref-num-locale" label={t("settings.locale.numberLocale")}><Input id="pref-num-locale" data-testid="pref-num-locale" value={prefs.locale.numberLocale} onChange={(e) => { prefs.setLocale({ numberLocale: e.target.value }); saved(); }} /></Field>
        </Section>
      )}

      {tab === "chat" && (
        <Section icon={<MessageCircle size={18} />} title={t("settings.chat.section")} testId="section-chat">
          <Toggle id="pref-chat-save-history" label={t("settings.chat.saveHistory")} checked={prefs.chat.saveHistoryLocally} onChange={(v) => { prefs.setChat({ saveHistoryLocally: v }); saved(); }} />
          <Toggle id="pref-chat-emoji" label={t("settings.chat.emoji")} checked={prefs.chat.emojiPickerEnabled} onChange={(v) => { prefs.setChat({ emojiPickerEnabled: v }); saved(); }} />
          <Toggle id="pref-chat-markdown" label={t("settings.chat.markdown")} checked={prefs.chat.markdownRendering} onChange={(v) => { prefs.setChat({ markdownRendering: v }); saved(); }} />
          <Toggle id="pref-chat-links" label={t("settings.chat.linkPreviews")} checked={prefs.chat.linkPreviews} onChange={(v) => { prefs.setChat({ linkPreviews: v }); saved(); }} />
          <Toggle id="pref-chat-send-enter" label={t("settings.chat.sendOnEnter")} checked={prefs.chat.sendOnEnter} onChange={(v) => { prefs.setChat({ sendOnEnter: v }); saved(); }} />
          <Toggle id="pref-chat-translate" label={t("settings.chat.autoTranslate")} checked={prefs.chat.autoTranslate} onChange={(v) => { prefs.setChat({ autoTranslate: v }); saved(); }} />
        </Section>
      )}

      {tab === "appearance" && (
        <Section icon={<Palette size={18} />} title={t("settings.appearance.section")} testId="section-appearance">
          <Field id="pref-theme" label={t("settings.appearance.theme")}>
            <Select id="pref-theme" data-testid="pref-theme" value={prefs.appearance.theme} onChange={(e) => { prefs.setAppearance({ theme: e.target.value as Theme }); saved(); }}>
              <option value="system">{t("settings.appearance.themeSystem")}</option>
              <option value="dark">{t("settings.appearance.themeDark")}</option>
              <option value="light">{t("settings.appearance.themeLight")}</option>
            </Select>
          </Field>
          <Field id="pref-accent" label={t("settings.appearance.accent")}><Input id="pref-accent" data-testid="pref-accent" type="color" value={prefs.appearance.accentColor} onChange={(e) => { prefs.setAppearance({ accentColor: e.target.value }); saved(); }} className="w-20 h-10 p-1" /></Field>
          <Toggle id="pref-compact" label={t("settings.appearance.compact")} checked={prefs.appearance.compactMode} onChange={(v) => { prefs.setAppearance({ compactMode: v }); saved(); }} />
          <Field id="pref-font-size" label={t("settings.appearance.fontSize")}>
            <Select id="pref-font-size" data-testid="pref-font-size" value={prefs.appearance.fontSize} onChange={(e) => { prefs.setAppearance({ fontSize: e.target.value as FontSize }); saved(); }}>
              <option value="small">{t("settings.accessibility.fontSmall")}</option>
              <option value="medium">{t("settings.accessibility.fontMedium")}</option>
              <option value="large">{t("settings.accessibility.fontLarge")}</option>
              <option value="xl">{t("settings.accessibility.fontXl")}</option>
            </Select>
          </Field>
          <Toggle id="pref-rounded-avatars" label={t("settings.appearance.roundedAvatars")} checked={prefs.appearance.roundedAvatars} onChange={(v) => { prefs.setAppearance({ roundedAvatars: v }); saved(); }} />
          <Field id="pref-bg-opacity" label={t("settings.appearance.bgOpacity")} hint={t("settings.appearance.bgOpacityHint")}>
            <Input id="pref-bg-opacity" data-testid="pref-bg-opacity" type="number" min={0} max={100} value={prefs.appearance.backgroundOpacity} onChange={(e) => { prefs.setAppearance({ backgroundOpacity: clamp(Number(e.target.value), 0, 100) }); saved(); }} />
          </Field>
        </Section>
      )}

      {tab === "developer" && (
        <Section icon={<Code2 size={18} />} title={t("settings.developer.section")} testId="section-developer">
          <Toggle id="pref-dev-stats" label={t("settings.developer.stats")} checked={prefs.developer.showStatsOverlay} onChange={(v) => { prefs.setDeveloper({ showStatsOverlay: v }); saved(); }} />
          <Toggle id="pref-dev-verbose" label={t("settings.developer.verbose")} checked={prefs.developer.verboseLogging} onChange={(v) => { prefs.setDeveloper({ verboseLogging: v }); saved(); }} />
          <Toggle id="pref-dev-experiments" label={t("settings.developer.experimental")} description={t("settings.developer.experimentalHint")} checked={prefs.developer.experimentalFeatures} onChange={(v) => { prefs.setDeveloper({ experimentalFeatures: v }); saved(); }} />
          <Toggle id="pref-dev-webrtc" label={t("settings.developer.webrtcInternals")} checked={prefs.developer.webrtcInternalsShortcut} onChange={(v) => { prefs.setDeveloper({ webrtcInternalsShortcut: v }); saved(); }} />
          <Toggle id="pref-dev-logs" label={t("settings.developer.persistLogs")} checked={prefs.developer.persistLogsLocally} onChange={(v) => { prefs.setDeveloper({ persistLogsLocally: v }); saved(); }} />
        </Section>
      )}

      {tab === "reset" && (
        <Section icon={<Globe size={18} />} title={t("settings.reset.section")} testId="section-reset">
          <div className="flex items-center gap-3">
            <Button
              variant="danger"
              data-testid="reset-all"
              onClick={() => {
                if (confirm(t("settings.reset.confirm"))) {
                  prefs.reset();
                  saved(t("settings.reset.doneFlash"));
                }
              }}
            >
              {t("settings.reset.button")}
            </Button>
            <span className="text-sm text-slate-400">{t("settings.reset.hint")}</span>
          </div>
        </Section>
      )}
    </div>
  );
}

function Section({
  icon,
  title,
  subtitle,
  testId,
  children,
}: {
  icon?: React.ReactNode;
  title: string;
  subtitle?: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <Card data-testid={testId}>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            {icon && <span className="text-primary-300">{icon}</span>}
            {title}
          </span>
        }
        subtitle={subtitle}
      />
      <div className="flex flex-col gap-4">{children}</div>
    </Card>
  );
}

function clamp(v: number, lo: number, hi: number): number {
  if (Number.isNaN(v)) return lo;
  return Math.min(Math.max(v, lo), hi);
}

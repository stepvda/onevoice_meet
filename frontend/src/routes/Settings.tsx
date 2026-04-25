import { useState } from "react";
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
  label: string;
  icon: LucideIcon;
}

const TABS: TabDef[] = [
  { id: "av", label: "Audio & Video", icon: Mic },
  { id: "display", label: "Display", icon: Video },
  { id: "meeting", label: "Meeting defaults", icon: Users },
  { id: "moderation", label: "Moderation", icon: Shield },
  { id: "recording", label: "Recording", icon: Radio },
  { id: "notifications", label: "Notifications", icon: Bell },
  { id: "privacy", label: "Privacy", icon: Shield },
  { id: "accessibility", label: "Accessibility", icon: Accessibility },
  { id: "keyboard", label: "Keyboard", icon: Keyboard },
  { id: "network", label: "Network", icon: Wifi },
  { id: "locale", label: "Language", icon: Languages },
  { id: "chat", label: "Chat", icon: MessageCircle },
  { id: "appearance", label: "Appearance", icon: Palette },
  { id: "developer", label: "Developer", icon: Code2 },
  { id: "reset", label: "Reset", icon: RotateCcw },
];

export default function Settings() {
  const prefs = usePreferences();
  const [tab, setTab] = useState<TabId>("av");
  const [flash, setFlash] = useState<string | null>(null);

  function saved(msg = "Saved") {
    setFlash(msg);
    window.setTimeout(() => setFlash(null), 1200);
  }

  return (
    <div className="p-4 lg:p-8 max-w-5xl mx-auto" data-testid="settings-page">
      <h1 className="text-2xl font-bold text-slate-50 mb-1">Settings</h1>
      <p className="text-slate-400 mb-6">
        Preferences are stored in this browser only. They apply when you join or create meetings.
      </p>

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
        {TABS.map(({ id, label, icon: Icon }) => {
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
              {label}
            </button>
          );
        })}
      </div>

      {/* Tab body */}
      {tab === "av" && (
        <Section
          icon={<Mic size={18} />}
          title="Audio & Video"
          subtitle="How your mic and camera behave when you join a meeting."
          testId="section-av"
        >
          <Toggle
            id="pref-camera-default"
            label="Camera on when I join"
            checked={prefs.av.cameraOnByDefault}
            onChange={(v) => {
              prefs.setAv({ cameraOnByDefault: v });
              saved();
            }}
          />
          <Toggle
            id="pref-mic-default"
            label="Microphone on when I join"
            checked={prefs.av.micOnByDefault}
            onChange={(v) => {
              prefs.setAv({ micOnByDefault: v });
              saved();
            }}
          />
          <Toggle
            id="pref-noise-suppression"
            label="Noise suppression"
            checked={prefs.av.noiseSuppression}
            onChange={(v) => {
              prefs.setAv({ noiseSuppression: v });
              saved();
            }}
          />
          <Toggle
            id="pref-echo-cancellation"
            label="Echo cancellation"
            checked={prefs.av.echoCancellation}
            onChange={(v) => {
              prefs.setAv({ echoCancellation: v });
              saved();
            }}
          />
          <Toggle
            id="pref-auto-gain"
            label="Automatic gain control"
            checked={prefs.av.autoGainControl}
            onChange={(v) => {
              prefs.setAv({ autoGainControl: v });
              saved();
            }}
          />
          <Toggle
            id="pref-mirror-preview"
            label="Mirror my local camera preview"
            checked={prefs.av.mirrorPreview}
            onChange={(v) => {
              prefs.setAv({ mirrorPreview: v });
              saved();
            }}
          />
          <Field id="pref-default-bg" label="Default background effect">
            <Select
              id="pref-default-bg"
              data-testid="pref-default-bg"
              value={prefs.av.defaultBackground}
              onChange={(e) => {
                prefs.setAv({ defaultBackground: e.target.value as BackgroundEffect });
                saved();
              }}
            >
              <option value="off">Off</option>
              <option value="light-blur">Light blur</option>
              <option value="blur">Blur</option>
              <option value="image">Image</option>
            </Select>
          </Field>
          <Field
            id="pref-default-volume"
            label="Default output volume (%)"
            hint="Applied to the meeting audio renderer when you join."
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
            label="Push-to-talk (hold key to transmit)"
            description="When enabled, your mic stays muted unless you hold the key below."
            checked={prefs.av.pushToTalk}
            onChange={(v) => {
              prefs.setAv({ pushToTalk: v });
              saved();
            }}
          />
          <Field id="pref-ptt-key" label="Push-to-talk key">
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
        <Section icon={<Video size={18} />} title="Display" testId="section-display">
          <Field id="pref-layout" label="Default layout">
            <Select
              id="pref-layout"
              data-testid="pref-layout"
              value={prefs.display.layout}
              onChange={(e) => {
                prefs.setDisplay({ layout: e.target.value as Layout });
                saved();
              }}
            >
              <option value="auto">Auto — presenter when set, grid otherwise</option>
              <option value="grid">Always grid</option>
              <option value="speaker">Always active speaker</option>
              <option value="spotlight">Spotlight pinned participant</option>
            </Select>
          </Field>
          <Toggle id="pref-mirror-own" label="Mirror my own video" checked={prefs.display.mirrorOwnVideo} onChange={(v) => { prefs.setDisplay({ mirrorOwnVideo: v }); saved(); }} />
          <Toggle id="pref-show-names" label="Show participant names on tiles" checked={prefs.display.showParticipantNames} onChange={(v) => { prefs.setDisplay({ showParticipantNames: v }); saved(); }} />
          <Toggle id="pref-hide-self" label="Hide self view" checked={prefs.display.hideSelfView} onChange={(v) => { prefs.setDisplay({ hideSelfView: v }); saved(); }} />
          <Toggle id="pref-pin-ss" label="Pin the first screenshare" checked={prefs.display.pinFirstScreenshare} onChange={(v) => { prefs.setDisplay({ pinFirstScreenshare: v }); saved(); }} />
          <Toggle id="pref-hide-empty" label="Hide tiles with no camera or audio" checked={prefs.display.hideEmptyTiles} onChange={(v) => { prefs.setDisplay({ hideEmptyTiles: v }); saved(); }} />
          <Toggle id="pref-conn-quality" label="Show connection quality indicators" checked={prefs.display.showConnectionQuality} onChange={(v) => { prefs.setDisplay({ showConnectionQuality: v }); saved(); }} />
          <Toggle id="pref-meeting-clock" label="Show meeting duration clock" checked={prefs.display.showMeetingClock} onChange={(v) => { prefs.setDisplay({ showMeetingClock: v }); saved(); }} />
          <Toggle id="pref-highlight-speaker" label="Highlight the active speaker" checked={prefs.display.highlightSpeaker} onChange={(v) => { prefs.setDisplay({ highlightSpeaker: v }); saved(); }} />
          <Field id="pref-max-tiles" label="Max visible tiles at once">
            <Input id="pref-max-tiles" data-testid="pref-max-tiles" type="number" min={4} max={50} value={prefs.display.maxVisibleTiles} onChange={(e) => { prefs.setDisplay({ maxVisibleTiles: clamp(Number(e.target.value), 4, 50) }); saved(); }} />
          </Field>
        </Section>
      )}

      {tab === "meeting" && (
        <Section icon={<Users size={18} />} title="Meeting defaults" subtitle="Applied when you create a new meeting — you can override per-meeting." testId="section-meeting-defaults">
          <Field id="pref-max-participants" label="Max participants">
            <Input id="pref-max-participants" data-testid="pref-max-participants" type="number" min={2} max={50} value={prefs.meetingDefaults.maxParticipants} onChange={(e) => { prefs.setMeetingDefaults({ maxParticipants: clamp(Number(e.target.value), 2, 50) }); saved(); }} />
          </Field>
          <Toggle id="pref-require-password" label="Require a password by default" checked={prefs.meetingDefaults.requirePassword} onChange={(v) => { prefs.setMeetingDefaults({ requirePassword: v }); saved(); }} />
          <Field id="pref-recording-mode" label="Recording mode">
            <Select id="pref-recording-mode" data-testid="pref-recording-mode" value={prefs.meetingDefaults.recordingMode} onChange={(e) => { prefs.setMeetingDefaults({ recordingMode: e.target.value as RecordingMode }); saved(); }}>
              <option value="manual">Manual — start/stop with a button</option>
              <option value="auto_on_start">Auto — record as soon as I join</option>
              <option value="off">Off — do not allow recording</option>
            </Select>
          </Field>
          <Field id="pref-auto-end" label="Auto-end after (minutes, 0 = never)">
            <Input id="pref-auto-end" data-testid="pref-auto-end" type="number" min={0} max={1440} value={prefs.meetingDefaults.autoEndMinutes ?? 0} onChange={(e) => { const n = clamp(Number(e.target.value), 0, 1440); prefs.setMeetingDefaults({ autoEndMinutes: n === 0 ? null : n }); saved(); }} />
          </Field>
          <Field id="pref-greeting" label="Lobby greeting" hint="Shown to guests on the join page.">
            <Input id="pref-greeting" data-testid="pref-greeting" value={prefs.meetingDefaults.greeting} onChange={(e) => { prefs.setMeetingDefaults({ greeting: e.target.value }); saved(); }} />
          </Field>
          <Field id="pref-welcome" label="Welcome message" hint="Posted to the chat when someone joins.">
            <Input id="pref-welcome" data-testid="pref-welcome" value={prefs.meetingDefaults.welcomeMessage} onChange={(e) => { prefs.setMeetingDefaults({ welcomeMessage: e.target.value }); saved(); }} />
          </Field>
          <Toggle id="pref-enable-chat" label="Enable chat" checked={prefs.meetingDefaults.enableChat} onChange={(v) => { prefs.setMeetingDefaults({ enableChat: v }); saved(); }} />
          <Toggle id="pref-enable-reactions" label="Enable reactions (emojis)" checked={prefs.meetingDefaults.enableReactions} onChange={(v) => { prefs.setMeetingDefaults({ enableReactions: v }); saved(); }} />
          <Toggle id="pref-enable-ss" label="Enable screenshare" checked={prefs.meetingDefaults.enableScreenshare} onChange={(v) => { prefs.setMeetingDefaults({ enableScreenshare: v }); saved(); }} />
        </Section>
      )}

      {tab === "moderation" && (
        <Section icon={<Shield size={18} />} title="Moderation" subtitle="Owner-only defaults applied to new meetings." testId="section-moderation">
          <Toggle id="pref-auto-admit" label="Auto-admit authenticated users" checked={prefs.moderation.autoAdmitAuthenticated} onChange={(v) => { prefs.setModeration({ autoAdmitAuthenticated: v }); saved(); }} />
          <Toggle id="pref-require-name" label="Require a display name on join" checked={prefs.moderation.requireNameOnJoin} onChange={(v) => { prefs.setModeration({ requireNameOnJoin: v }); saved(); }} />
          <Toggle id="pref-auto-mute" label="Auto-mute new joiners" checked={prefs.moderation.autoMuteNewJoiners} onChange={(v) => { prefs.setModeration({ autoMuteNewJoiners: v }); saved(); }} />
          <Toggle id="pref-auto-cam-off" label="Auto-disable camera for new joiners" checked={prefs.moderation.autoDisableCameraForNew} onChange={(v) => { prefs.setModeration({ autoDisableCameraForNew: v }); saved(); }} />
          <Toggle id="pref-waiting-room" label="Enable waiting room" checked={prefs.moderation.waitingRoomEnabled} onChange={(v) => { prefs.setModeration({ waitingRoomEnabled: v }); saved(); }} />
          <Toggle id="pref-lock-start" label="Lock the room after the meeting starts" checked={prefs.moderation.lockRoomAfterStart} onChange={(v) => { prefs.setModeration({ lockRoomAfterStart: v }); saved(); }} />
          <Toggle id="pref-allow-ss" label="Allow participants to screenshare" checked={prefs.moderation.allowParticipantScreenshare} onChange={(v) => { prefs.setModeration({ allowParticipantScreenshare: v }); saved(); }} />
          <Toggle id="pref-allow-chat" label="Allow participants to chat" checked={prefs.moderation.allowParticipantChat} onChange={(v) => { prefs.setModeration({ allowParticipantChat: v }); saved(); }} />
        </Section>
      )}

      {tab === "recording" && (
        <Section icon={<Radio size={18} />} title="Recording" subtitle="Owner-only settings for server-side recordings." testId="section-recording">
          <Field id="pref-rec-format" label="Format">
            <Select id="pref-rec-format" data-testid="pref-rec-format" value={prefs.recording.format} onChange={(e) => { prefs.setRecording({ format: e.target.value as RecordingFormat }); saved(); }}>
              <option value="mp4">MP4 (H.264 + AAC) — broad compatibility</option>
              <option value="webm">WebM (VP9 + Opus) — smaller files</option>
            </Select>
          </Field>
          <Toggle id="pref-rec-audio-only" label="Record audio only (no video track)" checked={prefs.recording.audioOnly} onChange={(v) => { prefs.setRecording({ audioOnly: v }); saved(); }} />
          <Toggle id="pref-rec-chat" label="Include chat transcript with the recording" checked={prefs.recording.includeChat} onChange={(v) => { prefs.setRecording({ includeChat: v }); saved(); }} />
          <Toggle id="pref-rec-ss-sep" label="Record screenshare as a separate file" checked={prefs.recording.recordScreenshareSeparately} onChange={(v) => { prefs.setRecording({ recordScreenshareSeparately: v }); saved(); }} />
          <Toggle id="pref-rec-captions" label="Embed live captions in the recording" checked={prefs.recording.captionsInRecording} onChange={(v) => { prefs.setRecording({ captionsInRecording: v }); saved(); }} />
          <Toggle id="pref-rec-notice" label="Show a notice to participants when recording starts" checked={prefs.recording.noticeParticipantsOnStart} onChange={(v) => { prefs.setRecording({ noticeParticipantsOnStart: v }); saved(); }} />
        </Section>
      )}

      {tab === "notifications" && (
        <Section icon={<Bell size={18} />} title="Notifications" testId="section-notifications">
          <Toggle id="pref-sound-join" label="Play a sound when someone joins" checked={prefs.notifications.soundOnJoin} onChange={(v) => { prefs.setNotifications({ soundOnJoin: v }); saved(); }} />
          <Field id="pref-join-sound" label="Join sound">
            <Select id="pref-join-sound" data-testid="pref-join-sound" value={prefs.notifications.joinSound} onChange={(e) => { prefs.setNotifications({ joinSound: e.target.value as JoinSound }); saved(); }}>
              <option value="none">None</option>
              <option value="chime">Chime</option>
              <option value="ping">Ping</option>
              <option value="doorbell">Doorbell</option>
            </Select>
          </Field>
          <Field id="pref-notif-volume" label="Notification volume (%)">
            <Input id="pref-notif-volume" data-testid="pref-notif-volume" type="number" min={0} max={100} value={prefs.notifications.notificationVolume} onChange={(e) => { prefs.setNotifications({ notificationVolume: clamp(Number(e.target.value), 0, 100) }); saved(); }} />
          </Field>
          <Toggle id="pref-browser-notif" label="Show a browser notification when someone joins" checked={prefs.notifications.browserNotificationOnJoin} onChange={(v) => { prefs.setNotifications({ browserNotificationOnJoin: v }); saved(); }} />
          <Toggle id="pref-ignore-own" label="Don't play sounds for my own joins" checked={prefs.notifications.ignoreOwnJoins} onChange={(v) => { prefs.setNotifications({ ignoreOwnJoins: v }); saved(); }} />
          <Toggle id="pref-chat-sound" label="Sound on chat message" checked={prefs.notifications.chatMessageSound} onChange={(v) => { prefs.setNotifications({ chatMessageSound: v }); saved(); }} />
          <Toggle id="pref-moderator-highlight" label="Highlight moderator actions in the activity log" checked={prefs.notifications.highlightModeratorActions} onChange={(v) => { prefs.setNotifications({ highlightModeratorActions: v }); saved(); }} />
          <div className="grid grid-cols-2 gap-4">
            <Field id="pref-dnd-start" label="Do-not-disturb starts (HH:mm)">
              <Input id="pref-dnd-start" data-testid="pref-dnd-start" type="time" value={prefs.notifications.doNotDisturbStart ?? ""} onChange={(e) => { prefs.setNotifications({ doNotDisturbStart: e.target.value || null }); saved(); }} />
            </Field>
            <Field id="pref-dnd-end" label="Do-not-disturb ends (HH:mm)">
              <Input id="pref-dnd-end" data-testid="pref-dnd-end" type="time" value={prefs.notifications.doNotDisturbEnd ?? ""} onChange={(e) => { prefs.setNotifications({ doNotDisturbEnd: e.target.value || null }); saved(); }} />
            </Field>
          </div>
        </Section>
      )}

      {tab === "privacy" && (
        <Section icon={<Shield size={18} />} title="Privacy & data retention" testId="section-privacy">
          <Toggle id="pref-secure-mode" label="Start new meetings in secure mode (E2EE — disables recording)" checked={prefs.privacy.secureModeByDefault} onChange={(v) => { prefs.setPrivacy({ secureModeByDefault: v }); saved(); }} />
          <Field id="pref-retention" label="Delete my recordings after (days, 0 = server default of 30)">
            <Input id="pref-retention" data-testid="pref-retention" type="number" min={0} max={365} value={prefs.privacy.recordingRetentionDaysOverride ?? 0} onChange={(e) => { const n = clamp(Number(e.target.value), 0, 365); prefs.setPrivacy({ recordingRetentionDaysOverride: n === 0 ? null : n }); saved(); }} />
          </Field>
          <Toggle id="pref-anonymise" label="Anonymise participant emails in my server-side join log" checked={prefs.privacy.anonymiseEmailInJoinLog} onChange={(v) => { prefs.setPrivacy({ anonymiseEmailInJoinLog: v }); saved(); }} />
          <Toggle id="pref-no-log-ip" label="Don't log my IP address with join events" checked={prefs.privacy.dontLogMyIp} onChange={(v) => { prefs.setPrivacy({ dontLogMyIp: v }); saved(); }} />
          <Toggle id="pref-no-analytics" label="Disable client analytics" checked={prefs.privacy.disableAnalytics} onChange={(v) => { prefs.setPrivacy({ disableAnalytics: v }); saved(); }} />
          <Toggle id="pref-no-read-receipts" label="Disable read receipts in chat" checked={prefs.privacy.disableReadReceipts} onChange={(v) => { prefs.setPrivacy({ disableReadReceipts: v }); saved(); }} />
          <Toggle id="pref-blur-email" label="Blur participant emails in my screenshots" checked={prefs.privacy.blurEmailInScreenshots} onChange={(v) => { prefs.setPrivacy({ blurEmailInScreenshots: v }); saved(); }} />
        </Section>
      )}

      {tab === "accessibility" && (
        <Section icon={<Accessibility size={18} />} title="Accessibility" testId="section-accessibility">
          <Toggle id="pref-high-contrast" label="High-contrast theme" checked={prefs.accessibility.highContrast} onChange={(v) => { prefs.setAccessibility({ highContrast: v }); saved(); }} />
          <Toggle id="pref-reduced-motion" label="Reduced motion" checked={prefs.accessibility.reducedMotion} onChange={(v) => { prefs.setAccessibility({ reducedMotion: v }); saved(); }} />
          <Toggle id="pref-captions" label="Live captions (auto-generated)" checked={prefs.accessibility.liveCaptions} onChange={(v) => { prefs.setAccessibility({ liveCaptions: v }); saved(); }} />
          <Field id="pref-captions-size" label="Captions font size">
            <Select id="pref-captions-size" data-testid="pref-captions-size" value={prefs.accessibility.captionsFontSize} onChange={(e) => { prefs.setAccessibility({ captionsFontSize: e.target.value as FontSize }); saved(); }}>
              <option value="small">Small</option>
              <option value="medium">Medium</option>
              <option value="large">Large</option>
              <option value="xl">Extra large</option>
            </Select>
          </Field>
          <Toggle id="pref-announce-events" label="Announce participant events via screen reader" checked={prefs.accessibility.announceParticipantEvents} onChange={(v) => { prefs.setAccessibility({ announceParticipantEvents: v }); saved(); }} />
          <Toggle id="pref-focus-outlines" label="Always show keyboard focus outlines" checked={prefs.accessibility.keyboardFocusOutlines} onChange={(v) => { prefs.setAccessibility({ keyboardFocusOutlines: v }); saved(); }} />
          <Toggle id="pref-mono-audio" label="Mono audio mix (single ear users)" checked={prefs.accessibility.monoAudio} onChange={(v) => { prefs.setAccessibility({ monoAudio: v }); saved(); }} />
        </Section>
      )}

      {tab === "keyboard" && (
        <Section icon={<Keyboard size={18} />} title="Keyboard & input" testId="section-keyboard">
          <Toggle id="pref-kb-enable" label="Enable keyboard shortcuts" checked={prefs.keyboard.enableShortcuts} onChange={(v) => { prefs.setKeyboard({ enableShortcuts: v }); saved(); }} />
          <Field id="pref-kb-mute" label="Toggle mic"><Input id="pref-kb-mute" data-testid="pref-kb-mute" value={prefs.keyboard.muteToggleKey} onChange={(e) => { prefs.setKeyboard({ muteToggleKey: e.target.value }); saved(); }} /></Field>
          <Field id="pref-kb-cam" label="Toggle camera"><Input id="pref-kb-cam" data-testid="pref-kb-cam" value={prefs.keyboard.cameraToggleKey} onChange={(e) => { prefs.setKeyboard({ cameraToggleKey: e.target.value }); saved(); }} /></Field>
          <Field id="pref-kb-hand" label="Raise / lower hand"><Input id="pref-kb-hand" data-testid="pref-kb-hand" value={prefs.keyboard.handRaiseKey} onChange={(e) => { prefs.setKeyboard({ handRaiseKey: e.target.value }); saved(); }} /></Field>
          <Field id="pref-kb-leave" label="Leave meeting"><Input id="pref-kb-leave" data-testid="pref-kb-leave" value={prefs.keyboard.leaveMeetingKey} onChange={(e) => { prefs.setKeyboard({ leaveMeetingKey: e.target.value }); saved(); }} /></Field>
          <Field id="pref-kb-ss" label="Start / stop screenshare"><Input id="pref-kb-ss" data-testid="pref-kb-ss" value={prefs.keyboard.screenshareKey} onChange={(e) => { prefs.setKeyboard({ screenshareKey: e.target.value }); saved(); }} /></Field>
          <Toggle id="pref-wheel-zoom" label="Scroll wheel zooms participant tiles" checked={prefs.keyboard.scrollWheelZoomTiles} onChange={(v) => { prefs.setKeyboard({ scrollWheelZoomTiles: v }); saved(); }} />
        </Section>
      )}

      {tab === "network" && (
        <Section icon={<Wifi size={18} />} title="Network & quality" testId="section-network">
          <Field id="pref-quality" label="Preferred video quality">
            <Select id="pref-quality" data-testid="pref-quality" value={prefs.network.preferredVideoQuality} onChange={(e) => { prefs.setNetwork({ preferredVideoQuality: e.target.value as VideoQuality }); saved(); }}>
              <option value="auto">Auto (adapt to bandwidth)</option>
              <option value="low">Low (save data)</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </Select>
          </Field>
          <Toggle id="pref-simulcast" label="Simulcast (multi-bitrate publishing)" checked={prefs.network.simulcastEnabled} onChange={(v) => { prefs.setNetwork({ simulcastEnabled: v }); saved(); }} />
          <Field id="pref-bw-limit" label="Bandwidth limit (kbps, 0 = unlimited)">
            <Input id="pref-bw-limit" data-testid="pref-bw-limit" type="number" min={0} max={50000} value={prefs.network.bandwidthLimitKbps ?? 0} onChange={(e) => { const n = clamp(Number(e.target.value), 0, 50000); prefs.setNetwork({ bandwidthLimitKbps: n === 0 ? null : n }); saved(); }} />
          </Field>
          <Toggle id="pref-force-relay" label="Always use TURN (force media relay)" checked={prefs.network.forceRelay} onChange={(v) => { prefs.setNetwork({ forceRelay: v }); saved(); }} />
          <Field id="pref-force-ip" label="Force IP version">
            <Select id="pref-force-ip" data-testid="pref-force-ip" value={prefs.network.forceIpVersion} onChange={(e) => { prefs.setNetwork({ forceIpVersion: e.target.value as ForceIP }); saved(); }}>
              <option value="auto">Auto</option>
              <option value="v4">IPv4 only</option>
              <option value="v6">IPv6 only</option>
            </Select>
          </Field>
          <Toggle id="pref-no-hwaccel" label="Disable hardware video acceleration" checked={prefs.network.disableHardwareAcceleration} onChange={(v) => { prefs.setNetwork({ disableHardwareAcceleration: v }); saved(); }} />
          <Toggle id="pref-prewarm" label="Pre-warm ICE on page load" checked={prefs.network.prewarmIceOnPageLoad} onChange={(v) => { prefs.setNetwork({ prewarmIceOnPageLoad: v }); saved(); }} />
          <Field id="pref-reconnect" label="Reconnect attempts">
            <Input id="pref-reconnect" data-testid="pref-reconnect" type="number" min={0} max={10} value={prefs.network.reconnectAttempts} onChange={(e) => { prefs.setNetwork({ reconnectAttempts: clamp(Number(e.target.value), 0, 10) }); saved(); }} />
          </Field>
        </Section>
      )}

      {tab === "locale" && (
        <Section icon={<Languages size={18} />} title="Language & locale" testId="section-locale">
          <Field id="pref-lang" label="Interface language">
            <Select id="pref-lang" data-testid="pref-lang" value={prefs.locale.language} onChange={(e) => { prefs.setLocale({ language: e.target.value as Language }); saved(); }}>
              {LANGUAGES.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.native}
                  {l.native !== l.english ? ` (${l.english})` : ""}
                </option>
              ))}
            </Select>
          </Field>
          <Field id="pref-time-format" label="Time format">
            <Select id="pref-time-format" data-testid="pref-time-format" value={prefs.locale.timeFormat} onChange={(e) => { prefs.setLocale({ timeFormat: e.target.value as TimeFormat }); saved(); }}>
              <option value="24h">24-hour (13:30)</option>
              <option value="12h">12-hour (1:30 PM)</option>
            </Select>
          </Field>
          <Field id="pref-date-format" label="Date format">
            <Select id="pref-date-format" data-testid="pref-date-format" value={prefs.locale.dateFormat} onChange={(e) => { prefs.setLocale({ dateFormat: e.target.value as DateFormat }); saved(); }}>
              <option value="YYYY-MM-DD">YYYY-MM-DD</option>
              <option value="DD/MM/YYYY">DD/MM/YYYY</option>
              <option value="MM/DD/YYYY">MM/DD/YYYY</option>
            </Select>
          </Field>
          <Field id="pref-tz" label="Timezone"><Input id="pref-tz" data-testid="pref-tz" value={prefs.locale.timezone} onChange={(e) => { prefs.setLocale({ timezone: e.target.value }); saved(); }} /></Field>
          <Field id="pref-first-day" label="First day of week">
            <Select id="pref-first-day" data-testid="pref-first-day" value={String(prefs.locale.firstDayOfWeek)} onChange={(e) => { prefs.setLocale({ firstDayOfWeek: Number(e.target.value) as 0 | 1 }); saved(); }}>
              <option value="1">Monday</option>
              <option value="0">Sunday</option>
            </Select>
          </Field>
          <Field id="pref-num-locale" label="Number / currency locale"><Input id="pref-num-locale" data-testid="pref-num-locale" value={prefs.locale.numberLocale} onChange={(e) => { prefs.setLocale({ numberLocale: e.target.value }); saved(); }} /></Field>
        </Section>
      )}

      {tab === "chat" && (
        <Section icon={<MessageCircle size={18} />} title="Chat" testId="section-chat">
          <Toggle id="pref-chat-save-history" label="Save chat history on this device" checked={prefs.chat.saveHistoryLocally} onChange={(v) => { prefs.setChat({ saveHistoryLocally: v }); saved(); }} />
          <Toggle id="pref-chat-emoji" label="Show emoji picker" checked={prefs.chat.emojiPickerEnabled} onChange={(v) => { prefs.setChat({ emojiPickerEnabled: v }); saved(); }} />
          <Toggle id="pref-chat-markdown" label="Render markdown" checked={prefs.chat.markdownRendering} onChange={(v) => { prefs.setChat({ markdownRendering: v }); saved(); }} />
          <Toggle id="pref-chat-links" label="Show link previews" checked={prefs.chat.linkPreviews} onChange={(v) => { prefs.setChat({ linkPreviews: v }); saved(); }} />
          <Toggle id="pref-chat-send-enter" label="Enter sends (disable for Ctrl/Cmd+Enter to send)" checked={prefs.chat.sendOnEnter} onChange={(v) => { prefs.setChat({ sendOnEnter: v }); saved(); }} />
          <Toggle id="pref-chat-translate" label="Auto-translate to my interface language" checked={prefs.chat.autoTranslate} onChange={(v) => { prefs.setChat({ autoTranslate: v }); saved(); }} />
        </Section>
      )}

      {tab === "appearance" && (
        <Section icon={<Palette size={18} />} title="Appearance" testId="section-appearance">
          <Field id="pref-theme" label="Theme">
            <Select id="pref-theme" data-testid="pref-theme" value={prefs.appearance.theme} onChange={(e) => { prefs.setAppearance({ theme: e.target.value as Theme }); saved(); }}>
              <option value="system">System</option>
              <option value="dark">Dark</option>
              <option value="light">Light</option>
            </Select>
          </Field>
          <Field id="pref-accent" label="Accent color"><Input id="pref-accent" data-testid="pref-accent" type="color" value={prefs.appearance.accentColor} onChange={(e) => { prefs.setAppearance({ accentColor: e.target.value }); saved(); }} className="w-20 h-10 p-1" /></Field>
          <Toggle id="pref-compact" label="Compact mode (denser UI)" checked={prefs.appearance.compactMode} onChange={(v) => { prefs.setAppearance({ compactMode: v }); saved(); }} />
          <Field id="pref-font-size" label="UI font size">
            <Select id="pref-font-size" data-testid="pref-font-size" value={prefs.appearance.fontSize} onChange={(e) => { prefs.setAppearance({ fontSize: e.target.value as FontSize }); saved(); }}>
              <option value="small">Small</option>
              <option value="medium">Medium</option>
              <option value="large">Large</option>
              <option value="xl">Extra large</option>
            </Select>
          </Field>
          <Toggle id="pref-rounded-avatars" label="Rounded avatars" checked={prefs.appearance.roundedAvatars} onChange={(v) => { prefs.setAppearance({ roundedAvatars: v }); saved(); }} />
          <Field id="pref-bg-opacity" label="Background opacity (%)" hint="Applies to the meeting background and lobby overlays.">
            <Input id="pref-bg-opacity" data-testid="pref-bg-opacity" type="number" min={0} max={100} value={prefs.appearance.backgroundOpacity} onChange={(e) => { prefs.setAppearance({ backgroundOpacity: clamp(Number(e.target.value), 0, 100) }); saved(); }} />
          </Field>
        </Section>
      )}

      {tab === "developer" && (
        <Section icon={<Code2 size={18} />} title="Developer" testId="section-developer">
          <Toggle id="pref-dev-stats" label="Show LiveKit stats overlay" checked={prefs.developer.showStatsOverlay} onChange={(v) => { prefs.setDeveloper({ showStatsOverlay: v }); saved(); }} />
          <Toggle id="pref-dev-verbose" label="Verbose client logging" checked={prefs.developer.verboseLogging} onChange={(v) => { prefs.setDeveloper({ verboseLogging: v }); saved(); }} />
          <Toggle id="pref-dev-experiments" label="Enable experimental features" description="Opts this browser into features that may be incomplete or change without notice." checked={prefs.developer.experimentalFeatures} onChange={(v) => { prefs.setDeveloper({ experimentalFeatures: v }); saved(); }} />
          <Toggle id="pref-dev-webrtc" label="Enable chrome://webrtc-internals shortcut" checked={prefs.developer.webrtcInternalsShortcut} onChange={(v) => { prefs.setDeveloper({ webrtcInternalsShortcut: v }); saved(); }} />
          <Toggle id="pref-dev-logs" label="Persist logs to localStorage" checked={prefs.developer.persistLogsLocally} onChange={(v) => { prefs.setDeveloper({ persistLogsLocally: v }); saved(); }} />
        </Section>
      )}

      {tab === "reset" && (
        <Section icon={<Globe size={18} />} title="Reset" testId="section-reset">
          <div className="flex items-center gap-3">
            <Button
              variant="danger"
              data-testid="reset-all"
              onClick={() => {
                if (confirm("Reset all preferences to defaults?")) {
                  prefs.reset();
                  saved("Reset to defaults");
                }
              }}
            >
              Reset all preferences
            </Button>
            <span className="text-sm text-slate-400">
              Restores every setting on this page to the factory default.
            </span>
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

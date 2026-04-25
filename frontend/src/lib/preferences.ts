/**
 * User preferences store.
 *
 * Every value here lives in localStorage on this device — the browser is the
 * source of truth. Nothing syncs across devices yet. If we add a server-side
 * user profile later, the keys here map 1:1 to JSON columns.
 *
 * Many settings below describe intent rather than behaviour wired to code
 * today. Phase 13+ will read these. The shape is intentionally forward-
 * looking so we don't have to migrate localStorage schemas later.
 */
import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

export type Layout = "auto" | "grid" | "speaker" | "spotlight";
export type BackgroundEffect = "off" | "blur" | "light-blur" | "image";
export type RecordingMode = "manual" | "auto_on_start" | "off";
export type VideoQuality = "low" | "medium" | "high" | "auto";
export type Theme = "system" | "dark" | "light";
// All 50 locales we ship translations for (matches frontend/public/locales/).
export type Language =
  | "am" | "ar" | "bg" | "bn" | "cs" | "da" | "de" | "el" | "en" | "es"
  | "fa" | "fi" | "fil" | "fr" | "gu" | "ha" | "he" | "hi" | "hr" | "hu"
  | "id" | "it" | "ja" | "kk" | "ko" | "mr" | "ms" | "my" | "ne" | "nl"
  | "no" | "pa" | "pl" | "pt" | "ro" | "ru" | "sk" | "sl" | "sr" | "sv"
  | "sw" | "ta" | "te" | "th" | "tr" | "uk" | "ur" | "uz" | "vi" | "zh";

/** Native-name + English-label pairs for the language picker. */
export const LANGUAGES: { code: Language; native: string; english: string }[] = [
  { code: "en",  native: "English",        english: "English" },
  { code: "ar",  native: "العربية",        english: "Arabic" },
  { code: "am",  native: "አማርኛ",          english: "Amharic" },
  { code: "bg",  native: "Български",      english: "Bulgarian" },
  { code: "bn",  native: "বাংলা",          english: "Bengali" },
  { code: "cs",  native: "Čeština",        english: "Czech" },
  { code: "da",  native: "Dansk",          english: "Danish" },
  { code: "de",  native: "Deutsch",        english: "German" },
  { code: "el",  native: "Ελληνικά",       english: "Greek" },
  { code: "es",  native: "Español",        english: "Spanish" },
  { code: "fa",  native: "فارسی",          english: "Persian" },
  { code: "fi",  native: "Suomi",          english: "Finnish" },
  { code: "fil", native: "Filipino",       english: "Filipino" },
  { code: "fr",  native: "Français",       english: "French" },
  { code: "gu",  native: "ગુજરાતી",        english: "Gujarati" },
  { code: "ha",  native: "Hausa",          english: "Hausa" },
  { code: "he",  native: "עברית",          english: "Hebrew" },
  { code: "hi",  native: "हिन्दी",          english: "Hindi" },
  { code: "hr",  native: "Hrvatski",       english: "Croatian" },
  { code: "hu",  native: "Magyar",         english: "Hungarian" },
  { code: "id",  native: "Bahasa Indonesia", english: "Indonesian" },
  { code: "it",  native: "Italiano",       english: "Italian" },
  { code: "ja",  native: "日本語",          english: "Japanese" },
  { code: "kk",  native: "Қазақша",         english: "Kazakh" },
  { code: "ko",  native: "한국어",          english: "Korean" },
  { code: "mr",  native: "मराठी",          english: "Marathi" },
  { code: "ms",  native: "Bahasa Melayu",  english: "Malay" },
  { code: "my",  native: "မြန်မာ",          english: "Burmese" },
  { code: "ne",  native: "नेपाली",          english: "Nepali" },
  { code: "nl",  native: "Nederlands",     english: "Dutch" },
  { code: "no",  native: "Norsk",          english: "Norwegian" },
  { code: "pa",  native: "ਪੰਜਾਬੀ",         english: "Punjabi" },
  { code: "pl",  native: "Polski",         english: "Polish" },
  { code: "pt",  native: "Português",      english: "Portuguese" },
  { code: "ro",  native: "Română",         english: "Romanian" },
  { code: "ru",  native: "Русский",        english: "Russian" },
  { code: "sk",  native: "Slovenčina",     english: "Slovak" },
  { code: "sl",  native: "Slovenščina",    english: "Slovenian" },
  { code: "sr",  native: "Српски",         english: "Serbian" },
  { code: "sv",  native: "Svenska",        english: "Swedish" },
  { code: "sw",  native: "Kiswahili",      english: "Swahili" },
  { code: "ta",  native: "தமிழ்",          english: "Tamil" },
  { code: "te",  native: "తెలుగు",         english: "Telugu" },
  { code: "th",  native: "ไทย",            english: "Thai" },
  { code: "tr",  native: "Türkçe",         english: "Turkish" },
  { code: "uk",  native: "Українська",     english: "Ukrainian" },
  { code: "ur",  native: "اردو",           english: "Urdu" },
  { code: "uz",  native: "O‘zbek",         english: "Uzbek" },
  { code: "vi",  native: "Tiếng Việt",     english: "Vietnamese" },
  { code: "zh",  native: "中文",           english: "Chinese" },
];
export type TimeFormat = "12h" | "24h";
export type DateFormat = "YYYY-MM-DD" | "DD/MM/YYYY" | "MM/DD/YYYY";
export type FontSize = "small" | "medium" | "large" | "xl";
export type RecordingFormat = "mp4" | "webm";
export type JoinSound = "none" | "chime" | "ping" | "doorbell";
export type ForceIP = "auto" | "v4" | "v6";

export interface Preferences {
  // ── Audio & Video defaults when joining a meeting ─────────────────
  av: {
    cameraOnByDefault: boolean;
    micOnByDefault: boolean;
    noiseSuppression: boolean;
    echoCancellation: boolean;
    autoGainControl: boolean;
    defaultBackground: BackgroundEffect;
    preferredCameraId: string | null;
    preferredMicId: string | null;
    preferredSpeakerId: string | null;
    defaultVolume: number; // 0–100
    pushToTalk: boolean;
    pushToTalkKey: string; // e.g. "Space"
    mirrorPreview: boolean;
  };

  // ── Display preferences during a meeting ──────────────────────────
  display: {
    layout: Layout;
    mirrorOwnVideo: boolean;
    showParticipantNames: boolean;
    hideSelfView: boolean;
    pinFirstScreenshare: boolean;
    hideEmptyTiles: boolean;
    showConnectionQuality: boolean;
    showMeetingClock: boolean;
    highlightSpeaker: boolean;
    maxVisibleTiles: number; // 4–50
  };

  // ── Defaults applied when this user creates a new meeting ─────────
  meetingDefaults: {
    maxParticipants: number; // 2–50
    requirePassword: boolean;
    recordingMode: RecordingMode;
    autoEndMinutes: number | null; // null = no auto-end
    greeting: string;
    welcomeMessage: string;
    enableChat: boolean;
    enableReactions: boolean;
    enableScreenshare: boolean;
  };

  // ── Moderation (owner) ────────────────────────────────────────────
  moderation: {
    autoAdmitAuthenticated: boolean;
    requireNameOnJoin: boolean;
    autoMuteNewJoiners: boolean;
    autoDisableCameraForNew: boolean;
    waitingRoomEnabled: boolean;
    lockRoomAfterStart: boolean;
    allowParticipantScreenshare: boolean;
    allowParticipantChat: boolean;
  };

  // ── Recording (owner) ─────────────────────────────────────────────
  recording: {
    format: RecordingFormat;
    audioOnly: boolean;
    includeChat: boolean;
    recordScreenshareSeparately: boolean;
    captionsInRecording: boolean;
    noticeParticipantsOnStart: boolean;
  };

  // ── Notification behaviour ────────────────────────────────────────
  notifications: {
    soundOnJoin: boolean;
    browserNotificationOnJoin: boolean;
    highlightModeratorActions: boolean;
    joinSound: JoinSound;
    notificationVolume: number; // 0–100
    doNotDisturbStart: string | null; // "HH:mm" or null
    doNotDisturbEnd: string | null;
    ignoreOwnJoins: boolean;
    chatMessageSound: boolean;
  };

  // ── Privacy & data retention ──────────────────────────────────────
  privacy: {
    secureModeByDefault: boolean;
    recordingRetentionDaysOverride: number | null;
    anonymiseEmailInJoinLog: boolean;
    dontLogMyIp: boolean;
    disableAnalytics: boolean;
    disableReadReceipts: boolean;
    blurEmailInScreenshots: boolean;
  };

  // ── Accessibility ─────────────────────────────────────────────────
  accessibility: {
    highContrast: boolean;
    reducedMotion: boolean;
    liveCaptions: boolean;
    captionsFontSize: FontSize;
    announceParticipantEvents: boolean;
    keyboardFocusOutlines: boolean;
    monoAudio: boolean;
  };

  // ── Keyboard & input ──────────────────────────────────────────────
  keyboard: {
    enableShortcuts: boolean;
    muteToggleKey: string;
    cameraToggleKey: string;
    handRaiseKey: string;
    leaveMeetingKey: string;
    screenshareKey: string;
    scrollWheelZoomTiles: boolean;
  };

  // ── Network & quality ─────────────────────────────────────────────
  network: {
    preferredVideoQuality: VideoQuality;
    simulcastEnabled: boolean;
    bandwidthLimitKbps: number | null; // null = unlimited
    forceRelay: boolean; // always use TURN
    forceIpVersion: ForceIP;
    disableHardwareAcceleration: boolean;
    prewarmIceOnPageLoad: boolean;
    reconnectAttempts: number; // 0–10
  };

  // ── Language & locale ─────────────────────────────────────────────
  locale: {
    language: Language;
    timeFormat: TimeFormat;
    dateFormat: DateFormat;
    timezone: string; // e.g. "Europe/Brussels", or "auto"
    firstDayOfWeek: 0 | 1; // 0 = Sunday, 1 = Monday
    numberLocale: string; // e.g. "en-US"
  };

  // ── Chat (feature not yet implemented — prefs saved for later) ────
  chat: {
    saveHistoryLocally: boolean;
    emojiPickerEnabled: boolean;
    markdownRendering: boolean;
    linkPreviews: boolean;
    sendOnEnter: boolean; // false → Ctrl/Cmd+Enter to send
    autoTranslate: boolean;
  };

  // ── Appearance & theme ────────────────────────────────────────────
  appearance: {
    theme: Theme;
    accentColor: string; // hex
    compactMode: boolean;
    fontSize: FontSize;
    roundedAvatars: boolean;
    backgroundOpacity: number; // 0–100
  };

  // ── Developer / debug ─────────────────────────────────────────────
  developer: {
    showStatsOverlay: boolean;
    verboseLogging: boolean;
    experimentalFeatures: boolean;
    webrtcInternalsShortcut: boolean;
    persistLogsLocally: boolean;
  };
}

export const defaults: Preferences = {
  av: {
    cameraOnByDefault: true,
    micOnByDefault: true,
    noiseSuppression: true,
    echoCancellation: true,
    autoGainControl: true,
    defaultBackground: "off",
    preferredCameraId: null,
    preferredMicId: null,
    preferredSpeakerId: null,
    defaultVolume: 80,
    pushToTalk: false,
    pushToTalkKey: "Space",
    mirrorPreview: true,
  },
  display: {
    layout: "auto",
    mirrorOwnVideo: true,
    showParticipantNames: true,
    hideSelfView: false,
    pinFirstScreenshare: true,
    hideEmptyTiles: false,
    showConnectionQuality: true,
    showMeetingClock: false,
    highlightSpeaker: true,
    maxVisibleTiles: 16,
  },
  meetingDefaults: {
    maxParticipants: 50,
    requirePassword: false,
    recordingMode: "manual",
    autoEndMinutes: null,
    greeting: "",
    welcomeMessage: "",
    enableChat: true,
    enableReactions: true,
    enableScreenshare: true,
  },
  moderation: {
    autoAdmitAuthenticated: true,
    requireNameOnJoin: true,
    autoMuteNewJoiners: false,
    autoDisableCameraForNew: false,
    waitingRoomEnabled: false,
    lockRoomAfterStart: false,
    allowParticipantScreenshare: true,
    allowParticipantChat: true,
  },
  recording: {
    format: "mp4",
    audioOnly: false,
    includeChat: true,
    recordScreenshareSeparately: false,
    captionsInRecording: false,
    noticeParticipantsOnStart: true,
  },
  notifications: {
    soundOnJoin: true,
    browserNotificationOnJoin: false,
    highlightModeratorActions: true,
    joinSound: "chime",
    notificationVolume: 60,
    doNotDisturbStart: null,
    doNotDisturbEnd: null,
    ignoreOwnJoins: true,
    chatMessageSound: true,
  },
  privacy: {
    secureModeByDefault: false,
    recordingRetentionDaysOverride: null,
    anonymiseEmailInJoinLog: false,
    dontLogMyIp: false,
    disableAnalytics: false,
    disableReadReceipts: false,
    blurEmailInScreenshots: false,
  },
  accessibility: {
    highContrast: false,
    reducedMotion: false,
    liveCaptions: false,
    captionsFontSize: "medium",
    announceParticipantEvents: true,
    keyboardFocusOutlines: true,
    monoAudio: false,
  },
  keyboard: {
    enableShortcuts: true,
    muteToggleKey: "Ctrl+D",
    cameraToggleKey: "Ctrl+E",
    handRaiseKey: "Ctrl+H",
    leaveMeetingKey: "Ctrl+Shift+L",
    screenshareKey: "Ctrl+Shift+S",
    scrollWheelZoomTiles: false,
  },
  network: {
    preferredVideoQuality: "auto",
    simulcastEnabled: true,
    bandwidthLimitKbps: null,
    forceRelay: false,
    forceIpVersion: "auto",
    disableHardwareAcceleration: false,
    prewarmIceOnPageLoad: true,
    reconnectAttempts: 5,
  },
  locale: {
    language: "en",
    timeFormat: "24h",
    dateFormat: "YYYY-MM-DD",
    timezone: "auto",
    firstDayOfWeek: 1,
    numberLocale: "en-US",
  },
  chat: {
    saveHistoryLocally: true,
    emojiPickerEnabled: true,
    markdownRendering: true,
    linkPreviews: false,
    sendOnEnter: true,
    autoTranslate: false,
  },
  appearance: {
    theme: "dark",
    accentColor: "#2563eb",
    compactMode: false,
    fontSize: "medium",
    roundedAvatars: true,
    backgroundOpacity: 100,
  },
  developer: {
    showStatsOverlay: false,
    verboseLogging: false,
    experimentalFeatures: false,
    webrtcInternalsShortcut: false,
    persistLogsLocally: false,
  },
};

type SectionKey = keyof Preferences;
type Setter<K extends SectionKey> = (p: Partial<Preferences[K]>) => void;

interface PreferencesState extends Preferences {
  reset: () => void;
  setAv: Setter<"av">;
  setDisplay: Setter<"display">;
  setMeetingDefaults: Setter<"meetingDefaults">;
  setModeration: Setter<"moderation">;
  setRecording: Setter<"recording">;
  setNotifications: Setter<"notifications">;
  setPrivacy: Setter<"privacy">;
  setAccessibility: Setter<"accessibility">;
  setKeyboard: Setter<"keyboard">;
  setNetwork: Setter<"network">;
  setLocale: Setter<"locale">;
  setChat: Setter<"chat">;
  setAppearance: Setter<"appearance">;
  setDeveloper: Setter<"developer">;
}

// Lazily import i18n only after initialization to avoid circular import order.
// `usePreferences.subscribe` (configured below) calls `i18n.changeLanguage`
// whenever the user picks a new language in Settings.
let _i18n: { changeLanguage: (lng: string) => void } | null = null;
function notifyLanguageChange(lng: string) {
  if (!_i18n) {
    void import("../i18n").then((m) => {
      _i18n = m.default;
      _i18n.changeLanguage(lng);
    });
  } else {
    _i18n.changeLanguage(lng);
  }
  // Persist server-side for authenticated users; this also flips the
  // `language_set_manually` flag so future sessions stop falling back to
  // browser-language detection.
  void import("./auth").then(({ isAuthenticated }) => {
    if (!isAuthenticated()) return;
    void import("./api").then(({ api }) => {
      api.updateMyPreferences({ language: lng }).catch(() => {
        /* offline / API down — picker still works locally */
      });
    });
  });
}

export const usePreferences = create<PreferencesState>()(
  persist(
    (set) => ({
      ...defaults,
      reset: () => set({ ...defaults }),
      setAv: (p) => set((s) => ({ av: { ...s.av, ...p } })),
      setDisplay: (p) => set((s) => ({ display: { ...s.display, ...p } })),
      setMeetingDefaults: (p) => set((s) => ({ meetingDefaults: { ...s.meetingDefaults, ...p } })),
      setModeration: (p) => set((s) => ({ moderation: { ...s.moderation, ...p } })),
      setRecording: (p) => set((s) => ({ recording: { ...s.recording, ...p } })),
      setNotifications: (p) => set((s) => ({ notifications: { ...s.notifications, ...p } })),
      setPrivacy: (p) => set((s) => ({ privacy: { ...s.privacy, ...p } })),
      setAccessibility: (p) => set((s) => ({ accessibility: { ...s.accessibility, ...p } })),
      setKeyboard: (p) => set((s) => ({ keyboard: { ...s.keyboard, ...p } })),
      setNetwork: (p) => set((s) => ({ network: { ...s.network, ...p } })),
      setLocale: (p) =>
        set((s) => {
          const next = { ...s.locale, ...p };
          if (p.language && p.language !== s.locale.language) {
            notifyLanguageChange(p.language);
          }
          return { locale: next };
        }),
      setChat: (p) => set((s) => ({ chat: { ...s.chat, ...p } })),
      setAppearance: (p) => set((s) => ({ appearance: { ...s.appearance, ...p } })),
      setDeveloper: (p) => set((s) => ({ developer: { ...s.developer, ...p } })),
    }),
    {
      name: "meet-preferences-v1",
      storage: createJSONStorage(() => localStorage),
      version: 1,
    }
  )
);

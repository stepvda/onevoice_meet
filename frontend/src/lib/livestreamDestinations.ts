/**
 * Metadata for each supported livestream destination. Both CreateMeeting and
 * LivestreamSettingsModal iterate over `LIVESTREAM_DESTINATIONS` so adding a
 * new platform = three new columns on the backend Meeting model + one entry
 * here + the corresponding rows in `egress_mgr.LIVESTREAM_DESTINATIONS` and
 * the `MeetingOut`/`createMeeting`/`updateMeeting` field lists.
 *
 * `id` is just a UI key; `fields` carries the actual backend column names so
 * X.com can keep its legacy unprefixed columns (`livestream_enabled` etc.)
 * while every other destination uses a `_<platform>_` prefix.
 */

export type LivestreamFields = {
  enabled: string;
  rtmps_url: string;
  stream_key: string;
};

export type LivestreamDestination = {
  id: "x" | "substack" | "youtube" | "facebook" | "rumble";
  // Backend column names for this destination on the Meeting row.
  fields: LivestreamFields;
  // i18n key + default text for the toggle label ("Stream to <platform>").
  toggleI18nKey: string;
  toggleDefault: string;
  // Field labels.
  urlLabel: { key: string; def: string };
  keyLabel: { key: string; def: string };
  urlPlaceholder: string;
  keyPlaceholder: string;
  // Help block: heading + ordered steps.
  helpTitle: { key: string; def: string };
  steps: { key: string; def: string }[];
};

export const LIVESTREAM_DESTINATIONS: LivestreamDestination[] = [
  {
    id: "x",
    fields: {
      enabled: "livestream_enabled",
      rtmps_url: "livestream_rtmps_url",
      stream_key: "livestream_stream_key",
    },
    toggleI18nKey: "createMeeting.livestreamEnableX",
    toggleDefault: "Stream to X.com",
    urlLabel: { key: "createMeeting.livestreamUrl", def: "RTMPS URL" },
    keyLabel: { key: "createMeeting.livestreamKey", def: "Stream key" },
    urlPlaceholder: "rtmps://va.pscp.tv:443/x",
    keyPlaceholder: "abcd-1234-…",
    helpTitle: { key: "createMeeting.livestreamWhereTitle", def: "Where to find these on X (Twitter):" },
    steps: [
      { key: "createMeeting.livestreamStep1", def: "Open studio.x.com and sign in." },
      { key: "createMeeting.livestreamStep2", def: "Click “Producer” in the left sidebar, then “Create broadcast”." },
      {
        key: "createMeeting.livestreamStep3",
        def: "Under “Source”, choose “External encoder”. X shows an RTMPS URL and a stream key — copy them into the two fields above.",
      },
      {
        key: "createMeeting.livestreamStep4",
        def: "The key is single-use per broadcast: regenerate it on studio.x.com if you reuse this meeting later.",
      },
    ],
  },
  {
    id: "substack",
    fields: {
      enabled: "livestream_substack_enabled",
      rtmps_url: "livestream_substack_rtmps_url",
      stream_key: "livestream_substack_stream_key",
    },
    toggleI18nKey: "createMeeting.substackEnable",
    toggleDefault: "Stream to Substack",
    urlLabel: { key: "createMeeting.substackUrl", def: "RTMP URL" },
    keyLabel: { key: "createMeeting.substackKey", def: "Stream key" },
    urlPlaceholder: "rtmp://live.substack.com/live",
    keyPlaceholder: "sub_live_…",
    helpTitle: { key: "createMeeting.substackWhereTitle", def: "Where to find these on Substack:" },
    steps: [
      { key: "createMeeting.substackStep1", def: "Sign in to substack.com and open your publication dashboard." },
      {
        key: "createMeeting.substackStep2",
        def: "Click “New post” → “Live video” (or open Notes → the camera icon → Go live).",
      },
      {
        key: "createMeeting.substackStep3",
        def: "Choose “Stream from external software (RTMP)”. Substack shows a server URL and a stream key — paste them above.",
      },
      {
        key: "createMeeting.substackStep4",
        def: "Live video on Substack requires a paid publication or the Notes Live feature on your plan. The key is per-broadcast — generate a new one each session.",
      },
    ],
  },
  {
    id: "youtube",
    fields: {
      enabled: "livestream_youtube_enabled",
      rtmps_url: "livestream_youtube_rtmps_url",
      stream_key: "livestream_youtube_stream_key",
    },
    toggleI18nKey: "createMeeting.youtubeEnable",
    toggleDefault: "Stream to YouTube Live",
    urlLabel: { key: "createMeeting.youtubeUrl", def: "RTMP URL" },
    keyLabel: { key: "createMeeting.youtubeKey", def: "Stream key" },
    urlPlaceholder: "rtmp://a.rtmp.youtube.com/live2",
    keyPlaceholder: "xxxx-xxxx-xxxx-xxxx-xxxx",
    helpTitle: { key: "createMeeting.youtubeWhereTitle", def: "Where to find these on YouTube:" },
    steps: [
      {
        key: "createMeeting.youtubeStep1",
        def: "Go to studio.youtube.com and sign in. Live streaming must be enabled on your channel (phone verification + a 24h wait the first time).",
      },
      {
        key: "createMeeting.youtubeStep2",
        def: "Click the “Create” camera icon (top right) → “Go live”. This opens Live Control Room.",
      },
      {
        key: "createMeeting.youtubeStep3",
        def: "Choose “Stream” in the left sidebar (not Webcam). Under “Stream settings”, copy the Stream URL (`rtmp://a.rtmp.youtube.com/live2`) and the Stream key into the fields above.",
      },
      {
        key: "createMeeting.youtubeStep4",
        def: "Your stream key is reusable across broadcasts, but you can reset it with the refresh icon next to it. Don't share it.",
      },
    ],
  },
  {
    id: "facebook",
    fields: {
      enabled: "livestream_facebook_enabled",
      rtmps_url: "livestream_facebook_rtmps_url",
      stream_key: "livestream_facebook_stream_key",
    },
    toggleI18nKey: "createMeeting.facebookEnable",
    toggleDefault: "Stream to Facebook Live",
    urlLabel: { key: "createMeeting.facebookUrl", def: "RTMPS URL" },
    keyLabel: { key: "createMeeting.facebookKey", def: "Stream key" },
    urlPlaceholder: "rtmps://live-api-s.facebook.com:443/rtmp/",
    keyPlaceholder: "FB-xxxx-…",
    helpTitle: { key: "createMeeting.facebookWhereTitle", def: "Where to find these on Facebook:" },
    steps: [
      {
        key: "createMeeting.facebookStep1",
        def: "Open facebook.com/live/producer (Live Producer). Sign in if needed.",
      },
      {
        key: "createMeeting.facebookStep2",
        def: "Pick where the stream goes (your timeline, a Page, or a Group) and click “Go Live”.",
      },
      {
        key: "createMeeting.facebookStep3",
        def: "Under “Select Video Source”, choose “Streaming software”. Copy the Server URL and Persistent Stream Key into the fields above.",
      },
      {
        key: "createMeeting.facebookStep4",
        def: "Facebook's persistent key is reusable for up to 7 days of idle time. For higher security, click “Use a Single-Use Key” and regenerate per broadcast.",
      },
    ],
  },
  {
    id: "rumble",
    fields: {
      enabled: "livestream_rumble_enabled",
      rtmps_url: "livestream_rumble_rtmps_url",
      stream_key: "livestream_rumble_stream_key",
    },
    toggleI18nKey: "createMeeting.rumbleEnable",
    toggleDefault: "Stream to Rumble",
    urlLabel: { key: "createMeeting.rumbleUrl", def: "RTMP URL" },
    keyLabel: { key: "createMeeting.rumbleKey", def: "Stream key" },
    urlPlaceholder: "rtmp://live.rumble.com/live",
    keyPlaceholder: "<your-channel-key>",
    helpTitle: { key: "createMeeting.rumbleWhereTitle", def: "Where to find these on Rumble:" },
    steps: [
      {
        key: "createMeeting.rumbleStep1",
        def: "Sign in to rumble.com, click your avatar (top right) → “Studio”, or go directly to rumble.com/studio.",
      },
      {
        key: "createMeeting.rumbleStep2",
        def: "Click “Go Live” → choose “Use RTMP encoder”.",
      },
      {
        key: "createMeeting.rumbleStep3",
        def: "Rumble shows the Server URL (`rtmp://live.rumble.com/live`) and a Stream Key — copy both into the fields above.",
      },
      {
        key: "createMeeting.rumbleStep4",
        def: "The stream key is per-channel and persistent. You can regenerate it from the same Studio page; doing so invalidates any encoder still using the old key.",
      },
    ],
  },
];

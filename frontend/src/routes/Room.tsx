import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  ControlBar,
  LiveKitRoom,
  RoomAudioRenderer,
  useRoomContext,
} from "@livekit/components-react";
import { RoomEvent } from "livekit-client";
import {
  Check,
  CircleStopIcon,
  Link2,
  Mail,
  MessageSquare,
  MicOff,
  Pin,
  PinOff,
  Radio,
  Film,
  FileText,
  Settings as SettingsIcon,
  UserPlus,
  Users,
  Vote,
} from "lucide-react";
import { clearPendingToken, clearRoomMeta, loadPendingToken, loadRoomMeta } from "./Lobby";
import { roomOptions } from "../lib/livekit";
import { api, MeetingOut } from "../lib/api";
import { LIVESTREAM_DESTINATIONS } from "../lib/livestreamDestinations";
import { usePreferences } from "../lib/preferences";
import PresenterSpotlight from "../components/PresenterSpotlight";
import BackgroundPicker, { processorsSupported } from "../components/BackgroundPicker";
import RecordingIndicator from "../components/RecordingIndicator";
import StreamingIndicator from "../components/StreamingIndicator";
import ChatPanel from "../components/ChatPanel";
import ParticipantsPanel from "../components/ParticipantsPanel";
import InMeetingSettings from "../components/InMeetingSettings";
import InviteModal from "../components/InviteModal";
import LivestreamSettingsModal from "../components/LivestreamSettingsModal";
import MeetingRecordingsModal from "../components/MeetingRecordingsModal";
import VideoPlaybackPanel from "../components/VideoPlaybackPanel";
import OutputVolumeControl from "../components/OutputVolumeControl";
import AudioWaveform from "../components/AudioWaveform";
import PendingJoinersPanel from "../components/PendingJoinersPanel";
import HandRaiseButton from "../components/HandRaiseButton";
import PostMeetingFeedback from "../components/PostMeetingFeedback";
import ShortcutOverlay from "../components/ShortcutOverlay";
import { useMeetingShortcuts } from "../lib/shortcuts";
import ReactionsButton from "../components/ReactionsButton";
import FloatingReactions from "../components/FloatingReactions";
import PipButton from "../components/PipButton";
import DeviceSwitcher from "../components/DeviceSwitcher";
import PollsQnaPanel, { POLLS_TOPIC } from "../components/PollsQnaPanel";
import NotesWhiteboardPanel, { BOARD_TOPIC, NOTES_TOPIC } from "../components/NotesWhiteboardPanel";
import MeetingClock from "../components/MeetingClock";
import CaptionsOverlay from "../components/CaptionsOverlay";
import PushToTalkIndicator from "../components/PushToTalkIndicator";
import { useJoinSound, useChatSound } from "../lib/sounds";
import { useMonoAudio } from "../lib/monoAudio";
import { useVideoQualityPref } from "../lib/videoQualityPref";
import { usePushToTalk } from "../lib/pushToTalk";
import { useBrowserNotifications } from "../lib/browserNotifications";
import { useJoinPolicy } from "../lib/joinPolicy";

interface InnerProps {
  meetingId: string | null;
  isOwner: boolean;
  meetingTitle: string | null;
  brandingUrl: string | null;
  roomName: string;
  onCaptureFeedbackInfo?: (info: { identity: string | null; name: string | null }) => void;
}

function InnerRoom({ meetingId, isOwner, meetingTitle, brandingUrl, roomName, onCaptureFeedbackInfo }: InnerProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const room = useRoomContext();
  const display = usePreferences((s) => s.display);
  const appearance = usePreferences((s) => s.appearance);
  const accessibility = usePreferences((s) => s.accessibility);
  // Activate the side-effect hooks that translate prefs into runtime behaviour.
  useJoinSound(room);
  useChatSound(room);
  useMonoAudio();
  useVideoQualityPref(room);
  usePushToTalk(room);
  useBrowserNotifications(room);
  useJoinPolicy(room);

  // Capture identity/name into the parent once connected so the post-meeting
  // feedback modal can attach them after LiveKit unmounts on disconnect.
  useEffect(() => {
    if (!onCaptureFeedbackInfo) return;
    onCaptureFeedbackInfo({
      identity: room.localParticipant.identity ?? null,
      name: room.localParticipant.name ?? null,
    });
  }, [room, onCaptureFeedbackInfo]);

  // Warm up the reliable data channel right after connect. LiveKit's SCTP
  // channel is negotiated lazily on the first `publishData` call, which
  // means the very first packet can be silently dropped before the
  // negotiation completes (the second works because the channel is open
  // by then). The whiteboard exposed this because its first stroke fires
  // immediately on pointer-up — there's no leading send to absorb the
  // negotiation cost. A zero-byte packet on a throwaway topic forces the
  // negotiation to happen ahead of any real send.
  useEffect(() => {
    const warmup = () => {
      void room.localParticipant
        .publishData(new Uint8Array(0), { reliable: true, topic: "meet-warmup" })
        .catch(() => undefined);
    };
    if (room.state === "connected") {
      warmup();
    } else {
      const onConnected = () => {
        warmup();
        room.off(RoomEvent.Connected, onConnected);
      };
      room.on(RoomEvent.Connected, onConnected);
      return () => {
        room.off(RoomEvent.Connected, onConnected);
      };
    }
  }, [room]);

  // Auto-open the Notes/Whiteboard and Polls/Q&A panels for everyone when
  // any remote participant creates OR updates anything. Routes to the
  // correct tab based on which topic/kind triggered the open:
  //   meet-notes  → notes panel, "notes" tab
  //   meet-board  → notes panel, "board" tab
  //   meet-polls  → polls panel, "polls" or "qna" tab (from payload.kind)
  // LiveKit doesn't echo local data, so this only fires from remote peers.
  const [notesInitialTab, setNotesInitialTab] = useState<"notes" | "board" | null>(null);
  const [pollsInitialTab, setPollsInitialTab] = useState<"polls" | "qna" | null>(null);
  useEffect(() => {
    const decoder = new TextDecoder();
    const onData = (
      payload: Uint8Array,
      _participant: unknown,
      _kind: unknown,
      topic?: string,
    ) => {
      if (topic === NOTES_TOPIC) {
        setNotesInitialTab("notes");
        setNotesOpen(true);
      } else if (topic === BOARD_TOPIC) {
        setNotesInitialTab("board");
        setNotesOpen(true);
      } else if (topic === POLLS_TOPIC) {
        // Default to the polls tab; if the payload says "question", show
        // the Q&A tab instead.
        let tab: "polls" | "qna" = "polls";
        try {
          const obj = JSON.parse(decoder.decode(payload)) as { kind?: string };
          if (obj?.kind === "question") tab = "qna";
        } catch {
          /* malformed payload — fall back to the polls tab */
        }
        setPollsInitialTab(tab);
        setPollsOpen(true);
      } else if (topic === "meet-playback") {
        // Video playback signals from the server: start / next / end.
        // On `playback-start` we force-mute the local mic + camera so the
        // viewer doesn't accidentally bleed into the shared video; the
        // SFU is also force-muting their tracks server-side but updating
        // local LiveKit state keeps the buttons in sync. On `playback-end`
        // we leave their state alone — re-enabling is their call.
        try {
          const obj = JSON.parse(decoder.decode(payload)) as {
            type?: string;
            filename?: string;
          };
          if (obj?.type === "playback-start" || obj?.type === "playback-next") {
            setPlaybackActive(true);
            if (obj.type === "playback-start") {
              void room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
              void room.localParticipant.setCameraEnabled(false).catch(() => undefined);
            }
          } else if (obj?.type === "playback-end") {
            setPlaybackActive(false);
          }
        } catch {
          /* ignore malformed playback packet */
        }
      } else if (topic === "meet-cohost") {
        // Owner just promoted (or demoted) this user. The current
        // LiveKit token either lacks the room_admin grant they just
        // received, or still carries one they just lost — only the
        // Lobby flow mints / strips it on rejoin. We surface a banner
        // asking them to refresh so the toolbar reflects the new role.
        try {
          const obj = JSON.parse(decoder.decode(payload)) as { type?: string };
          if (obj?.type === "promoted") setCohostNotice("promoted");
          else if (obj?.type === "demoted") setCohostNotice("demoted");
        } catch {
          /* ignore malformed */
        }
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room]);

  useMeetingShortcuts({
    room,
    onToggleScreenShare: () => {
      const lp = room.localParticipant;
      void lp.setScreenShareEnabled(!lp.isScreenShareEnabled);
    },
    onLeave: () => {
      void room.disconnect();
    },
    onOpenHelp: () => setShortcutsOpen(true),
  });
  const [recordingActive, setRecordingActive] = useState(false);
  // `recordingLayout` now mirrors the ROOM-WIDE composition layout
  // (mirrored to live viewers, the recording, and the livestream). The
  // host changes it via the toolbar picker, which calls api.setRoomLayout
  // — that persists on the meeting + writes LiveKit room metadata so
  // every connected client re-renders. Name kept for backward compat
  // with existing callers (startRecording / startStream pass it through).
  // Default "grid" — best fit for the typical multi-participant meeting
  // and matches user-facing intuition. Overridden once the meeting load
  // completes with the persisted `meeting.room_layout`.
  const [recordingLayout, setRecordingLayout] = useState<"speaker" | "grid" | "single-speaker">("grid");
  // Livestream-to-X.com. `livestreamEnabled` mirrors the meeting flag
  // (toolbar button only renders when true); `livestreamActive` flips when
  // the egress is running. We poll on mount/owner-only because the meeting
  // metadata doesn't (yet) include livestream state.
  const [livestreamEnabled, setLivestreamEnabled] = useState(false);
  const [livestreamActive, setLivestreamActive] = useState(false);
  const [livestreamSettingsOpen, setLivestreamSettingsOpen] = useState(false);
  const [recordingsModalOpen, setRecordingsModalOpen] = useState(false);
  const [livestreamMeeting, setLivestreamMeeting] = useState<MeetingOut | null>(null);
  // Video playback. `playbackEnabled` mirrors the meeting toggle —
  // when off, the Playlist toolbar button stays hidden. `playbackActive`
  // is whether an ingress is currently publishing (drives a small
  // pulsing indicator on the Playlist button itself; the side panel
  // owns the now-playing highlight and progress bar).
  const [playbackEnabled, setPlaybackEnabled] = useState(false);
  const [playbackActive, setPlaybackActive] = useState(false);
  const [playbackPanelOpen, setPlaybackPanelOpen] = useState(false);
  // "promoted" | "demoted" | null. When the owner toggles this user's
  // co-host status mid-session, the LiveKit token already in hand still
  // reflects the OLD role (room_admin grant either present or absent —
  // only the Lobby flow flips it on rejoin). The banner asks the user
  // to rejoin so the moderator toolbar reflects reality.
  const [cohostNotice, setCohostNotice] = useState<"promoted" | "demoted" | null>(null);
  const [pendingOpen, setPendingOpen] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [pollsOpen, setPollsOpen] = useState(false);
  const [notesOpen, setNotesOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [participantsOpen, setParticipantsOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const errClear = useRef<number | null>(null);
  const copiedClear = useRef<number | null>(null);

  async function copyMeetingLink() {
    const url = `${window.location.origin}/${roomName}`;
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      window.prompt(t("room.copyLinkPrompt", { defaultValue: "Copy this link" }), url);
    }
    setCopied(true);
    if (copiedClear.current) window.clearTimeout(copiedClear.current);
    copiedClear.current = window.setTimeout(() => setCopied(false), 1500);
  }

  function showErr(m: string) {
    setErr(m);
    if (errClear.current) window.clearTimeout(errClear.current);
    errClear.current = window.setTimeout(() => setErr(null), 4000);
  }

  // Load this meeting's livestream config (owner only). The config drives
  // whether the Start/Stop streaming button appears in the toolbar at all,
  // and the LivestreamSettingsModal needs the full row to pre-fill itself.
  useEffect(() => {
    if (!isOwner || !meetingId) return;
    let cancelled = false;
    void (async () => {
      try {
        // Single-meeting fetch instead of listMeetings + client-side find —
        // saves transferring every meeting the owner has just to read one.
        const mine = await api.getMeeting(meetingId);
        if (cancelled || !mine) return;
        setLivestreamMeeting(mine);
        // Show the toolbar button if ANY destination's toggle is on. The
        // egress validation on the backend additionally requires URL+key,
        // but at the UI layer "any toggle on" is enough to surface the
        // button — if creds are missing the start call returns a 400 with
        // a clear error which we already toast via `withBusy`.
        const anyEnabled = LIVESTREAM_DESTINATIONS.some(
          (d) => !!(mine as unknown as Record<string, unknown>)[d.fields.enabled],
        );
        setLivestreamEnabled(anyEnabled);
        setLivestreamActive(!!mine.livestream_active);
        setPlaybackEnabled(!!mine.playback_enabled);
        setPlaybackActive(!!mine.playback_active);
        // Seed the layout picker from the persisted room_layout. This is
        // the source of truth for what every live viewer + the recording
        // + the livestream are using. Fall back to current_egress_layout
        // for rooms persisted before room_layout existed.
        const seeded =
          mine.room_layout ?? mine.current_egress_layout ?? null;
        if (
          seeded === "speaker" ||
          seeded === "grid" ||
          seeded === "single-speaker"
        ) {
          setRecordingLayout(seeded);
        }
      } catch {
        /* surface as no button — keeps the toolbar usable */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isOwner, meetingId]);

  async function toggleLivestream() {
    if (!meetingId) return;
    if (livestreamActive) {
      await withBusy("stream-stop", async () => {
        await api.stopStream(meetingId);
        setLivestreamActive(false);
      });
    } else {
      await withBusy("stream-start", async () => {
        await api.startStream(meetingId, { layout: recordingLayout });
        setLivestreamActive(true);
      });
    }
  }


  // Mirror room.metadata.recording_active + streaming_active locally so
  // every moderator's toolbar reflects the live state. Without this the
  // second co-host's button stays on "Start streaming" even after the
  // first co-host has actually started — clicking then gets a 409
  // "livestream already in progress" instead of toggling stop.
  useEffect(() => {
    const read = () => {
      try {
        const md = JSON.parse(room.metadata || "{}");
        setRecordingActive(!!md.recording_active);
        setLivestreamActive(!!md.streaming_active);
      } catch {
        setRecordingActive(false);
        setLivestreamActive(false);
      }
    };
    read();
    room.on(RoomEvent.RoomMetadataChanged, read);
    return () => {
      room.off(RoomEvent.RoomMetadataChanged, read);
    };
  }, [room]);

  async function withBusy(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      showErr((e as Error).message || t("room.requestFailed"));
    } finally {
      setBusy(null);
    }
  }

  async function toggleRecording() {
    if (!meetingId) return;
    if (recordingActive) {
      await withBusy("rec-stop", () => api.stopRecording(meetingId));
    } else {
      await withBusy("rec-start", () => api.startRecording(meetingId, { layout: recordingLayout }));
    }
  }

  // Current pinned presenter (room metadata), mirrored locally so the
  // toolbar button can flip its label between "Pin myself" and "Unpin"
  // and so other PresenterSpotlight changes (e.g. another co-host pinning
  // a guest) reflect immediately. Updates via the same metadata events
  // the composition layer already subscribes to.
  const [presenterId, setPresenterId] = useState<string | null>(null);
  useEffect(() => {
    const apply = () => {
      try {
        const md = JSON.parse(room.metadata || "{}");
        setPresenterId(
          typeof md.presenter_identity === "string" ? md.presenter_identity : null,
        );
      } catch {
        setPresenterId(null);
      }
    };
    apply();
    room.on(RoomEvent.RoomMetadataChanged, apply);
    room.on(RoomEvent.Connected, apply);
    room.on(RoomEvent.Reconnected, apply);
    return () => {
      room.off(RoomEvent.RoomMetadataChanged, apply);
      room.off(RoomEvent.Connected, apply);
      room.off(RoomEvent.Reconnected, apply);
    };
  }, [room]);

  const me = room.localParticipant.identity;
  const iAmPinned = presenterId === me;

  async function togglePin() {
    if (!meetingId) return;
    if (iAmPinned) {
      await withBusy("clear-presenter", () => api.setPresenter(meetingId, null));
    } else {
      await withBusy("pin-self", () => api.setPresenter(meetingId, me));
    }
  }

  async function muteAll() {
    if (!meetingId) return;
    await withBusy("mute-all", () => api.muteAll(meetingId));
  }

  async function endMeeting() {
    if (!meetingId) return;
    if (!confirm(t("room.endMeetingConfirm"))) return;
    await withBusy("end-meeting", () => api.endMeeting(meetingId));
    await room.disconnect();
    navigate("/");
  }

  return (
    <div className="flex flex-col h-dvh w-screen bg-witysk-page overflow-hidden">
      {/* TOP BAR */}
      <header
        data-testid="room-topbar"
        className="flex items-center gap-2 px-3 py-2 bg-primary-900/90 backdrop-blur border-b border-primary-700 flex-wrap flex-shrink-0"
      >
        <div className="flex items-center gap-3 mr-2 min-w-0 max-w-[55%]">
          {brandingUrl && (
            <img
              src={brandingUrl}
              alt={t("room.topbarBrandingAlt")}
              data-testid="topbar-branding"
              className="h-8 w-8 object-cover rounded-md border border-primary-700 flex-shrink-0"
            />
          )}
          <div className="min-w-0 leading-tight">
            <div
              className="font-semibold text-slate-50 truncate"
              data-testid="topbar-title"
              title={livestreamMeeting?.display_title || meetingTitle || ""}
            >
              {livestreamMeeting?.display_title || meetingTitle || "meet.witysk.org"}
            </div>
            <div className="text-xs text-slate-400">meet.witysk.org</div>
          </div>
          <AudioWaveform width={120} height={28} className="flex-shrink-0" />
          <RecordingIndicator />
          <StreamingIndicator />
        </div>

        <div className="flex-1" />

        {processorsSupported() && <BackgroundPicker />}

        {isOwner && meetingId && (
          <button
            type="button"
            onClick={() => setInviteOpen(true)}
            data-testid="btn-invite"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-primary-700 text-slate-100 hover:bg-primary-600"
            title={t("room.inviteTitle")}
            aria-label={t("room.invite")}
          >
            <Mail size={16} />
            <span className="hidden md:inline">{t("room.invite")}</span>
          </button>
        )}

        {isOwner && meetingId && (
          <button
            type="button"
            onClick={() => setPendingOpen((v) => !v)}
            data-testid="btn-pending"
            title={t("pending.toolbarTitle")}
            aria-label={t("pending.title")}
            className={[
              "relative inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
              pendingOpen
                ? "bg-primary-500 text-white"
                : pendingCount > 0
                ? "bg-amber-500/80 text-white hover:bg-amber-500"
                : "bg-primary-700 text-slate-100 hover:bg-primary-600",
            ].join(" ")}
          >
            <UserPlus size={16} />
            <span className="hidden md:inline">{t("pending.toolbarLabel")}</span>
            {pendingCount > 0 && (
              <span
                data-testid="pending-badge"
                className="absolute -top-1 -right-1 min-w-[18px] h-[18px] px-1 rounded-full bg-red-600 text-white text-[10px] font-bold flex items-center justify-center"
              >
                {pendingCount}
              </span>
            )}
          </button>
        )}

        {isOwner && (
          <>
            {/* Pin-myself toggle. When pinned (presenter_identity ==
                me on the room metadata), the new layout system shows me
                as the main video in single-speaker / speaker layouts
                regardless of who is currently talking. Toggling off
                hands the spotlight back to the active speaker. The old
                separate "Take Stage" + "Grid" pair has been merged here
                — Grid's name collided with the new layout picker. */}
            <button
              type="button"
              onClick={togglePin}
              data-testid="btn-pin-self"
              aria-pressed={iAmPinned}
              disabled={busy !== null}
              className={[
                "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium disabled:opacity-50",
                iAmPinned
                  ? "bg-accent-500 text-white hover:bg-accent-600"
                  : "bg-primary-700 text-slate-100 hover:bg-primary-600",
              ].join(" ")}
              title={
                iAmPinned
                  ? t("room.unpinTitle", {
                      defaultValue:
                        "Stop pinning yourself — the spotlight returns to the active speaker.",
                    })
                  : t("room.pinSelfTitle", {
                      defaultValue:
                        "Pin yourself as the main video for everyone, regardless of who is talking.",
                    })
              }
              aria-label={
                iAmPinned
                  ? t("room.unpin", { defaultValue: "Unpin" })
                  : t("room.pinSelf", { defaultValue: "Pin myself" })
              }
            >
              {iAmPinned ? <PinOff size={16} /> : <Pin size={16} />}
              <span className="hidden md:inline">
                {iAmPinned
                  ? t("room.unpin", { defaultValue: "Unpin" })
                  : t("room.pinSelf", { defaultValue: "Pin myself" })}
              </span>
            </button>
            <button
              type="button"
              onClick={muteAll}
              data-testid="btn-mute-all"
              disabled={busy !== null}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-primary-700 text-slate-100 hover:bg-primary-600 disabled:opacity-50"
              title={t("room.muteAllTitle")}
              aria-label={t("room.muteAll")}
            >
              <MicOff size={16} />
              <span className="hidden md:inline">{t("room.muteAll")}</span>
            </button>
            {/* Room-wide layout picker. Always visible for hosts — the
                choice is mirrored to every live viewer + the recording +
                the livestream via room metadata. While playback is
                active the server force-locks the egress to
                single-speaker; the host's choice still persists and is
                applied to live viewers once playback ends. */}
            {meetingId && (
              <RecordingLayoutPicker
                value={recordingLayout}
                onChange={async (v) => {
                  // Optimistic update so the picker feels instant; if the
                  // API fails we roll back to the previous value.
                  const prev = recordingLayout;
                  setRecordingLayout(v);
                  try {
                    await api.setRoomLayout(meetingId, v);
                  } catch {
                    setRecordingLayout(prev);
                  }
                }}
                disabled={busy !== null}
              />
            )}
            {/* Owner-only Playlist button — toggles the right-hand
                side panel. The panel itself contains the
                play/pause/stop controls and the click-to-play list,
                so this is just an opener (no inline play action). The
                small pulsing accent dot indicates a video is
                currently playing so the host knows playback is live
                even with the panel closed. */}
            {isOwner && playbackEnabled && (
              <button
                type="button"
                onClick={() => setPlaybackPanelOpen((v) => !v)}
                data-testid="btn-playlist"
                aria-pressed={playbackPanelOpen ? "true" : "false"}
                aria-label={t("room.playlist", { defaultValue: "Playlist" })}
                title={t("room.playlistTitle", {
                  defaultValue:
                    "Open the video playlist (upload, reorder, play). All mics and cameras auto-mute while a video is playing.",
                })}
                className={[
                  "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium relative",
                  playbackPanelOpen
                    ? "bg-primary-500 text-white"
                    : "bg-primary-700 text-slate-100 hover:bg-primary-600",
                ].join(" ")}
              >
                <Film size={16} />
                <span className="hidden md:inline">
                  {t("room.playlist", { defaultValue: "Playlist" })}
                </span>
                {playbackActive && (
                  <span
                    aria-hidden
                    className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-accent-400 ring-2 ring-primary-900 animate-pulse"
                  />
                )}
              </button>
            )}
            {livestreamEnabled && (
              <button
                type="button"
                onClick={toggleLivestream}
                data-testid="btn-stream"
                disabled={busy !== null}
                aria-label={
                  livestreamActive
                    ? t("room.stopStream", { defaultValue: "Stop streaming" })
                    : t("room.startStream", { defaultValue: "Start streaming" })
                }
                title={
                  livestreamActive
                    ? t("room.stopStreamTitle", { defaultValue: "Stop livestreaming to X.com" })
                    : t("room.startStreamTitle", { defaultValue: "Start livestreaming to X.com" })
                }
                className={[
                  "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
                  livestreamActive
                    ? "bg-red-600 text-white hover:bg-red-700"
                    : "bg-primary-700 text-slate-100 hover:bg-primary-600",
                  "disabled:opacity-50",
                ].join(" ")}
              >
                <Radio size={16} />
                <span className="hidden md:inline">
                  {busy === "stream-start"
                    ? t("room.starting")
                    : busy === "stream-stop"
                    ? t("room.stopping")
                    : livestreamActive
                    ? t("room.stopStream", { defaultValue: "Stop streaming" })
                    : t("room.startStream", { defaultValue: "Start streaming" })}
                </span>
              </button>
            )}
            {/* Recording stays available during video playback — the
                composite egress picks up the playback participant the
                same way it picks up any other speaker. Host server was
                rescaled past the original 2-vCPU mutex budget. */}
            <button
              type="button"
              onClick={toggleRecording}
              data-testid="btn-record"
              disabled={busy !== null}
              aria-label={recordingActive ? t("room.stopRecording") : t("room.record")}
              className={[
                "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
                recordingActive
                  ? "bg-red-600 text-white hover:bg-red-700"
                  : "bg-accent-500 text-white hover:bg-accent-600",
                "disabled:opacity-50",
              ].join(" ")}
            >
              {recordingActive ? <CircleStopIcon size={16} /> : <Radio size={16} />}
              <span className="hidden md:inline">
                {busy === "rec-start"
                  ? t("room.starting")
                  : busy === "rec-stop"
                  ? t("room.stopping")
                  : recordingActive
                  ? t("room.stopRecording")
                  : t("room.record")}
              </span>
            </button>
            <button
              type="button"
              onClick={endMeeting}
              data-testid="btn-end-meeting"
              disabled={busy !== null}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-red-700 text-white hover:bg-red-800 disabled:opacity-50"
              title={t("room.endMeetingTitle")}
              aria-label={t("room.endMeeting")}
            >
              <span className="md:hidden">✕</span>
              <span className="hidden md:inline">{t("room.endMeeting")}</span>
            </button>
          </>
        )}

        <OutputVolumeControl />
        <HandRaiseButton />
        <ReactionsButton />
        <button
          type="button"
          onClick={() => setPollsOpen((v) => !v)}
          disabled={!meetingId}
          data-testid="btn-polls"
          aria-pressed={pollsOpen ? "true" : "false"}
          aria-label={t("polls.toolbar", { defaultValue: "Polls & Q&A" })}
          title={t("polls.toolbarTitle", { defaultValue: "Open polls and Q&A" })}
          className={[
            "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
            pollsOpen
              ? "bg-primary-500 text-white"
              : "bg-primary-700 text-slate-100 hover:bg-primary-600",
            "disabled:opacity-50",
          ].join(" ")}
        >
          <Vote size={16} />
        </button>
        <button
          type="button"
          onClick={() => setNotesOpen((v) => !v)}
          data-testid="btn-notes"
          aria-pressed={notesOpen ? "true" : "false"}
          aria-label={t("notes.toolbar", { defaultValue: "Notes & whiteboard" })}
          title={t("notes.toolbarTitle", { defaultValue: "Open shared notes and whiteboard" })}
          className={[
            "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
            notesOpen
              ? "bg-primary-500 text-white"
              : "bg-primary-700 text-slate-100 hover:bg-primary-600",
          ].join(" ")}
        >
          <FileText size={16} />
        </button>
        <PipButton />
        <DeviceSwitcher />

        <button
          type="button"
          onClick={copyMeetingLink}
          data-testid="btn-copy-link"
          title={t("room.copyLinkTitle", { defaultValue: "Copy meeting link to clipboard" })}
          aria-label={t("room.copyLink", { defaultValue: "Link" })}
          className={[
            "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
            copied
              ? "bg-accent-500 text-white"
              : "bg-primary-700 text-slate-100 hover:bg-primary-600",
          ].join(" ")}
        >
          {copied ? <Check size={16} /> : <Link2 size={16} />}
          <span className="hidden md:inline">
            {copied
              ? t("room.linkCopied", { defaultValue: "Copied!" })
              : t("room.copyLink", { defaultValue: "Link" })}
          </span>
        </button>

        <button
          type="button"
          onClick={() => setSettingsOpen((v) => !v)}
          data-testid="btn-settings"
          aria-pressed={settingsOpen ? "true" : "false"}
          aria-label={t("room.settingsLabel")}
          title={t("room.settings")}
          className={[
            "inline-flex items-center justify-center px-2 py-1.5 rounded-lg text-sm font-medium",
            settingsOpen
              ? "bg-primary-500 text-white"
              : "bg-primary-700 text-slate-100 hover:bg-primary-600",
          ].join(" ")}
        >
          <SettingsIcon size={16} />
        </button>

        <button
          type="button"
          onClick={() => {
            setParticipantsOpen((v) => !v);
            // On mobile only one panel can be open at a time, otherwise the
            // stage gets squeezed to nothing — close chat when opening people.
            if (!participantsOpen && window.innerWidth < 640) setChatOpen(false);
          }}
          data-testid="btn-participants"
          aria-pressed={participantsOpen ? "true" : "false"}
          aria-label={t("room.people")}
          className={[
            "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
            participantsOpen
              ? "bg-primary-500 text-white"
              : "bg-primary-700 text-slate-100 hover:bg-primary-600",
          ].join(" ")}
        >
          <Users size={16} />
          <span className="hidden md:inline">{t("room.people")}</span>
        </button>

        <button
          type="button"
          onClick={() => {
            setChatOpen((v) => !v);
            if (!chatOpen && window.innerWidth < 640) setParticipantsOpen(false);
          }}
          data-testid="btn-chat"
          aria-pressed={chatOpen ? "true" : "false"}
          aria-label={t("room.chat")}
          className={[
            "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
            chatOpen
              ? "bg-primary-500 text-white"
              : "bg-primary-700 text-slate-100 hover:bg-primary-600",
          ].join(" ")}
        >
          <MessageSquare size={16} />
          <span className="hidden md:inline">{t("room.chat")}</span>
        </button>
      </header>

      {err && (
        <div
          role="alert"
          data-testid="room-error"
          className="px-4 py-2 bg-red-900/40 text-red-200 text-sm border-b border-red-900 flex-shrink-0"
        >
          {err}
        </div>
      )}

      {/* MIDDLE: stage + side panels (panels are inline so they never overlap
          the stage; stage flexes down to make room). */}
      <div className="flex-1 min-h-0 flex">
        <div
          className={[
            // `min-h-0` is the critical bit: flex children default to
            // `min-height: auto` (their content's intrinsic minimum),
            // which lets the grid push past the parent and clip at the
            // bottom when the viewport gets short. `overflow-hidden` is
            // the safety net so any residual overflow doesn't bleed into
            // the bottom control bar.
            "meet-stage flex-1 min-w-0 min-h-0 overflow-hidden relative",
            display.showParticipantNames ? "" : "no-participant-names",
            display.showConnectionQuality ? "" : "no-conn-quality",
            display.highlightSpeaker ? "" : "no-speaker-highlight",
            appearance.compactMode ? "compact" : "",
            accessibility.reducedMotion ? "reduced-motion" : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          <PresenterSpotlight />
          <RoomAudioRenderer />
          {display.showMeetingClock && <MeetingClock />}
          {accessibility.liveCaptions && (
            <CaptionsOverlay fontSize={accessibility.captionsFontSize} />
          )}
          <PushToTalkIndicator />
          <FloatingReactions room={room} />
        </div>
        <InMeetingSettings
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
          onConfigureLivestream={
            isOwner && livestreamMeeting
              ? () => {
                  setLivestreamSettingsOpen(true);
                  setSettingsOpen(false);
                }
              : undefined
          }
          onViewRecordings={
            isOwner
              ? () => {
                  setRecordingsModalOpen(true);
                  setSettingsOpen(false);
                }
              : undefined
          }
          meeting={isOwner ? livestreamMeeting : null}
          onMeetingUpdated={
            isOwner ? (updated) => setLivestreamMeeting(updated) : undefined
          }
        />
        {isOwner && meetingId && (
          <MeetingRecordingsModal
            open={recordingsModalOpen}
            onClose={() => setRecordingsModalOpen(false)}
            meetingId={meetingId}
          />
        )}
        <ParticipantsPanel
          open={participantsOpen}
          onClose={() => setParticipantsOpen(false)}
          meetingId={meetingId}
          isOwner={isOwner}
        />
        {isOwner && meetingId && (
          <PendingJoinersPanel
            meetingId={meetingId}
            open={pendingOpen}
            onClose={() => setPendingOpen(false)}
            onCountChange={setPendingCount}
          />
        )}
        <PollsQnaPanel
          open={pollsOpen}
          onClose={() => setPollsOpen(false)}
          meetingId={meetingId}
          isModerator={isOwner}
          initialTab={pollsInitialTab}
          onConsumeInitialTab={() => setPollsInitialTab(null)}
        />
        <NotesWhiteboardPanel
          open={notesOpen}
          onClose={() => setNotesOpen(false)}
          initialTab={notesInitialTab}
          onConsumeInitialTab={() => setNotesInitialTab(null)}
        />
        <ChatPanel
          open={chatOpen}
          onClose={() => setChatOpen(false)}
          isOwner={isOwner}
          meetingId={meetingId}
        />
        {isOwner && livestreamMeeting && (
          <VideoPlaybackPanel
            meeting={livestreamMeeting}
            open={playbackPanelOpen}
            onClose={() => setPlaybackPanelOpen(false)}
            onMeetingUpdated={(updated) => {
              setLivestreamMeeting(updated);
              setPlaybackEnabled(!!updated.playback_enabled);
            }}
          />
        )}
      </div>

      {/* BOTTOM CONTROL BAR — always reachable, full width below panels.
          Pads the iOS home-indicator safe area so Mute/Leave aren't under the
          system bar on iPhones with no home button. */}
      <div className="bg-primary-900/90 border-t border-primary-700 flex-shrink-0 pb-[env(safe-area-inset-bottom)]">
        <ControlBar variation="verbose" controls={{ chat: false, leave: true }} />
      </div>

      <ShortcutOverlay open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />

      {/* INVITE */}
      {isOwner && meetingId && (
        <InviteModal
          meetingId={meetingId}
          meetingTitle={meetingTitle ?? undefined}
          open={inviteOpen}
          onClose={() => setInviteOpen(false)}
        />
      )}

      {/* LIVESTREAM CONFIG */}
      {isOwner && livestreamMeeting && (
        <LivestreamSettingsModal
          meeting={livestreamMeeting}
          open={livestreamSettingsOpen}
          onClose={() => setLivestreamSettingsOpen(false)}
          onSaved={(updated) => {
            setLivestreamMeeting(updated);
            const anyEnabled = LIVESTREAM_DESTINATIONS.some(
              (d) => !!(updated as unknown as Record<string, unknown>)[d.fields.enabled],
            );
            setLivestreamEnabled(anyEnabled);
          }}
        />
      )}

      {/* Co-host role-change notice — same banner for both promotion AND
          demotion. The token in hand still reflects the OLD role until
          the user rejoins (room_admin grant either present or absent),
          so a single rejoin shortcut covers both cases. Dismissing
          keeps the moderator UI in its current (stale) state until the
          user does refresh; the participants-panel crown / role tag
          updates on the next listCohosts poll regardless. */}
      {cohostNotice && (
        <div
          data-testid={`cohost-${cohostNotice}-banner`}
          className={[
            "fixed top-2 right-2 z-50 max-w-sm px-3 py-2 rounded-lg text-white text-sm shadow-lg flex items-start gap-2",
            cohostNotice === "promoted" ? "bg-accent-500/95" : "bg-amber-600/95",
          ].join(" ")}
          role="status"
        >
          <div className="flex-1">
            <div className="font-semibold">
              {cohostNotice === "promoted"
                ? t("room.cohostPromotedTitle", { defaultValue: "You're now a co-host" })
                : t("room.cohostDemotedTitle", {
                    defaultValue: "You're no longer a co-host",
                  })}
            </div>
            <div className="text-xs opacity-90 mt-0.5">
              {cohostNotice === "promoted"
                ? t("room.cohostPromotedBody", {
                    defaultValue:
                      "Rejoin the meeting to activate moderator tools (mute all, record, kick, etc.).",
                  })
                : t("room.cohostDemotedBody", {
                    defaultValue:
                      "Rejoin the meeting so the moderator tools (mute all, record, kick, etc.) are removed from your toolbar.",
                  })}
            </div>
            <div className="mt-2 flex items-center gap-2">
              <button
                type="button"
                onClick={() => window.location.reload()}
                data-testid="cohost-rejoin"
                className="px-2.5 py-1 rounded bg-white/15 hover:bg-white/25 text-white text-xs font-medium"
              >
                {t("room.cohostRejoin", { defaultValue: "Rejoin now" })}
              </button>
              <button
                type="button"
                onClick={() => setCohostNotice(null)}
                data-testid="cohost-dismiss"
                className="px-2.5 py-1 rounded bg-transparent hover:bg-white/10 text-white/80 text-xs"
              >
                {t("common.dismiss", { defaultValue: "Dismiss" })}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* "Now playing" toolbar pill removed — the Playlist side panel
          highlights the currently-playing row instead, which the user
          asked for. The pulsing dot on the toolbar Playlist button
          (above) is the at-a-glance "video is playing" cue. */}
    </div>
  );
}

export default function Room() {
  const { roomName = "" } = useParams();
  const navigate = useNavigate();
  const pending = loadPendingToken();
  const meta = loadRoomMeta();
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackInfo, setFeedbackInfo] = useState<{ identity: string | null; name: string | null } | null>(null);

  // No token in sessionStorage → user reached /r/ directly. Bounce them back
  // to the lobby. Calling `navigate()` during render is a React anti-pattern
  // (warns in dev, can double-fire); defer to an effect instead.
  useEffect(() => {
    if (!pending) navigate(`/${roomName}`, { replace: true });
  }, [pending, roomName, navigate]);

  if (!pending) return null;

  const cfg = roomOptions(pending);
  const ownerMeetingId = sessionStorage.getItem(`owner:${roomName}`);
  const isOwner = !!ownerMeetingId;
  // Every participant (owner, co-host or anon) needs the meeting_id to read
  // and write polls / Q&A / shared notes. Owners and co-hosts already have
  // it from sessionStorage; anon joiners pick it up from the Lobby's
  // publicRoomInfo cache.
  const meetingId = ownerMeetingId ?? meta.meeting_id ?? null;
  const feedbackFlagKey = `feedback-shown:${roomName}`;

  return (
    <>
      <LiveKitRoom
        serverUrl={cfg.serverUrl}
        token={cfg.token}
        connect
        audio
        video
        options={cfg.roomOptions}
        connectOptions={cfg.connectOptions}
        onDisconnected={() => {
          clearPendingToken();
          clearRoomMeta();
          // Capture identity/name BEFORE LiveKit unmounts the room context.
          // Owners aren't asked for feedback (they ran the meeting). Anyone
          // who's already submitted in this session is also skipped.
          const alreadyShown = sessionStorage.getItem(feedbackFlagKey) === "1";
          if (!isOwner && !alreadyShown) {
            sessionStorage.setItem(feedbackFlagKey, "1");
            setFeedbackOpen(true);
            return;
          }
          navigate("/");
        }}
      >
        <InnerRoom
          meetingId={meetingId}
          isOwner={isOwner}
          meetingTitle={meta.display_title ?? null}
          brandingUrl={meta.branding_url ?? null}
          roomName={roomName}
          onCaptureFeedbackInfo={setFeedbackInfo}
        />
      </LiveKitRoom>
      {feedbackOpen && (
        <PostMeetingFeedback
          roomName={roomName}
          participantIdentity={feedbackInfo?.identity ?? null}
          participantName={feedbackInfo?.name ?? null}
          onClose={() => {
            setFeedbackOpen(false);
            navigate("/");
          }}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Recording-layout picker: three icon buttons (one per layout) instead of a
// text dropdown. Native <select> can't render images per option, so we use a
// radio-group of buttons. The active button is highlighted; tooltips on
// each button still show the localized layout name.
// ---------------------------------------------------------------------------
type RecordingLayoutKey = "speaker" | "grid" | "single-speaker";

function IconSingleSpeaker() {
  return (
    <svg viewBox="0 0 32 24" width={26} height={20} aria-hidden="true">
      <rect x="2" y="2" width="28" height="20" rx="2" fill="currentColor" />
    </svg>
  );
}

function IconSpeaker() {
  return (
    <svg viewBox="0 0 32 24" width={26} height={20} aria-hidden="true">
      <rect x="2" y="2" width="28" height="14" rx="1.5" fill="currentColor" />
      <rect x="2" y="18" width="6" height="4" rx="0.5" fill="currentColor" opacity="0.6" />
      <rect x="13" y="18" width="6" height="4" rx="0.5" fill="currentColor" opacity="0.6" />
      <rect x="24" y="18" width="6" height="4" rx="0.5" fill="currentColor" opacity="0.6" />
    </svg>
  );
}

function IconGrid() {
  return (
    <svg viewBox="0 0 32 24" width={26} height={20} aria-hidden="true">
      <rect x="2" y="2" width="13" height="9" rx="1" fill="currentColor" />
      <rect x="17" y="2" width="13" height="9" rx="1" fill="currentColor" />
      <rect x="2" y="13" width="13" height="9" rx="1" fill="currentColor" />
      <rect x="17" y="13" width="13" height="9" rx="1" fill="currentColor" />
    </svg>
  );
}

function RecordingLayoutPicker({
  value,
  onChange,
  disabled,
}: {
  value: RecordingLayoutKey;
  onChange: (v: RecordingLayoutKey) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();
  const options: Array<{ key: RecordingLayoutKey; icon: React.ReactNode; label: string }> = [
    { key: "single-speaker", icon: <IconSingleSpeaker />, label: t("room.recordLayoutSingle") },
    { key: "speaker", icon: <IconSpeaker />, label: t("room.recordLayoutSpeaker") },
    { key: "grid", icon: <IconGrid />, label: t("room.recordLayoutGrid") },
  ];
  return (
    <div
      role="radiogroup"
      aria-label={t("room.recordLayout")}
      title={t("room.recordLayoutTitle")}
      data-testid="rec-layout-picker"
      className={[
        "inline-flex items-center gap-0.5 p-0.5 rounded-lg bg-primary-700",
        disabled ? "opacity-50" : "",
      ].join(" ")}
    >
      {options.map(({ key, icon, label }) => {
        const active = value === key;
        return (
          <button
            key={key}
            type="button"
            role="radio"
            aria-checked={active ? "true" : "false"}
            aria-label={label}
            title={label}
            disabled={disabled}
            data-testid={`btn-rec-layout-${key}`}
            onClick={() => onChange(key)}
            className={[
              "inline-flex items-center justify-center px-2 py-1 rounded-md transition-colors",
              active
                ? "bg-primary-500 text-white"
                : "text-slate-300 hover:text-slate-100 hover:bg-primary-600",
              disabled ? "cursor-not-allowed" : "cursor-pointer",
            ].join(" ")}
          >
            {icon}
          </button>
        );
      })}
    </div>
  );
}

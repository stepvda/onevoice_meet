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
  Crown,
  Link2,
  Mail,
  MessageSquare,
  MicOff,
  Radio,
  Play,
  Film,
  FileText,
  Settings as SettingsIcon,
  Square,
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
import BackgroundPicker from "../components/BackgroundPicker";
import RecordingIndicator from "../components/RecordingIndicator";
import ChatPanel from "../components/ChatPanel";
import ParticipantsPanel from "../components/ParticipantsPanel";
import InMeetingSettings from "../components/InMeetingSettings";
import InviteModal from "../components/InviteModal";
import LivestreamSettingsModal from "../components/LivestreamSettingsModal";
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
            setPlaybackCurrentName(obj.filename ?? null);
            if (obj.type === "playback-start") {
              void room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
              void room.localParticipant.setCameraEnabled(false).catch(() => undefined);
            }
          } else if (obj?.type === "playback-end") {
            setPlaybackActive(false);
            setPlaybackCurrentName(null);
          }
        } catch {
          /* ignore malformed playback packet */
        }
      } else if (topic === "meet-cohost") {
        // Owner just promoted this user to co-host. Their current
        // LiveKit token still lacks the room_admin grant (only the
        // Lobby flow mints that on rejoin), so we surface a banner
        // asking them to refresh to activate the moderator tools.
        try {
          const obj = JSON.parse(decoder.decode(payload)) as { type?: string };
          if (obj?.type === "promoted") setCohostPromoted(true);
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
  const [recordingLayout, setRecordingLayout] = useState<"speaker" | "grid" | "single-speaker">("speaker");
  // Livestream-to-X.com. `livestreamEnabled` mirrors the meeting flag
  // (toolbar button only renders when true); `livestreamActive` flips when
  // the egress is running. We poll on mount/owner-only because the meeting
  // metadata doesn't (yet) include livestream state.
  const [livestreamEnabled, setLivestreamEnabled] = useState(false);
  const [livestreamActive, setLivestreamActive] = useState(false);
  const [livestreamSettingsOpen, setLivestreamSettingsOpen] = useState(false);
  const [livestreamMeeting, setLivestreamMeeting] = useState<MeetingOut | null>(null);
  // Video playback. `playbackEnabled` mirrors the meeting toggle; we also
  // need the playlist length to decide whether to surface the Play button
  // (Play is hidden until at least one item exists). `playbackActive`
  // reflects whether a LiveKit ingress is currently publishing.
  const [playbackEnabled, setPlaybackEnabled] = useState(false);
  const [playbackActive, setPlaybackActive] = useState(false);
  const [playbackItemCount, setPlaybackItemCount] = useState(0);
  const [playbackPanelOpen, setPlaybackPanelOpen] = useState(false);
  const [playbackCurrentName, setPlaybackCurrentName] = useState<string | null>(null);
  // True when this user was promoted to co-host mid-session — flips the
  // "Refresh to activate moderator tools" banner on.
  const [cohostPromoted, setCohostPromoted] = useState(false);
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
        try {
          const items = await api.listPlaybackItems(meetingId);
          if (!cancelled) setPlaybackItemCount(items.length);
        } catch {
          /* not fatal */
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

  async function togglePlayback() {
    if (!meetingId) return;
    if (playbackActive) {
      await withBusy("playback-stop", async () => {
        await api.stopPlayback(meetingId);
        setPlaybackActive(false);
        setPlaybackCurrentName(null);
      });
    } else {
      await withBusy("playback-start", async () => {
        await api.startPlayback(meetingId);
        // `playback-start` data-channel signal flips setPlaybackActive
        // when it arrives — set it optimistically too so the button
        // doesn't flicker between the Start request and the broadcast.
        setPlaybackActive(true);
      });
    }
  }

  // Mirror room.metadata.recording_active locally.
  useEffect(() => {
    const read = () => {
      try {
        const md = JSON.parse(room.metadata || "{}");
        setRecordingActive(!!md.recording_active);
      } catch {
        setRecordingActive(false);
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

  async function takeCenterStage() {
    if (!meetingId) return;
    const me = room.localParticipant.identity;
    await withBusy("center-stage", () => api.setPresenter(meetingId, me));
  }

  async function backToGrid() {
    if (!meetingId) return;
    await withBusy("clear-presenter", () => api.setPresenter(meetingId, null));
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
              title={meetingTitle ?? ""}
            >
              {meetingTitle || "meet.witysk.org"}
            </div>
            <div className="text-xs text-slate-400">meet.witysk.org</div>
          </div>
          <AudioWaveform width={120} height={28} className="flex-shrink-0" />
          <RecordingIndicator />
        </div>

        <div className="flex-1" />

        <BackgroundPicker />

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
            <button
              type="button"
              onClick={takeCenterStage}
              data-testid="btn-center-stage"
              disabled={busy !== null}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-primary-700 text-slate-100 hover:bg-primary-600 disabled:opacity-50"
              title={t("room.takeStageTitle")}
              aria-label={t("room.takeStage")}
            >
              <Crown size={16} />
              <span className="hidden md:inline">{t("room.takeStage")}</span>
            </button>
            <button
              type="button"
              onClick={backToGrid}
              data-testid="btn-grid"
              disabled={busy !== null}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-primary-700 text-slate-100 hover:bg-primary-600 disabled:opacity-50"
              title={t("room.gridTitle")}
              aria-label={t("room.grid")}
            >
              <Square size={16} />
              <span className="hidden md:inline">{t("room.grid")}</span>
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
            {!recordingActive && !playbackActive && (
              <select
                value={recordingLayout}
                onChange={(e) =>
                  setRecordingLayout(e.target.value as "speaker" | "grid" | "single-speaker")
                }
                disabled={busy !== null}
                data-testid="select-rec-layout"
                title={t("room.recordLayoutTitle")}
                aria-label={t("room.recordLayout")}
                className="px-2 py-1.5 rounded-lg text-sm font-medium bg-primary-700 text-slate-100 hover:bg-primary-600 disabled:opacity-50 border-none"
              >
                <option value="speaker">{t("room.recordLayoutSpeaker")}</option>
                <option value="grid">{t("room.recordLayoutGrid")}</option>
                <option value="single-speaker">{t("room.recordLayoutSingle")}</option>
              </select>
            )}
            {playbackEnabled && playbackItemCount > 0 && (
              <button
                type="button"
                onClick={togglePlayback}
                data-testid="btn-playback"
                disabled={busy !== null}
                aria-label={
                  playbackActive
                    ? t("room.stopPlayback", { defaultValue: "Stop video playback" })
                    : t("room.startPlayback", { defaultValue: "Play video" })
                }
                title={
                  playbackActive
                    ? t("room.stopPlaybackTitle", {
                        defaultValue: "Stop the current video — all participants' mics/cams stay off",
                      })
                    : t("room.startPlaybackTitle", {
                        defaultValue:
                          "Play the playlist to everyone. Mics and cameras will be muted automatically.",
                      })
                }
                className={[
                  "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
                  playbackActive
                    ? "bg-red-600 text-white hover:bg-red-700"
                    : "bg-primary-700 text-slate-100 hover:bg-primary-600",
                  "disabled:opacity-50",
                ].join(" ")}
              >
                {playbackActive ? <CircleStopIcon size={16} /> : <Play size={16} />}
                <span className="hidden md:inline">
                  {busy === "playback-start"
                    ? t("room.starting")
                    : busy === "playback-stop"
                    ? t("room.stopping")
                    : playbackActive
                    ? t("room.stopPlayback", { defaultValue: "Stop video playback" })
                    : t("room.startPlayback", { defaultValue: "Play video" })}
                </span>
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
            {/* Recording is mutually exclusive with video playback — see
                the 2-vCPU CPU budget in playback_mgr.py. Hide the button
                rather than fail-with-toast so the host can't accidentally
                click it. Streaming stays available during playback. */}
            {!playbackActive && (
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
            )}
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
          onConfigurePlayback={
            isOwner && livestreamMeeting
              ? () => {
                  setPlaybackPanelOpen(true);
                  setSettingsOpen(false);
                }
              : undefined
          }
        />
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

      {/* VIDEO PLAYBACK PANEL */}
      {isOwner && livestreamMeeting && (
        <VideoPlaybackPanel
          meeting={livestreamMeeting}
          open={playbackPanelOpen}
          onClose={() => setPlaybackPanelOpen(false)}
          onMeetingUpdated={async (updated) => {
            setLivestreamMeeting(updated);
            setPlaybackEnabled(!!updated.playback_enabled);
            // Item count may have changed via uploads/deletes; re-poll.
            try {
              const items = await api.listPlaybackItems(updated.id);
              setPlaybackItemCount(items.length);
            } catch {
              /* not fatal */
            }
          }}
        />
      )}

      {/* Co-host promotion notice — appears only on the promoted user's
          screen. The current LiveKit token lacks the room_admin grant
          (Lobby mints the moderator token on rejoin), so we surface a
          banner with a single-click rejoin shortcut. Dismissing keeps
          the panel out of the way; the crown badge in the participants
          panel still indicates their new role. */}
      {cohostPromoted && (
        <div
          data-testid="cohost-promoted-banner"
          className="fixed top-2 right-2 z-50 max-w-sm px-3 py-2 rounded-lg bg-accent-500/95 text-white text-sm shadow-lg flex items-start gap-2"
          role="status"
        >
          <div className="flex-1">
            <div className="font-semibold">
              {t("room.cohostPromotedTitle", { defaultValue: "You're now a co-host" })}
            </div>
            <div className="text-xs opacity-90 mt-0.5">
              {t("room.cohostPromotedBody", {
                defaultValue: "Rejoin the meeting to activate moderator tools (mute all, record, kick, etc.).",
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
                onClick={() => setCohostPromoted(false)}
                data-testid="cohost-dismiss"
                className="px-2.5 py-1 rounded bg-transparent hover:bg-white/10 text-white/80 text-xs"
              >
                {t("common.dismiss", { defaultValue: "Dismiss" })}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* "Now playing" banner — visible to everyone while a video is on the
          stage. Sits in the top toolbar's negative-margin area; uses a
          left ribbon so it doesn't fight with the existing recording dot. */}
      {playbackActive && (
        <div
          data-testid="playback-banner"
          className="fixed top-2 left-1/2 -translate-x-1/2 z-40 px-3 py-1.5 rounded-full bg-accent-500/90 text-white text-xs font-medium shadow-lg inline-flex items-center gap-2 pointer-events-none"
        >
          <Film size={14} />
          {playbackCurrentName
            ? t("room.nowPlayingNamed", {
                defaultValue: "Now playing: {{name}}",
                name: playbackCurrentName,
              })
            : t("room.nowPlaying", { defaultValue: "Now playing" })}
        </div>
      )}
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

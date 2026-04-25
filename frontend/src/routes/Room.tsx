import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ControlBar,
  LiveKitRoom,
  RoomAudioRenderer,
  useRoomContext,
} from "@livekit/components-react";
import { RoomEvent } from "livekit-client";
import {
  CircleStopIcon,
  Crown,
  Mail,
  MessageSquare,
  MicOff,
  Radio,
  Settings as SettingsIcon,
  Square,
  Users,
} from "lucide-react";
import { clearPendingToken, clearRoomMeta, loadPendingToken, loadRoomMeta } from "./Lobby";
import { roomOptions } from "../lib/livekit";
import { api } from "../lib/api";
import PresenterSpotlight from "../components/PresenterSpotlight";
import BackgroundPicker from "../components/BackgroundPicker";
import RecordingIndicator from "../components/RecordingIndicator";
import ChatPanel from "../components/ChatPanel";
import ParticipantsPanel from "../components/ParticipantsPanel";
import InMeetingSettings from "../components/InMeetingSettings";
import InviteModal from "../components/InviteModal";

interface InnerProps {
  meetingId: string | null;
  isOwner: boolean;
  meetingTitle: string | null;
  brandingUrl: string | null;
}

function InnerRoom({ meetingId, isOwner, meetingTitle, brandingUrl }: InnerProps) {
  const navigate = useNavigate();
  const room = useRoomContext();
  const [recordingActive, setRecordingActive] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [participantsOpen, setParticipantsOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const errClear = useRef<number | null>(null);

  function showErr(m: string) {
    setErr(m);
    if (errClear.current) window.clearTimeout(errClear.current);
    errClear.current = window.setTimeout(() => setErr(null), 4000);
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
      showErr((e as Error).message || "request failed");
    } finally {
      setBusy(null);
    }
  }

  async function toggleRecording() {
    if (!meetingId) return;
    if (recordingActive) {
      await withBusy("rec-stop", () => api.stopRecording(meetingId));
    } else {
      await withBusy("rec-start", () => api.startRecording(meetingId));
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
    if (!confirm("End the meeting for everyone? This will disconnect all participants.")) return;
    await withBusy("end-meeting", () => api.endMeeting(meetingId));
    await room.disconnect();
    navigate("/");
  }

  return (
    <div className="flex flex-col h-screen w-screen bg-witysk-page overflow-hidden">
      {/* TOP BAR */}
      <header
        data-testid="room-topbar"
        className="flex items-center gap-2 px-3 py-2 bg-primary-900/90 backdrop-blur border-b border-primary-700 flex-wrap flex-shrink-0"
      >
        <div className="flex items-center gap-3 mr-2 min-w-0 max-w-[40%]">
          {brandingUrl && (
            <img
              src={brandingUrl}
              alt=""
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
            title="Invite people by email"
          >
            <Mail size={16} />
            Invite
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
              title="Spotlight myself for everyone"
            >
              <Crown size={16} />
              Take stage
            </button>
            <button
              type="button"
              onClick={backToGrid}
              data-testid="btn-grid"
              disabled={busy !== null}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-primary-700 text-slate-100 hover:bg-primary-600 disabled:opacity-50"
              title="Switch back to grid view"
            >
              <Square size={16} />
              Grid
            </button>
            <button
              type="button"
              onClick={muteAll}
              data-testid="btn-mute-all"
              disabled={busy !== null}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-primary-700 text-slate-100 hover:bg-primary-600 disabled:opacity-50"
              title="Mute every other participant"
            >
              <MicOff size={16} />
              Mute all
            </button>
            <button
              type="button"
              onClick={toggleRecording}
              data-testid="btn-record"
              disabled={busy !== null}
              className={[
                "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
                recordingActive
                  ? "bg-red-600 text-white hover:bg-red-700"
                  : "bg-accent-500 text-white hover:bg-accent-600",
                "disabled:opacity-50",
              ].join(" ")}
            >
              {recordingActive ? <CircleStopIcon size={16} /> : <Radio size={16} />}
              {busy === "rec-start"
                ? "Starting…"
                : busy === "rec-stop"
                ? "Stopping…"
                : recordingActive
                ? "Stop recording"
                : "Record"}
            </button>
            <button
              type="button"
              onClick={endMeeting}
              data-testid="btn-end-meeting"
              disabled={busy !== null}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium bg-red-700 text-white hover:bg-red-800 disabled:opacity-50"
              title="End meeting and kick everyone"
            >
              End meeting
            </button>
          </>
        )}

        <button
          type="button"
          onClick={() => setSettingsOpen((v) => !v)}
          data-testid="btn-settings"
          aria-pressed={settingsOpen ? "true" : "false"}
          aria-label="In-meeting settings"
          title="Settings"
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
          onClick={() => setParticipantsOpen((v) => !v)}
          data-testid="btn-participants"
          aria-pressed={participantsOpen ? "true" : "false"}
          className={[
            "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
            participantsOpen
              ? "bg-primary-500 text-white"
              : "bg-primary-700 text-slate-100 hover:bg-primary-600",
          ].join(" ")}
        >
          <Users size={16} />
          People
        </button>

        <button
          type="button"
          onClick={() => setChatOpen((v) => !v)}
          data-testid="btn-chat"
          aria-pressed={chatOpen ? "true" : "false"}
          className={[
            "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
            chatOpen
              ? "bg-primary-500 text-white"
              : "bg-primary-700 text-slate-100 hover:bg-primary-600",
          ].join(" ")}
        >
          <MessageSquare size={16} />
          Chat
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
        <div className="flex-1 min-w-0 relative">
          <PresenterSpotlight />
          <RoomAudioRenderer />
        </div>
        <InMeetingSettings open={settingsOpen} onClose={() => setSettingsOpen(false)} />
        <ParticipantsPanel
          open={participantsOpen}
          onClose={() => setParticipantsOpen(false)}
          meetingId={meetingId}
          isOwner={isOwner}
        />
        <ChatPanel open={chatOpen} onClose={() => setChatOpen(false)} />
      </div>

      {/* BOTTOM CONTROL BAR — always reachable, full width below panels. */}
      <div className="bg-primary-900/90 border-t border-primary-700 flex-shrink-0">
        <ControlBar variation="verbose" controls={{ chat: false, leave: true }} />
      </div>

      {/* INVITE */}
      {isOwner && meetingId && (
        <InviteModal
          meetingId={meetingId}
          meetingTitle={meetingTitle ?? undefined}
          open={inviteOpen}
          onClose={() => setInviteOpen(false)}
        />
      )}
    </div>
  );
}

export default function Room() {
  const { roomName = "" } = useParams();
  const navigate = useNavigate();
  const pending = loadPendingToken();
  const meta = loadRoomMeta();

  if (!pending) {
    navigate(`/${roomName}`, { replace: true });
    return null;
  }

  const cfg = roomOptions(pending);
  const ownerMeetingId = sessionStorage.getItem(`owner:${roomName}`);
  const isOwner = !!ownerMeetingId;

  return (
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
        navigate("/");
      }}
    >
      <InnerRoom
        meetingId={ownerMeetingId}
        isOwner={isOwner}
        meetingTitle={meta.display_title ?? null}
        brandingUrl={meta.branding_url ?? null}
      />
    </LiveKitRoom>
  );
}

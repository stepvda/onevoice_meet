import {
  useLocalParticipant,
  useRemoteParticipants,
} from "@livekit/components-react";
import { Crown, Hand, Mic, MicOff, Users, Video, VideoOff, X } from "lucide-react";
import { Track } from "livekit-client";
import type { Participant } from "livekit-client";
import { useTranslation } from "react-i18next";
import { api } from "../lib/api";
import { useState } from "react";
import { useHandRaiseState } from "../lib/handRaise";

interface Props {
  open: boolean;
  onClose: () => void;
  meetingId: string | null;
  isOwner: boolean;
}

export default function ParticipantsPanel({ open, onClose, meetingId, isOwner }: Props) {
  const { t } = useTranslation();
  const { localParticipant } = useLocalParticipant();
  const remotes = useRemoteParticipants();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  if (!open) return null;

  async function withBusy(label: string, fn: () => Promise<unknown>) {
    setBusyId(label);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  function audioOn(p: Participant): boolean {
    const pub = p.getTrackPublication(Track.Source.Microphone);
    return !!pub && !pub.isMuted;
  }
  function videoOn(p: Participant): boolean {
    const pub = p.getTrackPublication(Track.Source.Camera);
    return !!pub && !pub.isMuted;
  }

  const all = [localParticipant, ...remotes].filter(Boolean) as Participant[];

  return (
    <aside
      data-testid="participants-panel"
      className={[
        // On mobile: overlay the stage so we don't squeeze it to zero width
        // (use `absolute` to avoid affecting flex layout). On sm+ go back to
        // an inline column that pushes the stage left, like before.
        "absolute inset-y-0 right-0 z-20 sm:static sm:z-auto",
        "h-full w-full sm:w-72 flex-shrink-0 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col",
      ].join(" ")}
      role="complementary"
      aria-label={t("participants.title")}
    >
      <header className="flex items-center justify-between px-4 py-3 border-b border-primary-700">
        <h2 className="text-sm font-semibold text-slate-100 flex items-center gap-2">
          <Users size={16} /> {t("participants.titleCount", { count: all.length })}
        </h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("participants.close")}
          data-testid="participants-close"
          className="p-1 rounded hover:bg-primary-700 text-slate-300"
        >
          <X size={18} />
        </button>
      </header>

      <ul
        className="flex-1 overflow-y-auto py-1"
        data-testid="participants-list"
      >
        {all.map((p) => (
          <ParticipantRow
            key={p.identity}
            p={p}
            isMe={p.identity === localParticipant?.identity}
            isOwner={isOwner}
            meetingId={meetingId}
            busyId={busyId}
            audioOn={audioOn(p)}
            videoOn={videoOn(p)}
            withBusy={withBusy}
          />
        ))}
      </ul>

      {err && (
        <div className="px-3 py-2 text-xs text-red-400 border-t border-primary-700">
          {err}
        </div>
      )}
    </aside>
  );
}

function ParticipantRow({
  p,
  isMe,
  isOwner,
  meetingId,
  busyId,
  audioOn,
  videoOn,
  withBusy,
}: {
  p: Participant;
  isMe: boolean;
  isOwner: boolean;
  meetingId: string | null;
  busyId: string | null;
  audioOn: boolean;
  videoOn: boolean;
  withBusy: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const { t } = useTranslation();
  const hand = useHandRaiseState(p);
  const name = p.name || p.identity || t("common.anonymous");
  return (
    <li
      data-testid={`participant-${p.identity}`}
      data-hand-raised={hand.raised ? "true" : "false"}
      className={[
        "px-3 py-2 flex items-center gap-3 hover:bg-primary-800/60",
        hand.raised ? "bg-amber-500/10" : "",
      ].join(" ")}
    >
      <div className="flex-shrink-0 h-7 w-7 rounded-full bg-primary-600 flex items-center justify-center text-xs font-semibold text-slate-50">
        {name.slice(0, 1).toUpperCase()}
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm text-slate-100 truncate flex items-center gap-1.5">
          <span className="truncate">{name}</span>
          {isMe && <span className="text-xs text-slate-400">{t("participants.you")}</span>}
          {p.identity?.startsWith("user-") && (
            <span title={t("participants.authenticatedOwner")} className="text-amber-400">
              <Crown size={12} />
            </span>
          )}
          {hand.raised && (
            <span
              title={t("hand.tileBadge")}
              data-testid={`participant-hand-${p.identity}`}
              className="text-amber-400"
            >
              <Hand size={12} />
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 text-xs text-slate-400 mt-0.5">
          {audioOn ? (
            <Mic size={12} className="text-accent-500" />
          ) : (
            <MicOff size={12} className="text-slate-500" />
          )}
          {videoOn ? (
            <Video size={12} className="text-accent-500" />
          ) : (
            <VideoOff size={12} className="text-slate-500" />
          )}
        </div>
      </div>
      {/* Owner-only per-participant controls (not on self). 44px touch
          targets so phone-tapping mute/kick is reliable. */}
      {isOwner && !isMe && meetingId && (
        <div className="flex items-center gap-1">
          {hand.raised && (
            <button
              type="button"
              title={t("hand.lowerOther")}
              aria-label={t("hand.lowerName", { name })}
              data-testid={`participant-lower-hand-${p.identity}`}
              disabled={busyId !== null}
              onClick={() => withBusy(p.identity, () => api.lowerHand(meetingId, p.identity))}
              className="min-w-11 min-h-11 inline-flex items-center justify-center rounded hover:bg-primary-700 text-amber-300 disabled:opacity-50"
            >
              <Hand size={18} />
            </button>
          )}
          <button
            type="button"
            title={t("participants.muteMic")}
            aria-label={t("participants.muteName", { name })}
            data-testid={`participant-mute-${p.identity}`}
            disabled={busyId !== null}
            onClick={() =>
              withBusy(p.identity, () =>
                api.mute(meetingId, { participant_identity: p.identity, mute: true })
              )
            }
            className="min-w-11 min-h-11 inline-flex items-center justify-center rounded hover:bg-primary-700 text-slate-300 disabled:opacity-50"
          >
            <MicOff size={18} />
          </button>
          <button
            type="button"
            title={t("participants.makePresenter")}
            aria-label={t("participants.makeNamePresenter", { name })}
            data-testid={`participant-present-${p.identity}`}
            disabled={busyId !== null}
            onClick={() =>
              withBusy(p.identity, () => api.setPresenter(meetingId, p.identity))
            }
            className="min-w-11 min-h-11 inline-flex items-center justify-center rounded hover:bg-primary-700 text-slate-300 disabled:opacity-50"
          >
            <Crown size={18} />
          </button>
          <button
            type="button"
            title={t("participants.removeFromMeeting")}
            aria-label={t("participants.removeName", { name })}
            data-testid={`participant-kick-${p.identity}`}
            disabled={busyId !== null}
            onClick={() => withBusy(p.identity, () => api.kick(meetingId, p.identity))}
            className="min-w-11 min-h-11 inline-flex items-center justify-center rounded hover:bg-red-700/40 text-red-300 disabled:opacity-50"
          >
            <X size={18} />
          </button>
        </div>
      )}
    </li>
  );
}

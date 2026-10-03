import { useEffect, useRef, useState } from "react";
import type { Room } from "livekit-client";
import { RoomEvent } from "livekit-client";
import { meetppApi, setMeetppRoomToken } from "../../lib/meetpp/api";
import { useMeetpp } from "../../lib/meetpp/store";
import type { Announcement, BoardState } from "../../lib/meetpp/types";
import MeetppBoard from "./MeetppBoard";

interface Props {
  room: string;
  token: string;
  liveRoom?: Room | null;
  scale?: "normal" | "720p";
}

/** Read-only board for the egress page and public view. Polls state with the
 * page's own room token and plays announcement clips so they are captured in
 * the recording mix. */
export default function MeetppBoardView({ room, token, liveRoom, scale }: Props) {
  const board = useMeetpp((s) => s.board);
  const announcement = useMeetpp((s) => s.announcement);
  const audioRef = useRef<HTMLAudioElement>(null);
  const sidRef = useRef<string | null>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    setMeetppRoomToken(token);
  }, [token]);

  useEffect(() => {
    let stopped = false;
    const load = async () => {
      try {
        const active = await meetppApi.roomActive(room);
        if (stopped) return;
        if (!active.active || !active.sid) {
          sidRef.current = null;
          setVisible(false);
          return;
        }
        sidRef.current = active.sid;
        const state = (await meetppApi.getState(active.sid)) as BoardState;
        if (stopped) return;
        useMeetpp.getState().setBoard(state);
        setVisible(true);
      } catch {
        /* ignore */
      }
    };
    void load();
    const timer = window.setInterval(load, 5000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [room]);

  // Real-time updates when a live Room is available.
  useEffect(() => {
    if (!liveRoom) return;
    const decoder = new TextDecoder();
    const onData = (payload: Uint8Array, _p: unknown, _k: unknown, topic?: string) => {
      if (topic !== "meet-ai") return;
      try {
        const msg = JSON.parse(decoder.decode(payload)) as Record<string, unknown>;
        if (msg.type === "state") useMeetpp.getState().applyState(msg as never);
        else if (msg.type === "captions") {
          for (const c of (msg.items as never[]) ?? []) useMeetpp.getState().addCaption(c);
        } else if (msg.type === "announce") useMeetpp.getState().setAnnouncement(msg as unknown as Announcement);
        else if (msg.type === "session" && (msg.state === "ended" || msg.state === "finalising")) setVisible(false);
      } catch {
        /* ignore */
      }
    };
    liveRoom.on(RoomEvent.DataReceived, onData);
    return () => {
      liveRoom.off(RoomEvent.DataReceived, onData);
    };
  }, [liveRoom]);

  // Play announcement clips (captured by the egress audio mix).
  useEffect(() => {
    if (!announcement) return;
    const ms = (announcement.duration_ms || 2500) + 4000;
    const timer = window.setTimeout(() => useMeetpp.getState().setAnnouncement(null), ms);
    if (announcement.audio_url && audioRef.current) {
      audioRef.current.src = announcement.audio_url;
      void audioRef.current.play().catch(() => undefined);
    }
    return () => window.clearTimeout(timer);
  }, [announcement]);

  if (!visible && !board) return <audio ref={audioRef} className="hidden" />;

  return (
    <div className="absolute inset-0 bg-slate-900 p-2" style={scale === "720p" ? { fontSize: "1.35rem" } : undefined}>
      <MeetppBoard readOnly className="h-full w-full" />
      <audio ref={audioRef} />
    </div>
  );
}

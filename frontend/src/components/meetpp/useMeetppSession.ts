import { useCallback, useEffect, useState } from "react";
import type { Room } from "livekit-client";
import { RoomEvent } from "livekit-client";
import { useMeetpp } from "../../lib/meetpp/store";
import { meetppApi, setMeetppRoomToken } from "../../lib/meetpp/api";
import type { Announcement, BoardState, Caption, Proposal } from "../../lib/meetpp/types";

const MEET_AI_TOPIC = "meet-ai";

interface Options {
  room: Room;
  roomName: string;
  meetingId: string | null;
  isOwner: boolean;
  roomToken: string | null;
}

/** Read the store lazily via getState() so callbacks stay stable and do not
 * re-run effects on every store mutation. */
const S = () => useMeetpp.getState();

export function useMeetppSession({ room, roomName, meetingId, isOwner, roomToken }: Options) {
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setMeetppRoomToken(roomToken);
  }, [roomToken]);

  const fetchState = useCallback(async (sid: string) => {
    try {
      const state = (await meetppApi.getState(sid)) as BoardState;
      // Never let an in-flight (stale) snapshot overwrite a newer board.
      const current = S().board?.session.version ?? -1;
      if (state.session && state.session.version < current) return;
      S().setBoard(state);
      S().setActive(true);
      try {
        const tr = await meetppApi.getTranscript(sid, 0);
        S().setTranscript(tr.segments);
      } catch {
        /* transcript may be empty */
      }
    } catch {
      /* session may have ended */
    }
  }, []);

  // Discover an active session on mount / when the meeting id is known.
  useEffect(() => {
    if (!meetingId) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await meetppApi.roomActive(roomName);
        if (!cancelled && res.active && res.sid) {
          await fetchState(res.sid);
        }
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [meetingId, roomName, fetchState]);

  // Listen to the server-published meet-ai data channel.
  useEffect(() => {
    const decoder = new TextDecoder();
    const onData = (payload: Uint8Array, _p: unknown, _k: unknown, topic?: string) => {
      if (topic !== MEET_AI_TOPIC) return;
      let msg: Record<string, unknown>;
      try {
        msg = JSON.parse(decoder.decode(payload));
      } catch {
        return;
      }
      const type = msg.type as string;
      if (type === "session") {
        const state = msg.state as string;
        if (state === "started" || state === "resumed") {
          S().clearLiveTranscript();
          window.setTimeout(() => fetchState(String(msg.sid)), 300);
        } else if (state === "ended") {
          S().setActive(false);
          S().setProposal(null);
        } else if (state === "paused") {
          S().setAgent({ status: "paused", backlog_s: null });
        }
        return;
      }
      if (type === "state") {
        const version = Number(msg.version ?? 0);
        const local = S().board?.session.version ?? -1;
        if (local > version) return; // stale / out-of-order
        if (local < 0 || local < version - 1) {
          // First state or a gap: pull a full snapshot.
          void fetchState(String(msg.sid));
          return;
        }
        // local === version - 1 (next) or local === version (idempotent
        // re-broadcast carrying a delta, e.g. an attachment at the same
        // version). Always merge the delta.
        S().applyState(msg as never);
        S().setProposal(null);
        const focus = msg.focus as never;
        if (focus) S().applyFocus(focus, Date.now());
        return;
      }
      if (type === "caption") {
        S().addCaption(msg as unknown as Caption);
        return;
      }
      if (type === "captions") {
        for (const c of (msg.items as Caption[]) ?? []) S().addCaption(c);
        return;
      }
      if (type === "phase") {
        const current = S().board;
        if (current) {
          S().setBoard({
            ...current,
            session: { ...current.session, phase: msg.phase as string, current_item_id: (msg.item_id as string) ?? null },
          });
        }
        S().applyFocus({ tab: "agenda", id: (msg.item_id as string) ?? null, prio: 6 }, Date.now());
        return;
      }
      if (type === "proposal") {
        S().setProposal(msg as unknown as Proposal);
        return;
      }
      if (type === "announce") {
        S().setAnnouncement(msg as unknown as Announcement);
        return;
      }
      if (type === "agent") {
        S().setAgent({ status: (msg.status as never) ?? "listening", backlog_s: (msg.backlog_s as number) ?? null });
        return;
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, fetchState]);

  const startSession = useCallback(
    async (
      body: { template: string; mode: string; language: string; series_id?: string | null; goal?: string | null },
      files?: { agenda?: File | null; previousNotes?: File | null },
    ) => {
      if (!meetingId) throw new Error("no meeting");
      setLoading(true);
      try {
        S().clearLiveTranscript();
        const res = await meetppApi.createSession(meetingId, body);
        // Optional PDFs are uploaded (parsed in the background) before start.
        if (files?.agenda) await meetppApi.uploadDocument(res.session.id, "agenda", files.agenda);
        if (files?.previousNotes) await meetppApi.uploadDocument(res.session.id, "previous_notes", files.previousNotes);
        await meetppApi.start(res.session.id);
        await fetchState(res.session.id);
        return res.session.id;
      } finally {
        setLoading(false);
      }
    },
    [meetingId, fetchState],
  );

  const sid = (): string | null => S().session?.id ?? null;

  const sendOps = useCallback(async (ops: Array<Record<string, unknown>>) => {
    const id = sid();
    if (!id) return;
    await meetppApi.ops(id, ops);
  }, []);

  const acceptProposal = useCallback(async (proposal: Proposal) => {
    const id = sid();
    if (!id) return;
    await meetppApi.phase(id, { to: proposal.to, item_id: proposal.item_id, accept: true });
    S().setProposal(null);
  }, []);

  const rejectProposal = useCallback(async (proposal: Proposal) => {
    const id = sid();
    if (!id) return;
    await meetppApi.phase(id, { to: proposal.to, item_id: proposal.item_id, accept: false });
    S().setProposal(null);
  }, []);

  const jumpPhase = useCallback(async (to: string, itemId?: string | null) => {
    const id = sid();
    if (!id) return;
    await meetppApi.phase(id, { to, item_id: itemId ?? null, accept: true });
  }, []);

  const pauseResume = useCallback(async (paused: boolean) => {
    const id = sid();
    if (!id) return;
    if (paused) await meetppApi.pause(id);
    else await meetppApi.resume(id);
  }, []);

  const endSession = useCallback(async () => {
    const id = sid();
    if (!id) return;
    await meetppApi.end(id);
    S().setActive(false);
  }, []);

  const consent = useCallback(async (decision: "accept" | "opt_out") => {
    const id = sid();
    if (id) {
      try {
        await meetppApi.consent(id, decision);
      } catch {
        /* best effort */
      }
    }
    S().setConsent(decision === "accept" ? "accepted" : "opted_out");
  }, []);

  const boardToMain = useCallback(async () => {
    const id = sid();
    if (!id) return;
    await meetppApi.boardToMain(id);
  }, []);

  const snapshot = useCallback(async () => {
    const id = sid();
    if (!id) return;
    const [{ api }, wr] = await Promise.all([import("../../lib/api"), import("../../lib/meetpp/whiteboardRender")]);
    const [strokes, shapes] = await Promise.all([
      api.getWhiteboardStrokes(roomName),
      api.listWhiteboardShapes(roomName),
    ]);
    const blob = await wr.buildSnapshot(strokes, shapes, "#0b1220");
    const item = S().board?.agenda.find((a) => a.status === "active");
    await meetppApi.uploadAttachment(id, blob, `Whiteboard: ${item?.title ?? ""} — ${new Date().toLocaleTimeString()}`);
  }, [roomName]);

  return {
    loading,
    isOwner,
    startSession,
    sendOps,
    acceptProposal,
    rejectProposal,
    jumpPhase,
    pauseResume,
    endSession,
    consent,
    boardToMain,
    snapshot,
  };
}

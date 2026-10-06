import { RoomEvent, type Participant, type Room } from "livekit-client";
import { meetppApi } from "./api";
import { decideStateMessage } from "./state";
import {
  addCaption,
  addSegments,
  applyPosition,
  applySnapshot,
  clearSession,
  effectiveViewed,
  endSession,
  knownIds,
  mergeDelta,
  processActivations,
  refineCaption,
  setAgent,
  setConsentState,
  setProposal,
  setProviderLabel,
  setSpeaking,
  showAnnouncement,
  suppressProposal,
  useMeetpp,
} from "./store";
import type { Activation, HumanOp, MeetAiMessage, SegmentDto, SessionSettings, StateMsg } from "./types";
import { personKeyFor } from "./personKey";

export { personKeyFor };

/**
 * Meet++ session layer: discovery, snapshot + transcript loading, the
 * `meet-ai` data-channel handler (contract §4), consent and the chair /
 * editor actions. Shared by the room, the egress page and the public view.
 */

export const MEET_AI_TOPIC = "meet-ai";

const S = () => useMeetpp.getState();

// ── Loading ───────────────────────────────────────────────────────────────

let stateInflight: Promise<void> | null = null;
let stateAgain = false;
let pendingActivations: Array<{ version: number; activations: Activation[]; before: Set<string> }> = [];

/** GET /state, coalesced (one request in flight; repeats once if asked again). */
export function refetchState(): Promise<void> {
  const sid = S().sid;
  if (!sid) return Promise.resolve();
  if (stateInflight) {
    stateAgain = true;
    return stateInflight;
  }
  stateInflight = (async () => {
    do {
      stateAgain = false;
      try {
        const snap = await meetppApi.getState(sid);
        if (S().sid !== sid && S().sid !== null) return;
        applySnapshot(snap);
      } catch {
        /* keep the current board; the next message retries */
      }
    } while (stateAgain);
    flushPendingActivations();
  })().finally(() => {
    stateInflight = null;
  });
  return stateInflight;
}

function flushPendingActivations(): void {
  const local = S().snap?.version ?? -1;
  const ready = pendingActivations.filter((p) => p.version <= local);
  pendingActivations = pendingActivations.filter((p) => p.version > local);
  for (const p of ready) processActivations(p.activations, p.before);
}

let transcriptInflight: Promise<void> | null = null;
let transcriptAgain = false;
/** Re-read this many recent lines on reconnect to catch missed refinements. */
const RECONNECT_OVERLAP = 200;

/** Page GET /transcript from the last known seq (or from the start, or
 * `overlap` lines back) until `next_after` is null. */
export function syncTranscript(overlap = 0, fromStart = false): Promise<void> {
  const sid = S().sid;
  if (!sid) return Promise.resolve();
  if (transcriptInflight) {
    transcriptAgain = true;
    return transcriptInflight;
  }
  transcriptInflight = (async () => {
    do {
      transcriptAgain = false;
      const list = S().transcript;
      const anchor = overlap > 0 && list.length > overlap ? list[list.length - overlap - 1] : null;
      let after: number | null = fromStart
        ? 0
        : overlap > 0
          ? anchor
            ? anchor.seq
            : 0
          : list.length
            ? list[list.length - 1].seq
            : 0;
      overlap = 0;
      fromStart = false;
      for (let guard = 0; guard < 200 && after !== null; guard++) {
        try {
          const page = await meetppApi.getTranscript(sid, after, 1000);
          if (S().sid !== sid) return;
          const next: number | null = page.next_after ?? null;
          addSegments(page.segments ?? [], next === null);
          if (next !== null && next === after) break;
          after = next;
        } catch {
          addSegments([], true);
          break;
        }
      }
    } while (transcriptAgain);
  })().finally(() => {
    transcriptInflight = null;
  });
  return transcriptInflight;
}

/** Load a session: snapshot, then the whole transcript (paged). */
export async function loadSession(sid: string): Promise<void> {
  if (S().sid !== sid) {
    // A different session: drop the previous board, transcript and view.
    pendingActivations = [];
    clearSession();
    useMeetpp.setState({ sid, consent: "unknown", consentPrompt: false });
  }
  const snap = await meetppApi.getState(sid);
  applySnapshot(snap);
  // From the start: captions that arrived during GET /state must not move
  // the paging cursor past the history.
  void syncTranscript(0, true);
}

/** Discover the room's active session (no auth) and load it. */
export async function discover(roomName: string): Promise<boolean> {
  try {
    const res = await meetppApi.roomActive(roomName);
    if (res.provider_label) setProviderLabel(res.provider_label);
    if (res.active && res.sid) {
      if (S().sid !== res.sid || !S().snap) await loadSession(res.sid);
      else {
        await refetchState();
        // e.g. a session started from the setup preview: load its transcript.
        if (!S().transcriptReady) void syncTranscript(0, true);
      }
      afterLoad();
      return true;
    }
    if (S().active) endSession();
    return false;
  } catch {
    return false;
  }
}

// ── Consent (participants only) ───────────────────────────────────────────

const consentKey = (sid: string) => `meetpp-consent:${sid}`;

function storedConsent(sid: string): "accept" | "opt_out" | null {
  try {
    const v = sessionStorage.getItem(consentKey(sid));
    return v === "accept" || v === "opt_out" ? v : null;
  } catch {
    return null;
  }
}

/** POST /consent with the stored choice (every join / reconnect). */
export async function postConsent(): Promise<void> {
  const s = S();
  if (s.ctx.mode !== "participant" || !s.sid || !s.active) return;
  // The person_key needs the LiveKit identity (posted again on Connected).
  if (!s.ctx.localIdentity) return;
  const decision = storedConsent(s.sid);
  if (!decision) return;
  try {
    await meetppApi.consent(s.sid, {
      decision,
      person_key: personKeyFor(s.ctx.localIdentity),
      name: s.ctx.localName ?? undefined,
    });
  } catch {
    /* retried on the next reconnect */
  }
}

/** After a (re)load: restore the consent choice or ask for it. */
function afterLoad(): void {
  const s = S();
  if (s.ctx.mode !== "participant" || !s.sid || !s.active) return;
  const decision = storedConsent(s.sid);
  if (decision) {
    setConsentState(decision, false);
    void postConsent();
  } else {
    setConsentState("unknown", true);
  }
}

export async function chooseConsent(decision: "accept" | "opt_out"): Promise<void> {
  const sid = S().sid;
  if (!sid) return;
  try {
    sessionStorage.setItem(consentKey(sid), decision);
  } catch {
    /* ignore */
  }
  setConsentState(decision, false);
  await postConsent();
}

export function reopenConsent(): void {
  setConsentState(S().consent, true);
}

// ── meet-ai message handler (contract §4) ─────────────────────────────────

let positionTimer: ReturnType<typeof setTimeout> | null = null;

function onState(msg: StateMsg): void {
  const local = S().snap?.version ?? null;
  const decision = decideStateMessage(local, msg.version, !!msg.delta);
  if (decision === "ignore") return;
  const before = knownIds();
  if (decision === "merge" && msg.delta) {
    mergeDelta(msg.version, msg.delta);
    processActivations(msg.activations, before);
    return;
  }
  if (msg.activations?.length) pendingActivations.push({ version: msg.version, activations: msg.activations, before });
  void refetchState();
}

export function handleMessage(msg: MeetAiMessage, opts: { roomName?: string | null } = {}): void {
  const s = S();
  switch (msg.type) {
    case "caption": {
      if (!s.sid) return;
      const seg: SegmentDto = {
        seq: msg.seq,
        identity: msg.identity,
        name: msg.name,
        person_key: msg.person_key ?? null,
        t_start: msg.t_start ?? null,
        t_end: null,
        text: msg.text,
        text_refined: null,
        tier: 1,
        is_gap: false,
        gap_reason: null,
      };
      addCaption(seg);
      return;
    }
    case "caption-update":
      refineCaption(msg.seq, msg.text);
      return;
    case "gap":
      addSegments([
        {
          seq: msg.seq,
          identity: null,
          name: msg.name ?? null,
          person_key: null,
          t_start: msg.t_from,
          t_end: msg.t_to,
          text: "",
          text_refined: null,
          tier: 1,
          is_gap: true,
          gap_reason: msg.reason,
        },
      ]);
      return;
    case "state":
      if (!s.sid) {
        if (opts.roomName) void discover(opts.roomName);
        return;
      }
      onState(msg);
      return;
    case "position": {
      if (!s.snap) return;
      applyPosition(msg);
      // The authoritative section statuses come with a state delta of the
      // same version; refetch if it has not arrived shortly after.
      if (positionTimer) clearTimeout(positionTimer);
      const want = msg.version;
      positionTimer = setTimeout(() => {
        positionTimer = null;
        if ((S().snap?.version ?? -1) < want) void refetchState();
      }, 600);
      return;
    }
    case "announce":
      showAnnouncement(msg);
      return;
    case "proposal": {
      const target = s.snap?.sections.find((x) => x.id === msg.to_section_id);
      setProposal({
        pid: msg.pid,
        to: msg.to_section_id,
        title: msg.title || (target ? (target.number ? `${target.number} · ${target.title}` : target.title) : ""),
        reason: msg.reason,
        confidence: msg.confidence,
      });
      return;
    }
    case "agent":
      setAgent({ status: msg.status, backlog_s: msg.backlog_s ?? null, speakers: msg.speakers ?? [], tier2: msg.tier2 ?? "off" });
      return;
    case "session": {
      const st = msg.state;
      if (st === "started" || st === "resumed" || st === "paused") {
        const roomName = opts.roomName ?? s.ctx.roomName;
        if (st === "started" && roomName) void discover(roomName);
        else void refetchState().then(() => afterLoad());
        return;
      }
      // ended | finalising | review | published
      if (s.active) endSession();
      return;
    }
  }
}

function decode(payload: Uint8Array): MeetAiMessage | null {
  try {
    const msg = JSON.parse(new TextDecoder().decode(payload));
    return msg && typeof msg.type === "string" ? (msg as MeetAiMessage) : null;
  } catch {
    return null;
  }
}

const BOT_PREFIXES = ["meetpp-", "composite-", "playback", "EG_", "egress"];

export function isBotIdentity(identity: string): boolean {
  return BOT_PREFIXES.some((p) => identity.startsWith(p));
}

/** Wire a LiveKit room: data channel, active speakers, reconnects. */
export function attachRoom(room: Room, roomName: string): () => void {
  const onData = (payload: Uint8Array, _p?: unknown, _k?: unknown, topic?: string) => {
    if (topic !== MEET_AI_TOPIC) return;
    const msg = decode(payload);
    if (msg) handleMessage(msg, { roomName });
  };
  const onSpeakers = (speakers: Participant[]) => {
    setSpeaking(
      speakers
        .filter((p) => !isBotIdentity(p.identity))
        .map((p) => ({ identity: p.identity, name: p.name || p.identity })),
    );
  };
  const onReconnected = () => {
    void (async () => {
      // discover() re-posts the stored consent (afterLoad).
      const found = await discover(roomName);
      if (found) void syncTranscript(RECONNECT_OVERLAP);
    })();
  };
  room.on(RoomEvent.DataReceived, onData);
  room.on(RoomEvent.ActiveSpeakersChanged, onSpeakers);
  room.on(RoomEvent.Reconnected, onReconnected);
  room.on(RoomEvent.SignalReconnecting, () => undefined);
  return () => {
    room.off(RoomEvent.DataReceived, onData);
    room.off(RoomEvent.ActiveSpeakersChanged, onSpeakers);
    room.off(RoomEvent.Reconnected, onReconnected);
  };
}

export function resetSessionLayer(): void {
  pendingActivations = [];
  clearSession();
}

// ── Chair / editor actions ────────────────────────────────────────────────

function sid(): string {
  const id = S().sid;
  if (!id) throw new Error("No Meet++ session");
  return id;
}

async function afterWrite<T>(p: Promise<T>): Promise<T> {
  const res = await p;
  // Deltas normally arrive on the data channel; refetch if they are late.
  const v = (res as { version?: number } | undefined)?.version;
  if (typeof v === "number") {
    setTimeout(() => {
      if ((S().snap?.version ?? -1) < v) void refetchState();
    }, 800);
  }
  return res;
}

export const meetppActions = {
  next: () => afterWrite(meetppApi.position(sid(), { action: "next" })),
  back: () => afterWrite(meetppApi.position(sid(), { action: "back" })),
  move: (sectionId: string) => afterWrite(meetppApi.position(sid(), { action: "move", section_id: sectionId })),
  undo: () => afterWrite(meetppApi.positionUndo(sid())),
  acceptProposal: async (pid: string) => {
    await meetppApi.proposal(sid(), { pid, accept: true });
    useMeetpp.setState({ proposal: null });
  },
  rejectProposal: async (pid: string, target: string) => {
    suppressProposal(target);
    await meetppApi.proposal(sid(), { pid, accept: false });
  },
  ops: (ops: HumanOp[]) => afterWrite(meetppApi.ops(sid(), ops)),
  compose: (sectionId: string) => meetppApi.compose(sid(), sectionId).then((r) => {
    void refetchState();
    return r;
  }),
  patchAttendee: async (id: string, patch: Parameters<typeof meetppApi.patchAttendee>[2]) => {
    const a = await meetppApi.patchAttendee(sid(), id, patch);
    void refetchState();
    return a;
  },
  patchAttachment: async (id: string, caption: string) => {
    await meetppApi.patchAttachment(sid(), id, caption);
    void refetchState();
  },
  deleteAttachment: async (id: string) => {
    await meetppApi.deleteAttachment(sid(), id);
    void refetchState();
  },
  setMode: (mode: "lead" | "assist") => meetppApi.patchSession(sid(), { mode }).then(() => refetchState()),
  setSettings: (settings: SessionSettings) => meetppApi.patchSession(sid(), { settings }).then(() => refetchState()),
  setEditors: (editors: string[]) => meetppApi.patchSession(sid(), { editors }).then(() => refetchState()),
  pause: () => meetppApi.pause(sid()).then(() => refetchState()),
  resume: () => meetppApi.resume(sid()).then(() => refetchState()),
  end: async () => {
    const id = sid();
    await meetppApi.end(id);
    endSession();
    return id;
  },
  /** Render the shared whiteboard to PNG and file it under the viewed section. */
  snapshotWhiteboard: async (roomName: string) => {
    const id = sid();
    const [{ api }, wr] = await Promise.all([import("../api"), import("./whiteboardRender")]);
    const [strokes, shapes] = await Promise.all([api.getWhiteboardStrokes(roomName), api.listWhiteboardShapes(roomName)]);
    const blob = await wr.buildSnapshot(strokes, shapes, "#0b1220");
    const s = S();
    const sectionId = effectiveViewed(s);
    const section = s.snap?.sections.find((x) => x.id === sectionId);
    const label = section ? (section.number ? `${section.number} · ${section.title}` : section.title) : "";
    const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const caption = `Whiteboard${label ? ` · ${label}` : ""} · ${time}`;
    const res = await meetppApi.uploadAttachment(id, blob, caption, sectionId);
    void refetchState();
    return res.attachment;
  },
};

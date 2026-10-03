import { create } from "zustand";
import { decideFocus, type FollowState } from "./follow";
import type {
  AgentStatus,
  Announcement,
  BoardState,
  Caption,
  FocusHint,
  MeetppTab,
  Proposal,
  SessionMeta,
  TranscriptSegment,
} from "./types";

const MAX_CAPTIONS = 4;

function mergeById<T extends { id: string }>(existing: T[], incoming: T[]): T[] {
  const map = new Map(existing.map((x) => [x.id, x]));
  for (const item of incoming) map.set(item.id, { ...map.get(item.id), ...item });
  return Array.from(map.values());
}

export interface MeetppStore {
  active: boolean;
  session: SessionMeta | null;
  board: BoardState | null;
  captions: Caption[];
  transcript: TranscriptSegment[];
  // Live-only segments (never replaced by a backfill); drives the subtitles.
  liveTranscript: TranscriptSegment[];
  proposal: Proposal | null;
  announcement: Announcement | null;
  agent: AgentStatus;
  consent: "unknown" | "accepted" | "opted_out";
  follow: boolean;
  pausedUntil: number;
  lastSwitchAt: number;
  editing: boolean;
  tab: MeetppTab;
  focusId: string | null;
  highlights: Record<string, number>;
  unseen: Partial<Record<MeetppTab, boolean>>;
  lastFocusAt: number | null;
  lastUpdatedAt: number;

  setActive: (active: boolean) => void;
  setSession: (s: SessionMeta | null) => void;
  setTab: (tab: MeetppTab) => void;
  setBoard: (b: BoardState) => void;
  applyState: (msg: { version: number; changes: Array<{ kind: string; id: string | null; op: string }>; delta?: Record<string, unknown[]>; focus?: FocusHint | null }) => void;
  hydrateVersion: (version: number) => void;
  addCaption: (c: Caption) => void;
  setTranscript: (segments: TranscriptSegment[]) => void;
  clearLiveTranscript: () => void;
  setProposal: (p: Proposal | null) => void;
  setAnnouncement: (a: Announcement | null) => void;
  setAgent: (a: AgentStatus) => void;
  setConsent: (c: MeetppStore["consent"]) => void;
  toggleFollow: () => void;
  noteInteraction: () => void;
  setEditing: (editing: boolean) => void;
  markSeen: (tab: MeetppTab) => void;
  applyFocus: (hint: FocusHint | null | undefined, now: number) => void;
  setFocusId: (id: string | null) => void;
}

export const useMeetpp = create<MeetppStore>((set, get) => ({
  active: false,
  session: null,
  board: null,
  captions: [],
  transcript: [],
  liveTranscript: [],
  proposal: null,
  announcement: null,
  agent: { status: "listening", backlog_s: null },
  consent: "unknown",
  follow: true,
  pausedUntil: 0,
  lastSwitchAt: 0,
  editing: false,
  tab: "agenda",
  focusId: null,
  highlights: {},
  unseen: {},
  lastFocusAt: null,
  lastUpdatedAt: 0,

  setActive: (active) => set({ active }),
  setSession: (session) =>
    set((s) => ({
      session,
      active: session ? ["running", "paused", "finalising", "setup"].includes(session.status) : s.active,
    })),
  setTab: (tab) => set({ tab }),
  setBoard: (board) =>
    set((s) => ({
      board,
      session: board.session ?? s.session,
      active: true,
      lastUpdatedAt: Date.now(),
    })),
  applyState: (msg) =>
    set((s) => {
      const board = s.board;
      if (!board || !msg.delta) {
        return {};
      }
      const next: BoardState = { ...board };
      const delta = msg.delta as Record<string, unknown[]>;
      if (delta.agenda) next.agenda = mergeById(board.agenda, delta.agenda as never);
      if (delta.decisions) next.decisions = mergeById(board.decisions, delta.decisions as never);
      if (delta.actions) next.actions = mergeById(board.actions, delta.actions as never);
      if (delta.attendance) next.attendance = mergeById(board.attendance, delta.attendance as never);
      if (delta.minutes) next.minutes = mergeById(board.minutes, delta.minutes as never);
      if (delta.attachments) next.attachments = mergeById(board.attachments, delta.attachments as never);
      if (delta.session) next.session = { ...board.session, ...(delta.session[0] as unknown as SessionMeta) };
      if (next.session) next.session = { ...next.session, version: msg.version };

      const highlights = { ...s.highlights };
      const now = Date.now();
      for (const change of msg.changes) {
        const tab = kindToTab(change.kind);
        if (!tab) continue;
        if (change.id) highlights[change.id] = now + 4000;
        if (tab !== s.tab) {
          s.unseen[tab] = true;
        }
      }
      return { board: next, highlights, unseen: { ...s.unseen }, lastUpdatedAt: now };
    }),
  hydrateVersion: (version) =>
    set((s) => (s.board ? { board: { ...s.board, session: { ...s.board.session, version } } } : {})),
  addCaption: (c) =>
    set((s) => {
      const merged = s.captions.filter((x) => x.identity !== c.identity).concat(c);
      const already = s.transcript.some((t) => t.seq === c.seq);
      const seg: TranscriptSegment = {
        seq: c.seq,
        identity: c.identity,
        name: c.name,
        text: c.text,
        t_end: c.t,
        t_start: c.t_start ?? null,
        duration_ms: c.duration_ms ?? null,
      };
      const transcript = already ? s.transcript : [...s.transcript, seg];
      const liveTranscript = s.liveTranscript.some((t) => t.seq === c.seq)
        ? s.liveTranscript
        : [...s.liveTranscript, seg];
      return { captions: merged.slice(-MAX_CAPTIONS), transcript, liveTranscript };
    }),
  setTranscript: (segments) => set({ transcript: segments }),
  clearLiveTranscript: () => set({ liveTranscript: [] }),
  setProposal: (proposal) => set({ proposal }),
  setAnnouncement: (announcement) => set({ announcement }),
  setAgent: (agent) => set({ agent }),
  setConsent: (consent) => set({ consent }),
  toggleFollow: () =>
    set((s) => ({
      follow: !s.follow,
      pausedUntil: 0,
    })),
  noteInteraction: () => set({ pausedUntil: Date.now() + 30000 }),
  setEditing: (editing) => set({ editing }),
  markSeen: (tab) =>
    set((s) => {
      const unseen = { ...s.unseen };
      delete unseen[tab];
      return { unseen };
    }),
  applyFocus: (hint, now) => {
    const s = get();
    const st: FollowState = {
      follow: s.follow,
      pausedUntil: s.pausedUntil,
      lastSwitchAt: s.lastSwitchAt,
      editing: s.editing,
    };
    const decision = decideFocus(st, hint, now, s.lastFocusAt);
    if (decision) {
      set({ tab: decision.tab, focusId: decision.id, lastSwitchAt: now, lastFocusAt: now });
    }
  },
  setFocusId: (id) => set({ focusId: id }),
}));

export function kindToTab(kind: string): MeetppTab | null {
  switch (kind) {
    case "agenda":
      return "agenda";
    case "decision":
      return "decisions";
    case "action":
      return "actions";
    case "attendance":
      return "attendance";
    case "minutes":
      return "minutes";
    case "attachment":
      return "attachments";
    default:
      return null;
  }
}

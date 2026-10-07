import { create } from "zustand";
import i18n from "i18next";
import { FocusQueue, type FocusEntry, type FocusSnapshot, type FollowMode } from "./focusQueue";
import {
  applyDelta,
  applyRefinement,
  mergeSegments,
  outlineOrder,
  previousActionsHome,
  sectionLabel,
  sortSections,
  tabHasContentFor,
} from "./state";
import type {
  Activation,
  AgentInfo,
  AnnounceMsg,
  PositionMsg,
  SectionDto,
  SegmentDto,
  Snapshot,
  StateDelta,
  Tab,
} from "./types";

/**
 * Meet++ client store: the server snapshot (merged with deltas), the full
 * transcript, live signals (agent, proposal, undo, announcement, speakers),
 * and this user's view state (tab, viewed section, follow, highlights).
 *
 * The view state is per client (FDD §5.3). Automatic activations go through
 * the FocusQueue (focusQueue.ts); programmatic tab/scroll changes made by the
 * queue never count as user interactions.
 *
 * Only the recording / livestream view (egress) switches tabs on its own. A
 * person's board stays on the tab they chose: a change on another tab counts
 * up on that tab and makes it flash; a change on their tab is highlighted in
 * place and scrolled into view when it is out of sight.
 */

export type ClientMode = "participant" | "egress" | "public" | "review" | "none";

export interface MeetppCtx {
  mode: ClientMode;
  isChair: boolean;
  localIdentity: string | null;
  localName: string | null;
  meetingId: string | null;
  roomName: string | null;
}

export interface Highlight {
  tab: Tab;
  sectionId: string | null;
  items: string[];
  startedAt: number;
}

export interface Selected {
  kind: "decision" | "action" | "note";
  id: string;
  evidence: number[];
}

export interface CaptionLine {
  seq: number;
  name: string;
  text: string;
  at: number;
}

export interface LiveProposal {
  pid: string;
  to: string;
  title: string;
  reason: string;
  confidence: number;
}

export interface MeetppStore {
  ctx: MeetppCtx;
  sid: string | null;
  snap: Snapshot | null;
  /** Session running or paused: the board is shown. */
  active: boolean;
  /** Last session that ended in this room (chair: link to review). */
  endedSid: string | null;
  providerLabel: string | null;

  transcript: SegmentDto[];
  transcriptReady: boolean;
  captions: CaptionLine[];

  agent: AgentInfo | null;
  proposal: LiveProposal | null;
  /** The chair said "end this meeting": what was heard, awaiting confirmation. */
  endRequest: { heard: string; at: number } | null;
  undo: { from: string | null; to: string | null; until: number } | null;
  announcement: (AnnounceMsg & { at: number }) | null;
  speaking: Array<{ identity: string; name: string }>;

  consent: "unknown" | "accept" | "opt_out";
  consentPrompt: boolean;

  // view state (per client)
  tab: Tab;
  /** null = the live section. */
  viewedSectionId: string | null;
  /** Changes on each tab since it was last looked at. */
  unseen: Partial<Record<Tab, number>>;
  /** A tab that just received a change it does not show (it flashes). */
  flash: { tab: Tab; nonce: number } | null;
  badges: Record<string, "new" | "updated">;
  highlight: Highlight | null;
  follow: { mode: FollowMode; pausedUntil: number };
  /** ifHidden: only scroll when the target is out of sight (automatic). */
  scrollReq: { sectionId: string | null; itemId: string | null; nonce: number; ifHidden?: boolean } | null;
  selected: Selected | null;
  transcriptJump: { seq: number; nonce: number } | null;
  liveMsg: { text: string; nonce: number } | null;
  lastChange: { label: string; tab: Tab; sectionId: string | null; at: number } | null;
  expanded: Record<string, boolean>;

  // stage integration (the board is a stream window, lib/stage.ts):
  /** This viewer sees the full board as the main stream or zoomed. */
  boardOnStage: boolean;
  /** The room-wide presenter is the board. */
  boardPresenter: boolean;
}

const initialView = {
  tab: "agenda" as Tab,
  viewedSectionId: null,
  unseen: {},
  flash: null,
  badges: {},
  highlight: null,
  scrollReq: null,
  selected: null,
  transcriptJump: null,
  lastChange: null,
  expanded: {},
};

export const useMeetpp = create<MeetppStore>(() => ({
  ctx: { mode: "none", isChair: false, localIdentity: null, localName: null, meetingId: null, roomName: null },
  sid: null,
  snap: null,
  active: false,
  endedSid: null,
  providerLabel: null,
  transcript: [],
  transcriptReady: false,
  captions: [],
  agent: null,
  proposal: null,
  endRequest: null,
  undo: null,
  announcement: null,
  speaking: [],
  consent: "unknown",
  consentPrompt: false,
  ...initialView,
  follow: { mode: "following", pausedUntil: 0 },
  liveMsg: null,
  boardOnStage: false,
  boardPresenter: false,
}));

const get = () => useMeetpp.getState();
const set = useMeetpp.setState;

let scrollNonce = 0;

// ── Focus queue binding ───────────────────────────────────────────────────

let fq: FocusQueue | null = null;
/** People's boards never change tab on their own (egress does). */
let inPlace = true;
let flashNonce = 0;

function onShow(entry: FocusEntry, at: number): void {
  if (inPlace) return showInPlace(entry, at);
  const s = get();
  const live = s.snap?.session.live_section_id ?? null;
  const sectionId = entry.sectionId ?? s.viewedSectionId ?? live;
  const unseen = { ...s.unseen };
  delete unseen[entry.tab];
  set({
    tab: entry.tab,
    viewedSectionId: sectionId && sectionId !== live ? sectionId : null,
    highlight: { tab: entry.tab, sectionId, items: entry.items, startedAt: at },
    scrollReq: { sectionId, itemId: entry.items[0] ?? null, nonce: ++scrollNonce },
    unseen,
    expanded: sectionId ? { ...s.expanded, [`${entry.tab}:${sectionId}`]: true } : s.expanded,
  });
  announceLive(describeActivation(entry));
}

/** Highlight the change on the tab being looked at, without moving the
 * viewer: no tab switch, no change of the viewed section, no return jump. An
 * item is scrolled into view only when it is out of sight (MeetppTabs); a
 * new live point only on the tabs organised by point. */
function showInPlace(entry: FocusEntry, at: number): void {
  const s = get();
  if (entry.tab !== s.tab) return;
  const live = s.snap?.session.live_section_id ?? null;
  const sectionId = entry.sectionId ?? live;
  const item = entry.items[0] ?? null;
  const scroll = item !== null || (entry.kind === "topic" && (s.tab === "agenda" || s.tab === "minutes") && s.viewedSectionId === null);
  set({
    highlight: { tab: entry.tab, sectionId, items: entry.items, startedAt: at },
    scrollReq: scroll ? { sectionId, itemId: item, nonce: ++scrollNonce, ifHidden: true } : s.scrollReq,
    expanded: sectionId && item ? { ...s.expanded, [`${entry.tab}:${sectionId}`]: true } : s.expanded,
  });
  announceLive(describeActivation(entry));
}

/** The view an item activation returns to (tab and viewed section). */
interface FocusView {
  tab: Tab;
  viewedSectionId: string | null;
}

function captureView(): FocusView {
  const s = get();
  return { tab: s.tab, viewedSectionId: s.viewedSectionId };
}

function restoreView(view: unknown): void {
  const v = view as FocusView;
  const s = get();
  const live = s.snap?.session.live_section_id ?? null;
  const sectionId = v.viewedSectionId ?? live;
  set({
    tab: v.tab,
    viewedSectionId: v.viewedSectionId,
    highlight: null,
    scrollReq: { sectionId, itemId: null, nonce: ++scrollNonce },
  });
}

function onChange(snap: FocusSnapshot): void {
  const s = get();
  const patch: Partial<MeetppStore> = {};
  if (s.follow.mode !== snap.mode || s.follow.pausedUntil !== snap.pausedUntil) {
    patch.follow = { mode: snap.mode, pausedUntil: snap.pausedUntil };
  }
  if (!snap.current && s.highlight) patch.highlight = null;
  if (Object.keys(patch).length) set(patch);
}

/** (Re)create the focus queue for this client. Egress always follows. */
export function configureFocus(alwaysFollow: boolean): FocusQueue {
  if (fq && fq.alwaysFollow === alwaysFollow) return fq;
  fq?.stop();
  inPlace = !alwaysFollow;
  // In place there is no earlier view to go back to.
  fq = inPlace
    ? new FocusQueue({ show: onShow, onChange, alwaysFollow })
    : new FocusQueue({ show: onShow, onChange, alwaysFollow, capture: captureView, restore: restoreView });
  fq.start();
  set({ follow: { mode: "following", pausedUntil: 0 } });
  return fq;
}

export function focusQueue(): FocusQueue {
  return fq ?? configureFocus(false);
}

// ── Polite live region (≤ 1 announcement per 10 s, FDD §5.14) ─────────────

let lastLiveAt = 0;
let pendingLive: string | null = null;
let liveTimer: ReturnType<typeof setTimeout> | null = null;
let liveNonce = 0;

export function announceLive(text: string): void {
  if (!text) return;
  const now = Date.now();
  if (now - lastLiveAt >= 10000) {
    lastLiveAt = now;
    set({ liveMsg: { text, nonce: ++liveNonce } });
    return;
  }
  pendingLive = text;
  if (liveTimer) return;
  liveTimer = setTimeout(() => {
    liveTimer = null;
    if (pendingLive) {
      lastLiveAt = Date.now();
      set({ liveMsg: { text: pendingLive, nonce: ++liveNonce } });
      pendingLive = null;
    }
  }, 10000 - (now - lastLiveAt));
}

function sectionById(id: string | null | undefined): SectionDto | null {
  if (!id) return null;
  return get().snap?.sections.find((s) => s.id === id) ?? null;
}

/** i18next outside React; falls back to English before i18n is initialised. */
function safeT(fn: () => unknown, fallback: string): string {
  try {
    const v = fn();
    return typeof v === "string" && v && !v.startsWith("meetpp.") ? v : fallback;
  } catch {
    return fallback;
  }
}

function describeActivation(entry: Pick<FocusEntry, "kind" | "sectionId">): string {
  const sec = sectionLabel(sectionById(entry.sectionId));
  switch (entry.kind) {
    case "decision":
      return sec
        ? safeT(() => i18n.t("meetpp.live.decisionIn", { defaultValue: "New decision in {{sec}}", sec }), `New decision in ${sec}`)
        : safeT(() => i18n.t("meetpp.live.decision", { defaultValue: "New decision" }), "New decision");
    case "action":
      return sec
        ? safeT(() => i18n.t("meetpp.live.actionIn", { defaultValue: "New action in {{sec}}", sec }), `New action in ${sec}`)
        : safeT(() => i18n.t("meetpp.live.action", { defaultValue: "New action" }), "New action");
    case "attendance":
      return safeT(() => i18n.t("meetpp.live.attendance", { defaultValue: "Attendance updated" }), "Attendance updated");
    case "topic":
      return sec ? safeT(() => i18n.t("meetpp.live.topic", { defaultValue: "Now discussing {{sec}}", sec }), `Now discussing ${sec}`) : "";
  }
}

// ── Context / session lifecycle ───────────────────────────────────────────

export function setCtx(patch: Partial<MeetppCtx>): void {
  set((s) => ({ ctx: { ...s.ctx, ...patch } }));
}

function isActiveStatus(status: string | undefined): boolean {
  return status === "running" || status === "paused";
}

/** Replace the snapshot (initial load or refetch). Never goes backwards. */
export function applySnapshot(snap: Snapshot): boolean {
  const s = get();
  const sameSession = s.sid === snap.sid || s.sid === snap.session.id;
  if (sameSession && s.snap && snap.version < s.snap.version) return false;
  const sid = snap.sid || snap.session.id;
  const fresh: Snapshot = { ...snap, sid, sections: sortSections(snap.sections ?? []) };
  const patch: Partial<MeetppStore> = {
    sid,
    snap: fresh,
    active: isActiveStatus(fresh.session.status),
    agent: fresh.session.agent ?? s.agent,
  };
  if (!sameSession) {
    Object.assign(patch, initialView, {
      transcript: [],
      transcriptReady: false,
      captions: [],
      proposal: null,
      undo: null,
      consent: "unknown",
      consentPrompt: false,
    });
    fq?.reset();
  }
  // Proposal / undo carried in the session (late joiners, reconnects).
  const sp = fresh.session.proposal;
  if (sp && s.ctx.isChair && !isSuppressed(sp.to)) {
    const target = fresh.sections.find((x) => x.id === sp.to);
    patch.proposal = { pid: sp.pid, to: sp.to, title: sectionLabel(target) || sp.to, reason: sp.reason, confidence: sp.confidence };
  } else if (!sp && sameSession) {
    patch.proposal = null;
  }
  const su = fresh.session.undo;
  const until = su ? Date.parse(su.until) : NaN;
  patch.undo = su && Number.isFinite(until) && until > Date.now() ? { from: su.from, to: su.to, until } : null;
  set(patch);
  return true;
}

/** Merge a `state` delta that passed the version rule. Returns ids known
 * before the merge (to tell NEW from UPDATED for badges). */
export function mergeDelta(version: number, delta: StateDelta): void {
  const s = get();
  if (!s.snap) return;
  const next = applyDelta(s.snap, delta, version);
  const patch: Partial<MeetppStore> = { snap: next, active: isActiveStatus(next.session.status) };
  if (delta.session && "proposal" in delta.session && delta.session.proposal === null) patch.proposal = null;
  if (delta.session && "undo" in delta.session) {
    const su = delta.session.undo;
    const until = su ? Date.parse(su.until) : NaN;
    patch.undo = su && Number.isFinite(until) && until > Date.now() ? { from: su.from, to: su.to, until } : null;
  }
  if (delta.session?.agent) patch.agent = delta.session.agent;
  // Minutes notes / composition never activate; they only mark the tab unseen.
  const unseen = { ...s.unseen };
  let flash: Tab | null = null;
  if (delta.minutes?.length && s.tab !== "minutes") {
    unseen.minutes = (unseen.minutes ?? 0) + delta.minutes.length;
    flash = "minutes";
  }
  const papers = (delta.attachments?.length ?? 0) + (delta.documents?.length ?? 0);
  if (papers && s.tab !== "papers") {
    unseen.papers = (unseen.papers ?? 0) + papers;
    flash = "papers";
  }
  patch.unseen = unseen;
  if (flash && inPlace) patch.flash = { tab: flash, nonce: ++flashNonce };
  set(patch);
}

export function knownIds(): Set<string> {
  const snap = get().snap;
  const out = new Set<string>();
  if (!snap) return out;
  for (const list of [snap.decisions, snap.actions, snap.attendees]) for (const x of list) out.add(x.id);
  return out;
}

/** Badges + unseen dots for every activation, then the focus queue. */
export function processActivations(activations: Activation[] | undefined, before: Set<string>): void {
  if (!activations?.length) return;
  const s = get();
  const badges = { ...s.badges };
  const unseen = { ...s.unseen };
  let lastChange = s.lastChange;
  let flash: Tab | null = null;
  for (const a of activations) {
    const tab = a.tab as Tab;
    if (a.item_id) badges[a.item_id] = before.has(a.item_id) ? (badges[a.item_id] === "new" ? "new" : "updated") : "new";
    const elsewhere = s.tab !== tab;
    if (a.kind !== "topic" && (elsewhere || (!inPlace && s.follow.mode !== "following"))) {
      unseen[tab] = (unseen[tab] ?? 0) + 1;
      if (elsewhere) flash = tab;
    }
    if (a.kind !== "topic" || !lastChange) lastChange = { label: changeLabel(a), tab, sectionId: a.section_id, at: Date.now() };
  }
  set({ badges, unseen, lastChange, ...(flash && inPlace ? { flash: { tab: flash, nonce: ++flashNonce } } : {}) });
  const q = focusQueue();
  for (const a of activations) {
    // In place, only what the viewer's tab shows is highlighted — the Agenda
    // lists each point's decisions and actions too.
    if (inPlace && a.tab !== s.tab && a.kind !== "topic") {
      if (s.tab === "agenda" && a.item_id && (a.kind === "decision" || a.kind === "action")) {
        q.push({ kind: a.kind, tab: "agenda", sectionId: a.section_id ?? null, itemId: a.item_id, prio: a.prio });
      }
      continue;
    }
    q.push({ kind: a.kind, tab: inPlace && a.kind === "topic" ? s.tab : (a.tab as Tab), sectionId: a.section_id ?? null, itemId: a.item_id ?? null, prio: a.prio });
  }
}

function changeLabel(a: Activation): string {
  const snap = get().snap;
  if (!snap) return "";
  if (a.kind === "decision") {
    const d = snap.decisions.find((x) => x.id === a.item_id);
    return d ? `${d.ref} ${d.title}` : "";
  }
  if (a.kind === "action") {
    const x = snap.actions.find((y) => y.id === a.item_id);
    return x ? `${x.ref} ${x.title}` : "";
  }
  if (a.kind === "attendance") {
    const x = snap.attendees.find((y) => y.id === a.item_id);
    return x ? `${x.name}: ${x.status.replace("_", " ")}` : "";
  }
  return sectionLabel(snap.sections.find((x) => x.id === a.section_id));
}

export function applyPosition(msg: PositionMsg): void {
  const s = get();
  if (!s.snap) return;
  const live = msg.live_section_id;
  const sections = s.snap.sections.map((x) => {
    if (x.id === live && x.status !== "live") return { ...x, status: "live" as const, started_at: new Date().toISOString() };
    return x;
  });
  const patch: Partial<MeetppStore> = {
    snap: { ...s.snap, sections, session: { ...s.snap.session, live_section_id: live } },
  };
  if (s.viewedSectionId === live) patch.viewedSectionId = null;
  if (msg.by === "ai" && msg.undo_until) {
    const until = Date.parse(msg.undo_until);
    if (Number.isFinite(until)) patch.undo = { from: msg.prev_section_id, to: live, until };
  } else {
    patch.undo = null;
  }
  if (s.proposal && s.proposal.to === live) patch.proposal = null;
  if (s.viewedSectionId === null && (!inPlace || s.tab === "agenda" || s.tab === "minutes")) {
    patch.scrollReq = { sectionId: live, itemId: null, nonce: ++scrollNonce, ifHidden: inPlace };
  }
  // The point the meeting leaves stays open where it was open as the live
  // one: a list someone is reading must not fold up under them.
  const prev = msg.prev_section_id;
  if (prev && inPlace) {
    const expanded = { ...s.expanded };
    for (const tab of ["decisions", "actions", "minutes"] as Tab[]) expanded[`${tab}:${prev}`] ??= true;
    patch.expanded = expanded;
  }
  set(patch);
}

export function endSession(): void {
  const s = get();
  fq?.reset();
  set({
    active: false,
    endedSid: s.sid,
    proposal: null,
    endRequest: null,
    undo: null,
    consentPrompt: false,
    announcement: s.announcement,
  });
}

export function clearSession(): void {
  fq?.reset();
  set({
    sid: null,
    snap: null,
    active: false,
    transcript: [],
    transcriptReady: false,
    captions: [],
    proposal: null,
    endRequest: null,
    undo: null,
    ...initialView,
  });
}

// ── Transcript ────────────────────────────────────────────────────────────

export function addSegments(segments: SegmentDto[], ready = false): void {
  if (!segments.length && !ready) return;
  set((s) => ({ transcript: mergeSegments(s.transcript, segments), transcriptReady: s.transcriptReady || ready }));
}

export function addCaption(seg: SegmentDto): void {
  set((s) => {
    const captions = s.captions.filter((c) => c.seq !== seg.seq).concat({
      seq: seg.seq,
      name: seg.name ?? seg.identity ?? "",
      text: seg.text,
      at: Date.now(),
    });
    return { transcript: mergeSegments(s.transcript, [seg]), captions: captions.slice(-4) };
  });
}

export function refineCaption(seq: number, text: string): void {
  set((s) => ({
    transcript: applyRefinement(s.transcript, seq, text),
    captions: s.captions.map((c) => (c.seq === seq ? { ...c, text } : c)),
  }));
}

// ── Live signals ──────────────────────────────────────────────────────────

const suppressed = new Map<string, number>();
const NOT_NOW_MS = 3 * 60 * 1000;

function isSuppressed(target: string): boolean {
  const until = suppressed.get(target);
  return until !== undefined && until > Date.now();
}

export function setEndRequest(heard: string | null): void {
  if (heard && !get().ctx.isChair) return;
  set({ endRequest: heard ? { heard, at: Date.now() } : null });
}

export function setProposal(p: LiveProposal | null): void {
  if (p && (!get().ctx.isChair || isSuppressed(p.to))) return;
  set({ proposal: p });
}

/** "Not now" suppresses the same target for 3 minutes (FDD §5.9). */
export function suppressProposal(target: string): void {
  suppressed.set(target, Date.now() + NOT_NOW_MS);
  set({ proposal: null });
}

let announceTimer: ReturnType<typeof setTimeout> | null = null;
export const ANNOUNCE_MS = 4000;

export function showAnnouncement(msg: AnnounceMsg): void {
  set({ announcement: { ...msg, at: Date.now() } });
  if (announceTimer) clearTimeout(announceTimer);
  announceTimer = setTimeout(() => {
    if (get().announcement?.aid === msg.aid) set({ announcement: null });
  }, ANNOUNCE_MS);
}

export function dismissAnnouncement(): void {
  set({ announcement: null });
}

export function setAgent(agent: AgentInfo): void {
  set({ agent });
}

export function setSpeaking(list: Array<{ identity: string; name: string }>): void {
  const cur = get().speaking;
  if (cur.length === list.length && cur.every((c, i) => c.identity === list[i].identity && c.name === list[i].name)) return;
  set({ speaking: list });
}

// ── View actions (manual unless stated) ───────────────────────────────────

export function interact(): void {
  focusQueue().interact();
}

export function liveSectionId(): string | null {
  return get().snap?.session.live_section_id ?? null;
}

export function effectiveViewed(s: Pick<MeetppStore, "viewedSectionId" | "snap"> = get()): string | null {
  return s.viewedSectionId ?? s.snap?.session.live_section_id ?? null;
}

function contentData() {
  const snap = get().snap!;
  return { sections: snap.sections, decisions: snap.decisions, actions: snap.actions, minutes: snap.minutes, attachments: snap.attachments };
}

/** Click on a tab (or Alt+1…6): keeps the viewed section, pauses 10 s. */
export function selectTab(tab: Tab, manual = true): void {
  const s = get();
  const unseen = { ...s.unseen };
  delete unseen[tab];
  set({ tab, unseen, scrollReq: { sectionId: effectiveViewed(s), itemId: null, nonce: ++scrollNonce } });
  if (manual) interact();
}

/** Click on an outline row (or Alt+↑/↓): view that section; Agenda when the
 * current tab has nothing for it; pauses 10 s. */
export function viewSection(sectionId: string, manual = true): void {
  const s = get();
  if (!s.snap) return;
  const live = s.snap.session.live_section_id;
  let tab = s.tab;
  if (!tabHasContentFor(tab, sectionId, contentData())) tab = "agenda";
  const unseen = { ...s.unseen };
  delete unseen[tab];
  set({
    tab,
    unseen,
    viewedSectionId: sectionId === live ? null : sectionId,
    scrollReq: { sectionId, itemId: null, nonce: ++scrollNonce },
    expanded: { ...s.expanded, [`${tab}:${sectionId}`]: true },
  });
  if (manual) interact();
}

/** Alt+↑ / Alt+↓: move the viewed section through the outline. */
export function stepViewed(delta: 1 | -1): void {
  const s = get();
  if (!s.snap) return;
  const order = outlineOrder(s.snap.sections).filter((x) => x.status !== "skipped");
  if (!order.length) return;
  const cur = effectiveViewed(s);
  const i = Math.max(0, order.findIndex((x) => x.id === cur));
  const next = order[Math.min(order.length - 1, Math.max(0, i + delta))];
  viewSection(next.id, true);
}

/** "Back to live" / LIVE chip / Esc: view the live section, end the pause. */
export function backToLive(): void {
  const s = get();
  if (!s.snap) return;
  const live = s.snap.session.live_section_id;
  let tab = s.tab;
  if (live && !tabHasContentFor(tab, live, contentData())) tab = "agenda";
  set({ viewedSectionId: null, tab, scrollReq: { sectionId: live, itemId: null, nonce: ++scrollNonce } });
  focusQueue().resume();
}

export function toggleFollow(): void {
  const q = focusQueue();
  q.setFollow(!q.following);
}

export function resumeFollow(): void {
  focusQueue().resume();
}

export function toggleGroup(tab: Tab, sectionId: string, open?: boolean): void {
  const key = `${tab}:${sectionId}`;
  set((s) => ({ expanded: { ...s.expanded, [key]: open ?? !s.expanded[key] } }));
}

export function selectItem(sel: Selected | null, manual = true): void {
  set({ selected: sel });
  if (manual) interact();
}

/** Show an item's tab and section and scroll to it (manual navigation). */
export function showItem(kind: "decision" | "action", id: string): void {
  const s = get();
  if (!s.snap) return;
  const tab: Tab = kind === "decision" ? "decisions" : "actions";
  const item = kind === "decision" ? s.snap.decisions.find((d) => d.id === id) : s.snap.actions.find((a) => a.id === id);
  if (!item) return;
  let sectionId = item.section_id;
  if (kind === "action" && (item as { previous?: boolean }).previous) {
    sectionId = previousActionsHome(s.snap.sections)?.id ?? sectionId;
  }
  const live = s.snap.session.live_section_id;
  const unseen = { ...s.unseen };
  delete unseen[tab];
  set({
    tab,
    unseen,
    viewedSectionId: sectionId && sectionId !== live ? sectionId : null,
    scrollReq: { sectionId, itemId: id, nonce: ++scrollNonce },
    expanded: sectionId ? { ...s.expanded, [`${tab}:${sectionId}`]: true } : s.expanded,
  });
  interact();
}

export function jumpToTranscript(seq: number): void {
  set({ transcriptJump: { seq, nonce: ++scrollNonce } });
}

export function clearBadge(id: string): void {
  const s = get();
  if (!(id in s.badges)) return;
  const badges = { ...s.badges };
  delete badges[id];
  set({ badges });
}

export function markTabSeen(tab: Tab): void {
  const s = get();
  if (!s.unseen[tab]) return;
  const unseen = { ...s.unseen };
  delete unseen[tab];
  set({ unseen });
}

export function setStage(patch: Partial<Pick<MeetppStore, "boardOnStage" | "boardPresenter">>): void {
  const s = get();
  const changed = (Object.keys(patch) as Array<keyof typeof patch>).some((k) => s[k] !== patch[k]);
  if (changed) set(patch);
}

export function setConsentState(consent: MeetppStore["consent"], prompt: boolean): void {
  set({ consent, consentPrompt: prompt });
}

export function setProviderLabel(label: string | null): void {
  set({ providerLabel: label });
}

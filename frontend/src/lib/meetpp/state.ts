/**
 * Pure helpers for the Meet++ client state: the version rule for `state`
 * messages, delta merging, transcript merging and grouping, outline
 * navigation and search normalisation. No React, no network — unit-tested.
 */

import type {
  ActionDto,
  CollectionKey,
  DecisionDto,
  SectionDto,
  SegmentDto,
  Snapshot,
  StateDelta,
  Tab,
} from "./types";

// ── Version rule (contract §4) ────────────────────────────────────────────

export type StateDecision = "merge" | "ignore" | "refetch";

/** `version == local+1` with a delta → merge; `version <= local` → ignore;
 * anything else (gap, first message, delta omitted) → refetch. */
export function decideStateMessage(local: number | null, version: number, hasDelta: boolean): StateDecision {
  if (local === null || local < 0) return "refetch";
  if (version <= local) return "ignore";
  if (version === local + 1 && hasDelta) return "merge";
  return "refetch";
}

// ── Delta merge ───────────────────────────────────────────────────────────

const REMOVED_KIND: Record<string, CollectionKey> = {
  section: "sections",
  sections: "sections",
  decision: "decisions",
  decisions: "decisions",
  action: "actions",
  actions: "actions",
  minute: "minutes",
  minutes: "minutes",
  attendee: "attendees",
  attendees: "attendees",
  attendance: "attendees",
  attachment: "attachments",
  attachments: "attachments",
  document: "documents",
  documents: "documents",
};

export function mergeById<T extends { id: string }>(existing: T[], incoming: T[] | undefined): T[] {
  if (!incoming || incoming.length === 0) return existing;
  const index = new Map(existing.map((x, i) => [x.id, i]));
  const out = existing.slice();
  for (const item of incoming) {
    const i = index.get(item.id);
    if (i === undefined) {
      index.set(item.id, out.length);
      out.push(item);
    } else {
      out[i] = { ...out[i], ...item };
    }
  }
  return out;
}

const COLLECTIONS: CollectionKey[] = ["sections", "decisions", "actions", "minutes", "attendees", "attachments", "documents"];

/** Apply a `state` delta to a snapshot (merge by id, then `removed`). */
export function applyDelta(snap: Snapshot, delta: StateDelta, version: number): Snapshot {
  const next: Snapshot = { ...snap, version };
  for (const key of COLLECTIONS) {
    const incoming = delta[key] as Array<{ id: string }> | undefined;
    if (incoming) (next[key] as Array<{ id: string }>) = mergeById(snap[key] as Array<{ id: string }>, incoming);
  }
  if (delta.session) next.session = { ...snap.session, ...delta.session };
  if ("quorum" in delta) next.quorum = delta.quorum ?? null;
  for (const r of delta.removed ?? []) {
    const key = REMOVED_KIND[r.kind];
    if (!key) continue;
    (next[key] as Array<{ id: string }>) = (next[key] as Array<{ id: string }>).filter((x) => x.id !== r.id);
  }
  next.sections = sortSections(next.sections);
  return next;
}

export function sortSections(sections: SectionDto[]): SectionDto[] {
  return sections.slice().sort((a, b) => a.position - b.position);
}

// ── Transcript ────────────────────────────────────────────────────────────

/** Index of the first segment with seq >= `seq` (segments sorted by seq). */
export function lowerBound(segments: SegmentDto[], seq: number): number {
  let lo = 0;
  let hi = segments.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (segments[mid].seq < seq) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Merge segments by seq, keeping the list sorted. Incoming rows replace
 * existing ones (server rows carry refined text) unless they would downgrade
 * a tier-2 line back to tier 1. */
export function mergeSegments(existing: SegmentDto[], incoming: SegmentDto[]): SegmentDto[] {
  if (incoming.length === 0) return existing;
  const sorted = incoming.slice().sort((a, b) => a.seq - b.seq);
  const last = existing[existing.length - 1];
  // Fast path: pure append (the live case and paging).
  if (!last || sorted[0].seq > last.seq) return existing.concat(dedupeSorted(sorted));
  const out = existing.slice();
  for (const seg of sorted) {
    const i = lowerBound(out, seg.seq);
    if (i < out.length && out[i].seq === seg.seq) {
      const cur = out[i];
      const downgrade = cur.tier >= 2 && seg.tier < 2;
      out[i] = downgrade ? { ...seg, text_refined: cur.text_refined, tier: cur.tier } : { ...cur, ...seg };
    } else {
      out.splice(i, 0, seg);
    }
  }
  return out;
}

function dedupeSorted(list: SegmentDto[]): SegmentDto[] {
  const out: SegmentDto[] = [];
  for (const s of list) {
    if (out.length && out[out.length - 1].seq === s.seq) out[out.length - 1] = s;
    else out.push(s);
  }
  return out;
}

/** Tier-2 refinement: replace the line's text in place. Returns the same
 * array when the seq is unknown (the refinement is applied when the line
 * arrives via the transcript fetch, which carries text_refined). */
export function applyRefinement(segments: SegmentDto[], seq: number, text: string): SegmentDto[] {
  const i = lowerBound(segments, seq);
  if (i >= segments.length || segments[i].seq !== seq) return segments;
  const out = segments.slice();
  out[i] = { ...out[i], text_refined: text, tier: 2 };
  return out;
}

export function displayText(seg: SegmentDto): string {
  return (seg.text_refined ?? seg.text ?? "").trim();
}

export function toMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

export interface TranscriptBlock {
  key: string;
  kind: "speech" | "gap";
  identity: string | null;
  name: string | null;
  tStart: string | null;
  segments: SegmentDto[];
}

export const MERGE_WINDOW_MS = 4000;

/** Group consecutive lines of the same speaker within 4 s into one block
 * (visual merge, FDD §5.6); gaps are their own blocks. */
export function groupTranscript(segments: SegmentDto[], mergeWindowMs = MERGE_WINDOW_MS): TranscriptBlock[] {
  const blocks: TranscriptBlock[] = [];
  let prevEnd: number | null = null;
  for (const seg of segments) {
    if (seg.is_gap) {
      blocks.push({ key: `g${seg.seq}`, kind: "gap", identity: seg.identity, name: seg.name, tStart: seg.t_start, segments: [seg] });
      prevEnd = null;
      continue;
    }
    const last = blocks[blocks.length - 1];
    const start = toMs(seg.t_start);
    const sameSpeaker =
      last && last.kind === "speech" && (last.identity ?? last.name) === (seg.identity ?? seg.name);
    const close = start !== null && prevEnd !== null ? start - prevEnd <= mergeWindowMs : start === null;
    if (last && sameSpeaker && close) {
      last.segments.push(seg);
    } else {
      blocks.push({ key: `s${seg.seq}`, kind: "speech", identity: seg.identity, name: seg.name, tStart: seg.t_start, segments: [seg] });
    }
    prevEnd = toMs(seg.t_end) ?? start ?? prevEnd;
  }
  return blocks;
}

// ── Search (case- and accent-insensitive) ─────────────────────────────────

const DIACRITICS = /[̀-ͯ]/g;

export function normalise(text: string): string {
  return text.normalize("NFD").replace(DIACRITICS, "").toLowerCase();
}

/** Match ranges of `query` in `text` (indexes into the original text). */
export function findMatches(text: string, query: string): Array<[number, number]> {
  const q = normalise(query.trim());
  if (!q) return [];
  let norm = "";
  const map: number[] = [];
  for (let i = 0; i < text.length; i++) {
    const n = normalise(text[i]);
    for (let k = 0; k < n.length; k++) {
      norm += n[k];
      map.push(i);
    }
  }
  const out: Array<[number, number]> = [];
  let from = 0;
  for (;;) {
    const at = norm.indexOf(q, from);
    if (at < 0) break;
    out.push([map[at], map[at + q.length - 1] + 1]);
    from = at + q.length;
  }
  return out;
}

// ── Outline ───────────────────────────────────────────────────────────────

/** Sections in outline order: each top-level row followed by its sub-points. */
export function outlineOrder(sections: SectionDto[]): SectionDto[] {
  const sorted = sortSections(sections);
  const byParent = new Map<string, SectionDto[]>();
  const tops: SectionDto[] = [];
  const ids = new Set(sorted.map((s) => s.id));
  for (const s of sorted) {
    if (s.parent_id && ids.has(s.parent_id)) {
      const list = byParent.get(s.parent_id) ?? [];
      list.push(s);
      byParent.set(s.parent_id, list);
    } else {
      tops.push(s);
    }
  }
  const out: SectionDto[] = [];
  for (const t of tops) {
    out.push(t);
    for (const c of byParent.get(t.id) ?? []) out.push(c);
  }
  return out;
}

export function childrenOf(sections: SectionDto[], parentId: string): SectionDto[] {
  return sortSections(sections.filter((s) => s.parent_id === parentId));
}

/** Top-level sections that Next/Back can move to (skipped ones are passed over). */
export function navigableTopLevel(sections: SectionDto[]): SectionDto[] {
  const ids = new Set(sections.map((s) => s.id));
  return sortSections(sections).filter((s) => !(s.parent_id && ids.has(s.parent_id)) && s.status !== "skipped");
}

function topLevelOf(sections: SectionDto[], id: string | null): SectionDto | null {
  if (!id) return null;
  const s = sections.find((x) => x.id === id);
  if (!s) return null;
  if (s.parent_id) return sections.find((x) => x.id === s.parent_id) ?? s;
  return s;
}

/** Target of Next ▶ (contract §1: outline order of top-level sections). */
export function nextSection(sections: SectionDto[], liveId: string | null): SectionDto | null {
  const nav = navigableTopLevel(sections);
  const top = topLevelOf(sections, liveId);
  if (!top) return nav[0] ?? null;
  const all = sortSections(sections);
  const after = all.filter((s) => s.position > top.position);
  return nav.find((s) => after.includes(s)) ?? null;
}

export function prevSection(sections: SectionDto[], liveId: string | null): SectionDto | null {
  const nav = navigableTopLevel(sections);
  const top = topLevelOf(sections, liveId);
  if (!top) return null;
  const live = sections.find((s) => s.id === liveId);
  // From a sub-point, Back returns to its parent point.
  if (live && live.parent_id && top.id !== live.id) return top;
  const before = nav.filter((s) => s.position < top.position);
  return before[before.length - 1] ?? null;
}

/** "3 · Community garden" / "Opening". */
export function sectionLabel(s: Pick<SectionDto, "number" | "title"> | null | undefined): string {
  if (!s) return "";
  return s.number ? `${s.number} · ${s.title}` : s.title;
}

/** Elapsed live seconds of a section at `now` (accumulated closed runs +
 * the current run while it is live). */
export function sectionElapsed(s: SectionDto, now: number): number {
  const base = Number.isFinite(s.elapsed_seconds) ? s.elapsed_seconds : 0;
  if (s.status !== "live") return base;
  const started = toMs(s.started_at);
  return base + (started !== null ? Math.max(0, (now - started) / 1000) : 0);
}

export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function fmtClock(iso: string | null | undefined, withSeconds = true): string {
  const t = toMs(iso);
  if (t === null) return "";
  const d = new Date(t);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (!withSeconds) return `${hh}:${mm}`;
  return `${hh}:${mm}:${String(d.getSeconds()).padStart(2, "0")}`;
}

// ── Tab content per section ───────────────────────────────────────────────

/** Where the previous actions are listed: the agenda's own actions point when
 * it has one, else the Previous actions section (FDD §5.5, v3.2). */
export function previousActionsHome(sections: SectionDto[]): SectionDto | null {
  return sections.find((s) => s.previous_actions_home) ?? sections.find((s) => s.kind === "previous_actions") ?? null;
}

/** Section a decision/action is grouped under on the board. Previous (series)
 * actions are listed under their home section (FDD §5.5). */
export function actionGroupSection(a: ActionDto, sections: SectionDto[]): string | null {
  if (a.previous) {
    const prev = previousActionsHome(sections);
    if (prev) return prev.id;
  }
  if (a.section_id && sections.some((s) => s.id === a.section_id)) return a.section_id;
  return null;
}

/** The agenda point an action is about: where it was raised, or for a
 * previous action the point it was linked to at this meeting. */
export function actionPoint(a: ActionDto): string | null {
  return a.previous ? a.topic_section_id ?? null : a.topic_section_id ?? a.section_id ?? null;
}

/** Points an item can be linked to: agenda points and their sub-points, in
 * outline order, not the fixed parts of the meeting. */
export function linkablePoints(sections: SectionDto[]): SectionDto[] {
  return outlineOrder(sections).filter(
    (s) => s.status !== "skipped" && !["opening", "previous_actions", "new_actions", "closing"].includes(s.kind) && !s.previous_actions_home,
  );
}

export function decisionGroupSection(d: DecisionDto, sections: SectionDto[]): string | null {
  if (d.section_id && sections.some((s) => s.id === d.section_id)) return d.section_id;
  return null;
}

/** Whether the tab has content for a section (FDD §5.4: outline click opens
 * Agenda when the current tab has nothing for the clicked section). */
export function tabHasContentFor(
  tab: Tab,
  sectionId: string,
  data: { sections: SectionDto[]; decisions: DecisionDto[]; actions: ActionDto[]; minutes: Array<{ section_id: string | null; notes: unknown[]; narrative_md: string | null }>; attachments: Array<{ section_id: string | null }> },
): boolean {
  switch (tab) {
    case "agenda":
      return true;
    case "decisions":
      return data.decisions.some((d) => decisionGroupSection(d, data.sections) === sectionId);
    case "actions":
      return data.actions.some((a) => actionGroupSection(a, data.sections) === sectionId);
    case "minutes":
      return data.minutes.some((m) => m.section_id === sectionId && (m.notes.length > 0 || !!m.narrative_md));
    case "papers":
      return data.attachments.some((a) => a.section_id === sectionId);
    case "attendance":
      return false;
  }
}

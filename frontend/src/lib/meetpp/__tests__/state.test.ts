import { describe, expect, it } from "vitest";
import {
  applyDelta,
  applyRefinement,
  decideStateMessage,
  findMatches,
  groupTranscript,
  mergeSegments,
  nextSection,
  outlineOrder,
  prevSection,
  sectionElapsed,
  tabHasContentFor,
} from "../state";
import { personKeyFor } from "../personKey";
import type { SectionDto, SegmentDto, Snapshot } from "../types";

function sec(id: string, position: number, extra: Partial<SectionDto> = {}): SectionDto {
  return {
    id,
    kind: "agenda",
    parent_id: null,
    position,
    number: null,
    title: id,
    body: null,
    presenter: null,
    timebox_minutes: null,
    status: "pending",
    started_at: null,
    ended_at: null,
    elapsed_seconds: 0,
    source: "user",
    locked: false,
    counts: { decisions: 0, actions: 0 },
    ...extra,
  };
}

function seg(seq: number, extra: Partial<SegmentDto> = {}): SegmentDto {
  return {
    seq,
    identity: "user-a",
    name: "Ann",
    person_key: "sub:a",
    t_start: null,
    t_end: null,
    text: `line ${seq}`,
    text_refined: null,
    tier: 1,
    is_gap: false,
    gap_reason: null,
    ...extra,
  };
}

function snapshot(): Snapshot {
  return {
    sid: "S",
    version: 5,
    session: { id: "S", status: "running", live_section_id: "a" } as Snapshot["session"],
    sections: [sec("b", 2), sec("a", 1)],
    decisions: [{ id: "d1", ref: "D-1", title: "Old" } as Snapshot["decisions"][number]],
    actions: [],
    minutes: [],
    attendees: [],
    attachments: [{ id: "x1" } as Snapshot["attachments"][number]],
    documents: [],
    quorum: null,
  };
}

describe("version rule (contract §4)", () => {
  it("merges only version == local + 1 with a delta", () => {
    expect(decideStateMessage(5, 6, true)).toBe("merge");
  });
  it("ignores versions <= local", () => {
    expect(decideStateMessage(5, 5, true)).toBe("ignore");
    expect(decideStateMessage(5, 3, true)).toBe("ignore");
  });
  it("refetches on a gap, a missing delta or no local state", () => {
    expect(decideStateMessage(5, 7, true)).toBe("refetch");
    expect(decideStateMessage(5, 6, false)).toBe("refetch");
    expect(decideStateMessage(null, 1, true)).toBe("refetch");
  });
});

describe("applyDelta", () => {
  it("merges by id, appends new rows, applies removed and session/quorum", () => {
    const next = applyDelta(
      snapshot(),
      {
        decisions: [{ id: "d1", title: "New" } as never, { id: "d2", ref: "D-2", title: "Second" } as never],
        session: { live_section_id: "b" },
        quorum: { required: 2, voting_present: 2, voting_total: 3, met: true },
        removed: [{ kind: "attachment", id: "x1" }],
      },
      6,
    );
    expect(next.version).toBe(6);
    expect(next.decisions.map((d) => [d.id, d.title, d.ref])).toEqual([
      ["d1", "New", "D-1"],
      ["d2", "Second", "D-2"],
    ]);
    expect(next.session.live_section_id).toBe("b");
    expect(next.session.status).toBe("running");
    expect(next.quorum?.met).toBe(true);
    expect(next.attachments).toHaveLength(0);
    expect(next.sections.map((s) => s.id)).toEqual(["a", "b"]);
  });
});

describe("transcript", () => {
  it("appends, inserts out-of-order lines and never downgrades refined text", () => {
    let list = mergeSegments([], [seg(1), seg(3)]);
    list = mergeSegments(list, [seg(2)]);
    expect(list.map((s) => s.seq)).toEqual([1, 2, 3]);
    list = applyRefinement(list, 2, "refined two");
    expect(list[1]).toMatchObject({ text_refined: "refined two", tier: 2 });
    list = mergeSegments(list, [seg(2, { text: "late tier-1 duplicate" })]);
    expect(list[1]).toMatchObject({ text_refined: "refined two", tier: 2 });
    expect(list).toHaveLength(3);
  });

  it("keeps the whole session (no cap)", () => {
    const many = Array.from({ length: 2500 }, (_, i) => seg(i + 1));
    const list = mergeSegments(mergeSegments([], many.slice(0, 1000)), many.slice(1000));
    expect(list).toHaveLength(2500);
  });

  it("merges consecutive same-speaker lines within 4 s; gaps split blocks", () => {
    const t = (s: number) => new Date(Date.UTC(2026, 9, 6, 14, 0, s)).toISOString();
    const blocks = groupTranscript([
      seg(1, { t_start: t(0), t_end: t(3) }),
      seg(2, { t_start: t(5), t_end: t(8) }), // 2 s after → merged
      seg(3, { t_start: t(20), t_end: t(22) }), // 12 s after → new block
      seg(4, { identity: "user-b", name: "Bob", t_start: t(23), t_end: t(24) }),
      seg(5, { is_gap: true, t_start: t(30), t_end: t(40), gap_reason: "agent reconnecting" }),
      seg(6, { identity: "user-b", name: "Bob", t_start: t(41), t_end: t(42) }),
    ]);
    expect(blocks.map((b) => [b.kind, b.segments.map((s) => s.seq)])).toEqual([
      ["speech", [1, 2]],
      ["speech", [3]],
      ["speech", [4]],
      ["gap", [5]],
      ["speech", [6]],
    ]);
  });

  it("search is case- and accent-insensitive and maps back to the original text", () => {
    const text = "Le Crédit Agricole a CRÉDITÉ";
    const ranges = findMatches(text, "credit");
    expect(ranges.map(([a, b]) => text.slice(a, b))).toEqual(["Crédit", "CRÉDIT"]);
  });
});

describe("outline navigation", () => {
  const sections = [
    sec("open", 1, { kind: "opening" }),
    sec("prev", 2, { kind: "previous_actions", status: "skipped" }),
    sec("p1", 3, { number: "1" }),
    sec("p1a", 4, { parent_id: "p1", number: "1.1" }),
    sec("p2", 5, { number: "2" }),
    sec("close", 6, { kind: "closing" }),
  ];

  it("orders sub-points under their parent", () => {
    expect(outlineOrder(sections.slice().reverse()).map((s) => s.id)).toEqual(["open", "prev", "p1", "p1a", "p2", "close"]);
  });

  it("Next passes over skipped sections and sub-points", () => {
    expect(nextSection(sections, "open")?.id).toBe("p1");
    expect(nextSection(sections, "p1")?.id).toBe("p2");
    expect(nextSection(sections, "p1a")?.id).toBe("p2");
    expect(nextSection(sections, "close")).toBeNull();
  });

  it("Back returns to the previous top-level section (or the parent of a sub-point)", () => {
    expect(prevSection(sections, "p1")?.id).toBe("open");
    expect(prevSection(sections, "p1a")?.id).toBe("p1");
    expect(prevSection(sections, "open")).toBeNull();
  });

  it("elapsed time of the live section adds the current run", () => {
    const now = Date.UTC(2026, 9, 6, 14, 10, 0);
    const live = sec("p1", 3, { status: "live", elapsed_seconds: 60, started_at: new Date(now - 30_000).toISOString() });
    expect(sectionElapsed(live, now)).toBe(90);
    expect(sectionElapsed({ ...live, status: "done" }, now)).toBe(60);
  });

  it("outline click falls back to Agenda when the current tab has nothing for the section", () => {
    const data = { sections, decisions: [{ id: "d", section_id: "p2" } as never], actions: [], minutes: [], attachments: [] };
    expect(tabHasContentFor("decisions", "p2", data)).toBe(true);
    expect(tabHasContentFor("decisions", "p1", data)).toBe(false);
    expect(tabHasContentFor("agenda", "p1", data)).toBe(true);
  });
});

describe("person_key", () => {
  it("uses sub:<sub> for signed-in identities", () => {
    expect(personKeyFor("user-abc123", null)).toBe("sub:abc123");
  });
  it("uses a per-browser guest uuid kept in sessionStorage", () => {
    const store = new Map<string, string>();
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    const first = personKeyFor("anon-01H", storage);
    expect(first).toMatch(/^guest:[0-9a-f-]{36}$/);
    expect(store.get("meetpp_guest_key")).toBe(first.slice(6));
    expect(personKeyFor("anon-other", storage)).toBe(first);
  });
});

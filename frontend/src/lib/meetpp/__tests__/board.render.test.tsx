import { describe, expect, it, vi } from "vitest";

// Browser globals used at import time by preferences / auth (node test env).
vi.hoisted(() => {
  const mem = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k), clear: () => m.clear(), key: () => null, length: 0 };
  };
  (globalThis as Record<string, unknown>).localStorage = mem();
  (globalThis as Record<string, unknown>).sessionStorage = mem();
});
vi.mock("livekit-client", () => ({ RoomEvent: {} }));
// renderToString reads zustand's *initial* state as the server snapshot; make
// bound stores read the live state instead so the board renders real data.
vi.mock("zustand", async (importOriginal) => {
  const actual = await importOriginal<typeof import("zustand")>();
  const { useSyncExternalStore } = await import("react");
  type Api = { getState: () => unknown; subscribe: (l: () => void) => () => void };
  const createStore = actual.createStore as unknown as (fn: unknown) => Api;
  const make = (fn: unknown) => {
    const api = createStore(fn);
    const hook = (sel: (s: unknown) => unknown = (x) => x) =>
      useSyncExternalStore(api.subscribe, () => sel(api.getState()), () => sel(api.getState()));
    return Object.assign(hook, api);
  };
  return { ...actual, create: (fn?: unknown) => (fn ? make(fn) : make) };
});

import { renderToString } from "react-dom/server";
import MeetppBoard from "../../../components/meetpp/MeetppBoard";
import MeetppBoardWindow, { MeetppBoardTile } from "../../../components/meetpp/MeetppStage";
import StageTileControls from "../../../components/StageTileControls";
import { StageActionsContext, StageInfoContext } from "../../stageControls";
import { BOARD_KEY } from "../../stage";
import { addSegments, applySnapshot, processActivations, setCtx, useMeetpp } from "../store";
import type { Snapshot } from "../types";

const now = new Date().toISOString();

function richSnapshot(): Snapshot {
  const sec = (id: string, position: number, extra: Record<string, unknown> = {}) => ({
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
    source: "pdf",
    locked: false,
    counts: { decisions: 0, actions: 0 },
    ...extra,
  });
  return {
    sid: "S",
    version: 3,
    session: {
      id: "S",
      status: "running",
      template: "agenda",
      mode: "lead",
      language: "en",
      goal: null,
      meeting_type: "board",
      majority_rule: "ordinary",
      series_id: "SER",
      series_title: "Founders meeting",
      meeting_id: "M",
      room: "r",
      live_section_id: "p3",
      topic_section_id: "p3",
      started_at: now,
      ended_at: null,
      published_at: null,
      settings: { show_public: false, in_recordings: true, speak: true },
      editors: [],
      undo: { from: "p2", to: "p3", until: new Date(Date.now() + 8000).toISOString() },
      proposal: { pid: "P", to: "p4", reason: "Lawyers mentioned", confidence: 0.7 },
      agent: { status: "listening", backlog_s: 0, speakers: [{ name: "Noam", ok: true }, { name: "Stephane", ok: false }], tier2: "down" },
      ai: { status: "ok" },
      jobs: null,
    },
    sections: [
      sec("open", 1, { kind: "opening", title: "Opening", status: "done" }),
      sec("prev", 2, { kind: "previous_actions", title: "Previous actions", status: "done" }),
      sec("p1", 3, { number: "1", title: "Approve last minutes", status: "done", counts: { decisions: 1, actions: 0 } }),
      sec("p2", 4, { number: "2", title: "Platform restore", status: "done" }),
      sec("p3", 5, { number: "3", title: "Community garden", status: "live", started_at: now, timebox_minutes: 15, body: "Release planning", presenter: "Noam" }),
      sec("p3a", 6, { number: "3.1", parent_id: "p3", title: "Acknowledgment", status: "done" }),
      sec("p3b", 7, { number: "3.2", parent_id: "p3", title: "Areas to improve" }),
      sec("p4", 8, { number: "4", title: "Lawyers" }),
      sec("aob", 9, { kind: "aob", title: "Any other business", status: "skipped" }),
      sec("close", 10, { kind: "closing", title: "Closing" }),
    ] as Snapshot["sections"],
    decisions: [
      {
        id: "d1", ref: "D-1", section_id: "p1", title: "Minutes approved", resolution: "that the minutes are approved", how_taken: "By assent", status: "adopted",
        decided_at: now, origin: "ai", confirmed: false, locked: false, evidence: [2], previous: false,
        vote: { method: "assent", for: 2, against: 0, abstain: 0, eligible: 3, present: 2, quorum_required: 2, quorum_met: true, result: "adopted", outcome_note: null, confirmed: false, ballots: [] },
      },
      { id: "d2", ref: "D-2", section_id: "p3", title: "Release without sub-category fields?", resolution: "that the release is not held back", how_taken: null, status: "pending", decided_at: null, origin: "pdf", confirmed: false, locked: false, evidence: [], previous: false, vote: null },
      { id: "d0", ref: "D-0", section_id: null, title: "Old", resolution: null, how_taken: null, status: "adopted", decided_at: null, origin: "ai", confirmed: true, locked: false, evidence: [], previous: true, vote: null },
    ],
    actions: [
      { id: "a1", ref: "A-13", section_id: "x", title: "Test plan", description: null, assignees: [{ name: "Stephane", person_key: null }], due: "2026-10-20", status: "open", decision_ref: null, completed_at: null, completion_note: null, progress_notes: null, origin: "pdf", locked: false, evidence: [], previous: true, carried_forward: false, report: null },
      { id: "a2", ref: "A-14", section_id: "p3", title: "Clear test documents", description: "Library refuses duplicates", assignees: [], due: null, status: "proposed", decision_ref: "D-1", completed_at: null, completion_note: null, progress_notes: null, origin: "ai", locked: false, evidence: [3], previous: false, carried_forward: false, report: null },
    ],
    minutes: [
      { id: "m1", kind: "section", section_id: "p1", notes: [{ text: "Minutes reviewed", evidence: [1], at: now }], narrative_md: "The minutes were approved.\n\n> **RESOLVED:** that the minutes are approved.", version: 2, status: "composed", source_tier: "refined", composed_at: now, locked: false, error: null },
      { id: "m3", kind: "section", section_id: "p3", notes: [{ text: "One more week of testing", evidence: [3], at: now }], narrative_md: null, version: 0, status: "notes", source_tier: null, composed_at: null, locked: false, error: null },
      { id: "m2", kind: "section", section_id: "p2", notes: [], narrative_md: null, version: 1, status: "failed", source_tier: "live", composed_at: null, locked: false, error: "LLM timeout" },
    ],
    attendees: [
      { id: "at1", person_key: "sub:n", name: "Noam", username: "noam", email: null, status: "present", online: true, voting: true, represented_by: null, mandate_ref: null, opted_out: false, required_next: false, required_reason: null, talk_seconds: 10 },
      { id: "at2", person_key: "name:david", name: "David", username: null, email: null, status: "not_registered", online: false, voting: true, represented_by: null, mandate_ref: null, opted_out: false, required_next: true, required_reason: "lawyer", talk_seconds: 0 },
    ],
    attachments: [{ id: "x1", section_id: "p3", kind: "whiteboard", filename: "wb.png", caption: "Whiteboard", author: "Noam", created_at: now, url: "/api/v1/meetpp/sessions/S/attachments/x1" }],
    documents: [{ id: "doc1", kind: "agenda", filename: "agenda.pdf", title: "Agenda", status: "done", error: null, page_count: 3, summary: null }],
    quorum: { required: 2, voting_present: 1, voting_total: 2, met: false },
  };
}

function load(isChair: boolean) {
  setCtx({ mode: "participant", isChair, localIdentity: "user-n", localName: "Noam", roomName: "r", meetingId: "M" });
  applySnapshot(richSnapshot());
  addSegments(
    [
      { seq: 1, identity: "user-n", name: "Noam", person_key: "sub:n", t_start: now, t_end: now, text: "Since the fields can be added", text_refined: "Since the fields can be added from time to time", tier: 2, is_gap: false, gap_reason: null },
      { seq: 2, identity: "user-s", name: "Stephane", person_key: "sub:s", t_start: now, t_end: now, text: "Agreed.", text_refined: null, tier: 1, is_gap: false, gap_reason: null },
      { seq: 3, identity: null, name: null, person_key: null, t_start: now, t_end: now, text: "", text_refined: null, tier: 1, is_gap: true, gap_reason: "agent reconnecting" },
    ],
    true,
  );
}

const TABS = ["agenda", "decisions", "actions", "minutes", "attendance", "papers"] as const;

// useLayoutEffect is expected to be a no-op in this server render.
const origError = console.error;
vi.spyOn(console, "error").mockImplementation((msg?: unknown, ...rest: unknown[]) => {
  if (String(msg).includes("useLayoutEffect does nothing on the server")) return;
  origError(msg, ...rest);
});

describe("Meet++ board renders (SSR smoke test)", () => {
  for (const variant of ["stage", "drawer", "egress", "public", "preview"] as const) {
    for (const tab of TABS) {
      it(`${variant} · ${tab}`, () => {
        load(variant === "stage" || variant === "drawer");
        useMeetpp.setState({ tab, viewedSectionId: "p1", expanded: { [`${tab}:p3`]: true, [`${tab}:prev`]: true } });
        const html = renderToString(<MeetppBoard variant={variant} transcriptWidth={variant === "egress" ? 260 : undefined} />);
        expect(html).toContain("Meet++");
        expect(html.length).toBeGreaterThan(1000);
      });
    }
  }

  it("shows chair controls, proposal and undo for the chair only", () => {
    load(true);
    const chair = renderToString(<MeetppBoard variant="stage" />);
    expect(chair).toContain("btn-meetpp-next");
    load(false);
    const participant = renderToString(<MeetppBoard variant="stage" />);
    expect(participant).not.toContain("btn-meetpp-next");
  });

  it("renders badges after an activation", () => {
    load(true);
    processActivations([{ kind: "decision", tab: "decisions", section_id: "p3", item_id: "d2", prio: 5 }], new Set(["d2"]));
    useMeetpp.setState({ tab: "decisions" });
    const html = renderToString(<MeetppBoard variant="stage" />);
    expect(useMeetpp.getState().badges.d2).toBe("updated");
    expect(html).toContain("D-2");
  });
});

describe("Meet++ board as a stream window", () => {
  it("is the full board as the main stream, a compact summary as a thumbnail", () => {
    load(true);
    const main = renderToString(<MeetppBoardWindow size="main" />);
    expect(main).toContain('data-testid="meetpp-board"');
    const thumb = renderToString(<MeetppBoardWindow size="thumb" />);
    expect(thumb).toContain('data-testid="meetpp-board-tile"');
    expect(thumb).not.toContain('data-testid="meetpp-board"');
    // Recordings: no stage controls on the board's summary.
    expect(renderToString(<MeetppBoardTile controls={false} />)).not.toContain("tile-zoom-");
  });

  it("offers Present to the host and co-hosts only, and marks the presenter", () => {
    const actions = { canPresent: true, present: async () => undefined };
    const host = renderToString(
      <StageActionsContext.Provider value={actions}>
        <StageInfoContext.Provider value={{ presenter: "user-a", mainKey: "user-a" }}>
          <StageTileControls tileKey={BOARD_KEY} stageKey={BOARD_KEY} />
        </StageInfoContext.Provider>
      </StageActionsContext.Provider>,
    );
    expect(host).toContain(`tile-present-${BOARD_KEY}`);
    expect(host).toContain(`tile-zoom-${BOARD_KEY}`);
    const viewer = renderToString(<StageTileControls tileKey={BOARD_KEY} stageKey={BOARD_KEY} />);
    expect(viewer).not.toContain("tile-present-");
    expect(viewer).toContain(`tile-zoom-${BOARD_KEY}`);
    const presenting = renderToString(
      <StageActionsContext.Provider value={actions}>
        <StageInfoContext.Provider value={{ presenter: BOARD_KEY, mainKey: BOARD_KEY }}>
          <StageTileControls tileKey={BOARD_KEY} stageKey={BOARD_KEY} />
        </StageInfoContext.Provider>
      </StageActionsContext.Provider>,
    );
    expect(presenting).toContain("Presenter");
    expect(presenting).not.toContain("tile-present-");
  });
});

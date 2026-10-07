import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("livekit-client", () => ({ RoomEvent: {} }));

const getState = vi.fn();
const getTranscript = vi.fn();
const consent = vi.fn();
vi.mock("../api", () => ({
  meetppApi: {
    getState: (...a: unknown[]) => getState(...a),
    getTranscript: (...a: unknown[]) => getTranscript(...a),
    consent: (...a: unknown[]) => consent(...a),
    roomActive: vi.fn(async () => ({ active: true, sid: "S" })),
  },
  setMeetppRoomToken: vi.fn(),
}));

import { handleMessage, loadSession, postConsent, chooseConsent, resetSessionLayer } from "../session";
import { configureFocus, setCtx, useMeetpp } from "../store";
import type { Snapshot } from "../types";

function snap(version: number, extra: Partial<Snapshot> = {}): Snapshot {
  return {
    sid: "S",
    version,
    session: {
      id: "S",
      status: "running",
      live_section_id: "s1",
      settings: {},
      editors: [],
      proposal: null,
      undo: null,
      agent: null,
    } as unknown as Snapshot["session"],
    sections: [
      { id: "s1", position: 1, kind: "agenda", parent_id: null, number: "1", title: "One", status: "live" } as Snapshot["sections"][number],
      { id: "s2", position: 2, kind: "agenda", parent_id: null, number: "2", title: "Two", status: "pending" } as Snapshot["sections"][number],
    ],
    decisions: [],
    actions: [],
    minutes: [],
    attendees: [],
    attachments: [],
    documents: [],
    quorum: null,
    ...extra,
  };
}

const flush = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

describe("meet-ai handling", () => {
  beforeEach(async () => {
    vi.useFakeTimers();
    const mem = new Map<string, string>();
    vi.stubGlobal("sessionStorage", {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => void mem.set(k, v),
      removeItem: (k: string) => void mem.delete(k),
    });
    getState.mockReset();
    getTranscript.mockReset();
    consent.mockReset();
    getTranscript.mockResolvedValue({ segments: [], next_after: null });
    resetSessionLayer();
    configureFocus(false);
    setCtx({ mode: "participant", isChair: true, localIdentity: "user-abc", localName: "Ann", roomName: "r" });
    getState.mockResolvedValueOnce(snap(10));
    await loadSession("S");
    await flush();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("merges version local+1, ignores stale versions", () => {
    handleMessage({ v: 1, type: "state", version: 11, delta: { decisions: [{ id: "d1", ref: "D-1", title: "First", section_id: "s1" } as never] }, activations: [] });
    expect(useMeetpp.getState().snap?.version).toBe(11);
    expect(useMeetpp.getState().snap?.decisions).toHaveLength(1);
    handleMessage({ v: 1, type: "state", version: 11, delta: { decisions: [{ id: "d9", ref: "D-9", title: "Dup" } as never] }, activations: [] });
    expect(useMeetpp.getState().snap?.decisions).toHaveLength(1);
    expect(getState).toHaveBeenCalledTimes(1);
  });

  it("refetches on a version gap and applies its activations after the refetch", async () => {
    getState.mockResolvedValueOnce(snap(13, { decisions: [{ id: "d2", ref: "D-2", title: "Gap", section_id: "s2" } as never] }));
    handleMessage({
      v: 1,
      type: "state",
      version: 13,
      delta: { decisions: [{ id: "d2", ref: "D-2", title: "Gap", section_id: "s2" } as never] },
      activations: [{ kind: "decision", tab: "decisions", section_id: "s2", item_id: "d2", prio: 5 }],
    });
    expect(useMeetpp.getState().snap?.version).toBe(10);
    await flush();
    const s = useMeetpp.getState();
    expect(s.snap?.version).toBe(13);
    expect(s.badges.d2).toBe("new");
    // A person's board stays on its tab: the Decisions tab counts and flashes,
    // and the decision is highlighted where the Agenda lists it.
    expect(s.tab).toBe("agenda");
    expect(s.viewedSectionId).toBeNull();
    expect(s.unseen.decisions).toBe(1);
    expect(s.flash?.tab).toBe("decisions");
    expect(s.highlight).toMatchObject({ tab: "agenda", items: ["d2"] });
  });

  it("the recording view still switches to the tab of a change", () => {
    configureFocus(true);
    handleMessage({ v: 1, type: "state", version: 11, delta: { decisions: [{ id: "d1", ref: "D-1", title: "A", section_id: "s2" } as never] }, activations: [{ kind: "decision", tab: "decisions", section_id: "s2", item_id: "d1", prio: 5 }] });
    const s = useMeetpp.getState();
    expect(s.tab).toBe("decisions");
    expect(s.viewedSectionId).toBe("s2");
    expect(s.highlight?.items).toEqual(["d1"]);
    configureFocus(false);
  });

  it("marks NEW vs UPDATED badges and keeps them for activations dropped by a pause", () => {
    handleMessage({ v: 1, type: "state", version: 11, delta: { decisions: [{ id: "d1", ref: "D-1", title: "A", section_id: "s1" } as never] }, activations: [{ kind: "decision", tab: "decisions", section_id: "s1", item_id: "d1", prio: 5 }] });
    expect(useMeetpp.getState().badges.d1).toBe("new");
    useMeetpp.setState({ badges: {} });
    handleMessage({ v: 1, type: "state", version: 12, delta: { decisions: [{ id: "d1", status: "adopted" } as never] }, activations: [{ kind: "decision", tab: "decisions", section_id: "s1", item_id: "d1", prio: 5 }] });
    expect(useMeetpp.getState().badges.d1).toBe("updated");
  });

  it("a delta does not clear the proposal; only an explicit proposal:null or a move does", () => {
    handleMessage({ v: 1, type: "proposal", pid: "p1", to_section_id: "s2", title: "2 · Two", reason: "talking about two", confidence: 0.7 });
    expect(useMeetpp.getState().proposal?.pid).toBe("p1");
    handleMessage({ v: 1, type: "state", version: 11, delta: { minutes: [{ id: "m1", section_id: "s1", notes: [{ text: "n", evidence: [1], at: null }] } as never] }, activations: [] });
    expect(useMeetpp.getState().proposal?.pid).toBe("p1");
    expect(useMeetpp.getState().unseen.minutes).toBe(1);
    handleMessage({ v: 1, type: "position", version: 12, live_section_id: "s2", prev_section_id: "s1", by: "chair", undo_until: null });
    expect(useMeetpp.getState().proposal).toBeNull();
    expect(useMeetpp.getState().snap?.session.live_section_id).toBe("s2");
  });

  it("an AI move offers Undo until undo_until", () => {
    const until = new Date(Date.now() + 10_000).toISOString();
    handleMessage({ v: 1, type: "position", version: 11, live_section_id: "s2", prev_section_id: "s1", by: "ai", undo_until: until });
    expect(useMeetpp.getState().undo?.to).toBe("s2");
  });

  it("captions append, tier-2 updates replace in place, gaps add marker lines", () => {
    handleMessage({ v: 1, type: "caption", seq: 5, identity: "user-a", name: "Ann", person_key: "sub:a", t_start: null, text: "helo", tier: 1 });
    handleMessage({ v: 1, type: "caption-update", seq: 5, text: "hello", tier: 2 });
    handleMessage({ v: 1, type: "gap", seq: 6, t_from: null, t_to: null, reason: "agent reconnecting" });
    const tr = useMeetpp.getState().transcript;
    expect(tr.map((s) => [s.seq, s.text_refined ?? s.text, s.tier, s.is_gap])).toEqual([
      [5, "hello", 2, false],
      [6, "", 1, true],
    ]);
  });

  it("re-posts the stored consent with the person_key on every (re)connect", async () => {
    consent.mockResolvedValue({ ok: true });
    await chooseConsent("accept");
    expect(consent).toHaveBeenLastCalledWith("S", { decision: "accept", person_key: "sub:abc", name: "Ann" });
    await postConsent();
    await postConsent();
    expect(consent).toHaveBeenCalledTimes(3);
  });
});

describe("meet-ai message validation", () => {
  const enc = (o: unknown) => new TextEncoder().encode(JSON.stringify(o));
  it("accepts well-formed messages and drops malformed ones", async () => {
    const { decode } = await import("../session");
    expect(decode(enc({ v: 1, type: "caption", seq: 3, identity: "user-1", name: "A", text: "Hello" }))).not.toBeNull();
    expect(decode(enc({ v: 1, type: "state", version: 4, delta: {} }))?.type).toBe("state");
    // A caption whose text is not a string used to throw in render.
    expect(decode(enc({ v: 1, type: "caption", seq: 1e9, identity: "x", text: {} }))).toBeNull();
    expect(decode(enc({ v: 1, type: "position", version: "9" }))).toBeNull();
    expect(decode(enc({ v: 1, type: "unknown" }))).toBeNull();
    expect(decode(new TextEncoder().encode("not json"))).toBeNull();
  });
});

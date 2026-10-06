import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FocusQueue, MAX_QUEUE, MIN_DISPLAY_MS, PAUSE_MS, type FocusActivation, type FocusEntry, type FocusSnapshot } from "../focusQueue";

const T0 = Date.UTC(2026, 9, 6, 14, 0, 0);

function decision(section: string, item: string): FocusActivation {
  return { kind: "decision", tab: "decisions", sectionId: section, itemId: item, prio: 5 };
}
function action(section: string, item: string): FocusActivation {
  return { kind: "action", tab: "actions", sectionId: section, itemId: item, prio: 4 };
}
function topic(section: string): FocusActivation {
  return { kind: "topic", tab: "agenda", sectionId: section, prio: 3 };
}
function attendance(section: string, item: string): FocusActivation {
  return { kind: "attendance", tab: "attendance", sectionId: section, itemId: item, prio: 2 };
}

function setup(alwaysFollow = false) {
  const shown: Array<{ entry: FocusEntry; at: number }> = [];
  let last: FocusSnapshot | null = null;
  const q = new FocusQueue({
    show: (entry, at) => shown.push({ entry, at: at - T0 }),
    onChange: (s) => (last = s),
    alwaysFollow,
  });
  q.start();
  return { q, shown, snap: () => last ?? q.snapshot() };
}

/** Advance fake time to `ms` after T0. */
function at(ms: number) {
  vi.advanceTimersByTime(T0 + ms - Date.now());
}

describe("FocusQueue (FDD §5.7 / Appendix B)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows an activation immediately when idle", () => {
    const { q, shown } = setup();
    q.push(decision("s3", "D-2"));
    expect(shown).toHaveLength(1);
    expect(shown[0].entry).toMatchObject({ tab: "decisions", sectionId: "s3", items: ["D-2"] });
    q.stop();
  });

  it("keeps each activation on screen for at least 5.0 s", () => {
    const { q, shown } = setup();
    q.push(decision("s3", "D-2"));
    at(1000);
    q.push(action("s4", "A-1"));
    at(MIN_DISPLAY_MS - 1);
    expect(shown).toHaveLength(1);
    at(MIN_DISPLAY_MS + 250);
    expect(shown).toHaveLength(2);
    expect(shown[1].at - shown[0].at).toBeGreaterThanOrEqual(MIN_DISPLAY_MS);
    q.stop();
  });

  it("releases the highlight after the display time when nothing waits", () => {
    const { q, snap } = setup();
    q.push(decision("s3", "D-2"));
    expect(snap().current?.items).toEqual(["D-2"]);
    at(MIN_DISPLAY_MS + 250);
    expect(snap().current).toBeNull();
    q.stop();
  });

  it("merges waiting activations for the same tab and section (all items, max prio)", () => {
    const { q, shown } = setup();
    q.push(decision("s3", "D-2"));
    at(2000);
    q.push(action("s3", "A-7"));
    at(4500);
    q.push(action("s3", "A-8"));
    expect(q.snapshot().queue).toHaveLength(1);
    at(MIN_DISPLAY_MS + 250);
    expect(shown[1].entry).toMatchObject({ tab: "actions", sectionId: "s3", items: ["A-7", "A-8"] });
    q.stop();
  });

  it("does not merge into the activation currently on screen", () => {
    const { q } = setup();
    q.push(action("s3", "A-7"));
    q.push(action("s3", "A-8"));
    expect(q.snapshot().current?.items).toEqual(["A-7"]);
    expect(q.snapshot().queue.map((e) => e.items)).toEqual([["A-8"]]);
    q.stop();
  });

  it("a newer topic change replaces an older waiting one", () => {
    const { q } = setup();
    q.push(decision("s3", "D-2"));
    q.push(topic("s4"));
    q.push(action("s3", "A-1"));
    q.push(topic("s5"));
    const queue = q.snapshot().queue;
    expect(queue.filter((e) => e.kind === "topic").map((e) => e.sectionId)).toEqual(["s5"]);
    expect(queue.map((e) => e.kind)).toEqual(["action", "topic"]);
    q.stop();
  });

  it(`holds at most ${MAX_QUEUE} waiting and drops the lowest-priority, oldest one`, () => {
    const { q } = setup();
    q.push(decision("s0", "D-0")); // on screen
    q.push(attendance("a", "P-1")); // prio 2, oldest
    q.push(action("b", "A-1"));
    q.push(attendance("c", "P-2")); // prio 2, newer
    q.push(decision("d", "D-1"));
    expect(q.snapshot().queue).toHaveLength(4);
    q.push(action("e", "A-2"));
    const queue = q.snapshot().queue;
    expect(queue).toHaveLength(MAX_QUEUE);
    expect(queue.map((e) => e.sectionId)).toEqual(["b", "c", "d", "e"]);
    q.stop();
  });

  it("FIFO order for waiting activations", () => {
    const { q, shown } = setup();
    q.push(decision("s1", "D-1"));
    q.push(action("s2", "A-1"));
    q.push(attendance("s3", "P-1"));
    at(MIN_DISPLAY_MS);
    at(2 * MIN_DISPLAY_MS + 250);
    expect(shown.map((s) => s.entry.sectionId)).toEqual(["s1", "s2", "s3"]);
    q.stop();
  });

  it("pauses for 10 s after the LAST manual interaction, with a countdown", () => {
    const { q, shown, snap } = setup();
    at(1000);
    q.interact();
    expect(snap().mode).toBe("paused");
    expect(snap().pausedUntil).toBe(T0 + 1000 + PAUSE_MS);
    q.push(decision("s3", "D-3"));
    expect(shown).toHaveLength(0);
    at(7000);
    q.interact(); // extends the pause
    at(1000 + PAUSE_MS + 500);
    expect(shown).toHaveLength(0);
    expect(snap().mode).toBe("paused");
    at(7000 + PAUSE_MS + 250);
    expect(shown).toHaveLength(1);
    expect(snap().mode).toBe("following");
    q.stop();
  });

  it("catch-up after a pause shows only the newest waiting activation", () => {
    const { q, shown, snap } = setup();
    q.interact();
    q.push(decision("s3", "D-3"));
    q.push(action("s3", "A-9"));
    q.push(topic("s4"));
    at(PAUSE_MS + 250);
    expect(shown).toHaveLength(1);
    expect(shown[0].entry).toMatchObject({ kind: "topic", sectionId: "s4" });
    expect(snap().queue).toHaveLength(0);
    q.stop();
  });

  it("Resume now ends the pause immediately (catch-up)", () => {
    const { q, shown } = setup();
    q.interact();
    q.push(decision("s3", "D-3"));
    q.push(action("s5", "A-1"));
    at(2000);
    q.resume();
    expect(shown).toHaveLength(1);
    expect(shown[0].entry.sectionId).toBe("s5");
    expect(shown[0].at).toBe(2000);
    q.stop();
  });

  it("an interaction during a display does not cut the minimum display of the next", () => {
    const { q, shown } = setup();
    q.push(decision("s1", "D-1"));
    at(1000);
    q.push(action("s2", "A-1"));
    q.interact();
    at(1000 + PAUSE_MS + 250);
    expect(shown).toHaveLength(2);
    expect(shown[1].entry.sectionId).toBe("s2");
    q.stop();
  });

  it("programmatic shows never count as interactions (no self-pause)", () => {
    const { q, snap } = setup();
    q.push(decision("s1", "D-1"));
    q.push(action("s2", "A-1"));
    at(MIN_DISPLAY_MS + 250);
    expect(snap().mode).toBe("following");
    expect(snap().pausedUntil).toBe(0);
    q.stop();
  });

  it("Follow off: activations are not queued or shown until Follow is on again", () => {
    const { q, shown, snap } = setup();
    q.setFollow(false);
    expect(snap().mode).toBe("off");
    q.push(decision("s1", "D-1"));
    at(20000);
    expect(shown).toHaveLength(0);
    expect(snap().queue).toHaveLength(0);
    q.setFollow(true);
    expect(snap().mode).toBe("following");
    q.push(action("s2", "A-1"));
    expect(shown).toHaveLength(1);
    q.stop();
  });

  it("switching Follow off clears waiting activations", () => {
    const { q } = setup();
    q.push(decision("s1", "D-1"));
    q.push(action("s2", "A-1"));
    q.setFollow(false);
    expect(q.snapshot().queue).toHaveLength(0);
    expect(q.snapshot().current).toBeNull();
    q.stop();
  });

  it("egress always follows: no pause, cannot be switched off, same 5 s rule", () => {
    const { q, shown, snap } = setup(true);
    q.interact();
    q.setFollow(false);
    expect(snap().mode).toBe("following");
    q.push(decision("s1", "D-1"));
    q.push(action("s2", "A-1"));
    expect(shown).toHaveLength(1);
    at(MIN_DISPLAY_MS + 250);
    expect(shown).toHaveLength(2);
    q.stop();
  });

  it("replays the FDD Figure 3 timeline", () => {
    const { q, shown, snap } = setup();
    const events: Array<[number, () => void]> = [
      [2000, () => q.push(decision("s3", "D-2"))],
      [4000, () => q.push(action("s3", "A-7"))],
      [6500, () => q.push(action("s3", "A-8"))],
      [14000, () => q.interact()], // user clicks the Agenda tab
      [17000, () => q.push(decision("s3", "D-3"))],
      [19000, () => q.push(topic("s4"))],
      [33000, () => q.push(action("s4", "A-9"))],
    ];
    const modes: Record<number, string> = {};
    for (const [ms, fn] of events) {
      at(ms);
      fn();
      modes[ms] = snap().mode;
    }
    at(40000);
    expect(shown.map((s) => [s.at, s.entry.tab, s.entry.sectionId, s.entry.items])).toEqual([
      [2000, "decisions", "s3", ["D-2"]],
      [7000, "actions", "s3", ["A-7", "A-8"]],
      [24000, "agenda", "s4", []],
      [33000, "actions", "s4", ["A-9"]],
    ]);
    expect(modes[17000]).toBe("paused");
    expect(modes[33000]).toBe("following");
    q.stop();
  });
});

describe("FocusQueue return to the earlier view", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function withHome() {
    let view = "agenda:live";
    const restored: Array<{ view: unknown; at: number }> = [];
    const shown: FocusEntry[] = [];
    const q = new FocusQueue({
      show: (entry) => {
        shown.push(entry);
        view = `${entry.tab}:${entry.sectionId}`;
      },
      capture: () => view,
      restore: (v) => {
        restored.push({ view: v, at: Date.now() - T0 });
        view = String(v);
      },
    });
    q.start();
    return { q, shown, restored, view: () => view };
  }

  it("shows a new decision for 5 s, then goes back to where the user was", () => {
    const { q, restored, view } = withHome();
    q.push(decision("p2", "d1"));
    expect(view()).toBe("decisions:p2");
    at(MIN_DISPLAY_MS - 250);
    expect(restored).toHaveLength(0);
    at(MIN_DISPLAY_MS + 250);
    expect(restored).toEqual([{ view: "agenda:live", at: MIN_DISPLAY_MS }]);
    expect(view()).toBe("agenda:live");
  });

  it("returns once after a burst of items, to the view before the first one", () => {
    const { q, shown, restored } = withHome();
    q.push(decision("p2", "d1"));
    q.push(action("p2", "a1"));
    at(MIN_DISPLAY_MS + 250);
    expect(shown.map((e) => e.tab)).toEqual(["decisions", "actions"]);
    expect(restored).toHaveLength(0);
    at(2 * MIN_DISPLAY_MS + 500);
    expect(restored.map((r) => r.view)).toEqual(["agenda:live"]);
  });

  it("does not return after a topic change or a manual click", () => {
    const a = withHome();
    a.q.push(decision("p2", "d1"));
    a.q.push(topic("p3"));
    at(2 * MIN_DISPLAY_MS + 500);
    expect(a.restored).toHaveLength(0);
    a.q.stop();

    vi.setSystemTime(T0);
    const b = withHome();
    b.q.push(action("p2", "a1"));
    b.q.interact();
    at(PAUSE_MS + 1000);
    expect(b.restored).toHaveLength(0);
  });
});

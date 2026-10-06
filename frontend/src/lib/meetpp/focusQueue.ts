/**
 * Automatic activation queue — FDD v3.1 §5.7 and Appendix B.
 *
 * Pure (no React, no DOM): time comes from `now()` and the 250 ms pump runs on
 * setInterval, so the rules are unit-testable with fake timers.
 *
 *  - Minimum display: every shown activation stays ≥ MIN_DISPLAY_MS before the
 *    next one is applied, even when newer ones are waiting.
 *  - Merging: an activation for the same tab + section as a WAITING one is
 *    merged into it (items accumulate, prio = max). A newer topic activation
 *    replaces an older waiting topic.
 *  - Queue: FIFO, at most MAX_QUEUE waiting; on overflow the lowest-priority,
 *    oldest entry is dropped (its items keep their badges — the caller marks
 *    badges/unseen before calling push()).
 *  - Manual pause: interact() pauses automatic activation for PAUSE_MS after
 *    the LAST interaction. Programmatic scrolls never call interact().
 *  - Catch-up: the first show after a pause takes only the newest waiting
 *    activation and clears the rest.
 *  - Return: an item activation (decision, action, attendance) is shown for
 *    MIN_DISPLAY_MS and then the view the user was on before it comes back
 *    (captured when the first item of a burst is shown). A topic activation or
 *    a manual interaction during the burst cancels the return.
 *  - Follow off: activations are not queued at all until Follow is on again.
 *  - Egress (alwaysFollow): never pauses, cannot be switched off.
 */

import type { ActivationKind, Tab } from "./types";

export const MIN_DISPLAY_MS = 5000;
export const PAUSE_MS = 10000;
export const MAX_QUEUE = 4;
export const PUMP_INTERVAL_MS = 250;

export interface FocusActivation {
  kind: ActivationKind;
  tab: Tab;
  sectionId: string | null;
  itemId?: string | null;
  prio: number;
}

export interface FocusEntry {
  kind: ActivationKind;
  tab: Tab;
  sectionId: string | null;
  items: string[];
  prio: number;
  /** Arrival order (monotonic), used for FIFO and "oldest" on overflow. */
  order: number;
}

export type FollowMode = "following" | "paused" | "off";

export interface FocusSnapshot {
  mode: FollowMode;
  pausedUntil: number;
  current: FocusEntry | null;
  shownAt: number;
  queue: FocusEntry[];
}

export interface FocusQueueOptions {
  /** Apply an activation (programmatic: set tab, expand + scroll, highlight). */
  show: (entry: FocusEntry, at: number) => void;
  /** The view to come back to after item activations (tab, section, …). */
  capture?: () => unknown;
  /** Go back to a view returned by capture(). */
  restore?: (view: unknown) => void;
  /** Called whenever the follow mode, the current entry or the queue changes. */
  onChange?: (snap: FocusSnapshot) => void;
  now?: () => number;
  /** Egress / recording: always follows, never pauses. */
  alwaysFollow?: boolean;
  minDisplayMs?: number;
  pauseMs?: number;
  maxQueue?: number;
}

export class FocusQueue {
  private readonly opts: FocusQueueOptions;
  private readonly now: () => number;
  private readonly minDisplay: number;
  private readonly pauseMs: number;
  private readonly maxQueue: number;
  private follow = true;
  private pausedUntil = 0;
  private pausedSinceShow = false;
  private current: FocusEntry | null = null;
  private shownAt = 0;
  private queue: FocusEntry[] = [];
  private order = 0;
  /** View before the current burst of item activations (null: no return). */
  private home: { view: unknown } | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastMode: FollowMode = "following";

  constructor(opts: FocusQueueOptions) {
    this.opts = opts;
    this.now = opts.now ?? (() => Date.now());
    this.minDisplay = opts.minDisplayMs ?? MIN_DISPLAY_MS;
    this.pauseMs = opts.pauseMs ?? PAUSE_MS;
    this.maxQueue = opts.maxQueue ?? MAX_QUEUE;
  }

  /** Start the 250 ms pump timer (idempotent). */
  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => this.pump(), PUMP_INTERVAL_MS);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  /** Drop everything (new session, unmount). Follow preference is kept. */
  reset(): void {
    this.queue = [];
    this.current = null;
    this.home = null;
    this.shownAt = 0;
    this.pausedUntil = 0;
    this.pausedSinceShow = false;
    this.emit();
  }

  get alwaysFollow(): boolean {
    return !!this.opts.alwaysFollow;
  }

  mode(at = this.now()): FollowMode {
    if (!this.follow) return "off";
    if (at < this.pausedUntil) return "paused";
    return "following";
  }

  snapshot(): FocusSnapshot {
    return {
      mode: this.mode(),
      pausedUntil: this.pausedUntil,
      current: this.current ? { ...this.current, items: [...this.current.items] } : null,
      shownAt: this.shownAt,
      queue: this.queue.map((q) => ({ ...q, items: [...q.items] })),
    };
  }

  /** onActivation(a) of Appendix B (badges / unseen are the caller's job). */
  push(a: FocusActivation): void {
    if (!this.follow) return;
    const item = a.itemId ?? null;
    const same = this.queue.find((q) => q.tab === a.tab && q.sectionId === a.sectionId);
    if (same) {
      if (item && !same.items.includes(item)) same.items.push(item);
      same.prio = Math.max(same.prio, a.prio);
      this.emit();
      return;
    }
    if (a.kind === "topic") this.queue = this.queue.filter((q) => q.kind !== "topic");
    this.queue.push({
      kind: a.kind,
      tab: a.tab,
      sectionId: a.sectionId,
      items: item ? [item] : [],
      prio: a.prio,
      order: ++this.order,
    });
    if (this.queue.length > this.maxQueue) this.dropLowestOldest();
    this.pump();
    this.emit();
  }

  /** onUserInteraction(): click / keyboard on tab, outline, item; wheel in content; edit. */
  interact(): void {
    if (this.opts.alwaysFollow) return;
    this.pausedUntil = this.now() + this.pauseMs;
    this.pausedSinceShow = true;
    // The user went somewhere themselves: no return to the earlier view.
    this.home = null;
    this.emit();
  }

  /** "Resume now" / "Back to live": end the pause immediately. */
  resume(): void {
    if (this.pausedUntil === 0 && this.follow) {
      this.pump();
      return;
    }
    this.pausedUntil = 0;
    this.pump();
    this.emit();
  }

  /** Follow on/off toggle. Off clears the queue; on resumes immediately. */
  setFollow(on: boolean): void {
    if (this.opts.alwaysFollow) return;
    if (on === this.follow) return;
    this.follow = on;
    this.pausedUntil = 0;
    if (!on) {
      this.queue = [];
      this.current = null;
      this.home = null;
    }
    this.emit();
    if (on) this.pump();
  }

  get following(): boolean {
    return this.follow;
  }

  /** pump() of Appendix B; also runs every 250 ms. */
  pump(): void {
    const now = this.now();
    let changed = false;
    // The display time of the shown activation is over: release it, and after
    // the last of a burst of item activations go back to the earlier view.
    if (this.current && now - this.shownAt >= this.minDisplay) {
      this.current = null;
      changed = true;
      // Anything still waiting (another item, or a topic that becomes the new
      // place to stay) is shown first.
      if (this.home && this.queue.length === 0 && now >= this.pausedUntil && this.follow) {
        const home = this.home;
        this.home = null;
        this.opts.restore?.(home.view);
      }
    }
    const mode = this.mode(now);
    if (mode !== this.lastMode) changed = true;
    if (now < this.pausedUntil || !this.follow) {
      if (changed) this.emit();
      return;
    }
    if (this.current || this.queue.length === 0) {
      if (changed) this.emit();
      return;
    }
    const catchUp = this.pausedSinceShow;
    const next = catchUp ? this.queue.pop()! : this.queue.shift()!;
    if (catchUp) this.queue = [];
    this.pausedSinceShow = false;
    if (next.kind === "topic") {
      // The meeting moved on: the topic is where the board stays.
      this.home = null;
    } else if (!this.home && this.opts.capture) {
      this.home = { view: this.opts.capture() };
    }
    this.current = next;
    this.shownAt = now;
    this.opts.show({ ...next, items: [...next.items] }, now);
    this.emit();
  }

  private dropLowestOldest(): void {
    let idx = 0;
    for (let i = 1; i < this.queue.length; i++) {
      // Strictly lower prio wins; ties keep the earliest (= oldest) index.
      if (this.queue[i].prio < this.queue[idx].prio) idx = i;
    }
    this.queue.splice(idx, 1);
  }

  private emit(): void {
    this.lastMode = this.mode();
    this.opts.onChange?.(this.snapshot());
  }
}

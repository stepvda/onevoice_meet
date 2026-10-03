import type { FocusHint, MeetppTab } from "./types";

export const DWELL_MS = 6000;
export const INTERACTION_PAUSE_MS = 30000;
export const MINUTES_PRIO = 1;

const TAB_BY_KIND: Record<string, MeetppTab> = {
  agenda: "agenda",
  decision: "decisions",
  action: "actions",
  attendance: "attendance",
  minutes: "minutes",
  attachment: "attachments",
};

export interface FollowState {
  follow: boolean;
  pausedUntil: number;
  lastSwitchAt: number;
  editing: boolean;
}

export interface FollowDecision {
  tab: MeetppTab;
  id: string | null;
}

/** Decide whether a focus hint should move the board. Pure and local: the
 * server sends a hint, each client decides whether to obey it. */
export function decideFocus(
  st: FollowState,
  hint: FocusHint | null | undefined,
  now: number,
  lastFocusAt: number | null,
): FollowDecision | null {
  if (!st.follow || !hint) return null;
  if (st.editing) return null;
  if (now < st.pausedUntil) return null;
  if (now - st.lastSwitchAt < DWELL_MS) return null;
  if (hint.prio <= MINUTES_PRIO && lastFocusAt !== null && now - lastFocusAt < 60000) {
    return null;
  }
  return { tab: hint.tab, id: hint.id };
}

export function tabForKind(kind: string): MeetppTab | null {
  return TAB_BY_KIND[kind] ?? null;
}

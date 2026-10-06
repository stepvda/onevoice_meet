import { create } from "zustand";

/**
 * This viewer's own view of the stage, on top of the room layout the host
 * sets: a layout of their choice, and one stream window zoomed to the whole
 * stage (double-click a tile, or its zoom button). Never sent anywhere and not
 * persisted: a new meeting starts on the room's layout.
 */
export type ViewLayout = "room" | "grid" | "speaker" | "single-speaker";

interface StageView {
  layout: ViewLayout;
  /** Tile key of the zoomed stream window ("<identity>-<source>" or the board key). */
  focusKey: string | null;
}

export const useStageView = create<StageView>(() => ({ layout: "room", focusKey: null }));

export function setViewLayout(layout: ViewLayout): void {
  useStageView.setState({ layout, focusKey: null });
}

export function toggleFocus(key: string): void {
  useStageView.setState((s) => ({ focusKey: s.focusKey === key ? null : key }));
}

export function setFocus(key: string | null): void {
  useStageView.setState({ focusKey: key });
}

export function resetStageView(): void {
  useStageView.setState({ layout: "room", focusKey: null });
}

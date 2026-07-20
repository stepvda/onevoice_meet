import { createContext } from "react";

/**
 * True only inside the grid-mode stage. FlippableTile reads this to decide
 * whether the `display.gridAspect` standardization (4:3 / 3:4 uniform tile
 * boxes) applies — so full-bleed, speaker-thumbnail and PiP-overlay tiles,
 * which render OUTSIDE this provider, always keep their native aspect fit and
 * are never squished by the grid-only toggle.
 *
 * Carrying just a boolean (not the aspect value) keeps the value itself in the
 * zustand store where every tile already reads it, and avoids depending on
 * LiveKit's internal cloneElement preserving custom props on GridLayout's tile
 * template.
 */
export const GridStageContext = createContext(false);

/**
 * Per-viewer "zoom one tile to full stage" state, honoured only inside grid
 * mode (and the zoomed single-tile view). FlippableTile calls `toggle(key)`
 * on double-tap / double-click; PresenterSpotlight owns the state and renders
 * the focused track full-bleed. `null` (default) means double-tap is a no-op —
 * so tiles in speaker / single-speaker / PiP layouts are unaffected.
 */
export interface GridFocus {
  focusedKey: string | null;
  toggle: (key: string) => void;
}
export const GridFocusContext = createContext<GridFocus | null>(null);

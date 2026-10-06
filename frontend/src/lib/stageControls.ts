import { createContext } from "react";

/**
 * What the room lets this viewer do with the stage. Provided by the room
 * route (it knows the meeting and the viewer's role); absent in the public
 * view, where tiles only offer the local zoom.
 */
export interface StageActions {
  /** Host or co-host: may choose the room-wide presenter. */
  canPresent: boolean;
  /** Make a stream window the presenter (a stage key, see lib/stage.ts), or clear it. */
  present: (key: string | null) => Promise<unknown>;
}

export const StageActionsContext = createContext<StageActions | null>(null);

/** The stage as composed right now, for the tile controls and badges. */
export interface StageInfo {
  presenter: string | null;
  mainKey: string | null;
}

export const StageInfoContext = createContext<StageInfo>({ presenter: null, mainKey: null });

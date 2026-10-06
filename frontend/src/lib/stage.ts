/**
 * Stage composition shared by the live room (PresenterSpotlight) and the
 * recording / livestream page (EgressLayoutPiP), so both pick the same main
 * stream.
 *
 * Every stream window on the stage has a key, which is also the value the
 * room-wide `presenter_identity` (LiveKit room metadata) holds:
 *   - a camera: the participant identity ("user-42"; the playback ingress is
 *     simply "playback");
 *   - a screen share: "<identity>#screen";
 *   - the Meet++ board: "meetpp:board".
 *
 * The host and co-hosts choose the presenter; the server makes the board the
 * presenter when a Meet++ session starts, and hands the stage to a screen share
 * or playback while it runs (restoring the previous presenter afterwards).
 */

export const BOARD_KEY = "meetpp:board";
export const PLAYBACK_IDENTITY = "playback";

export type StageKind = "camera" | "screen" | "playback" | "board";
export type RoomLayout = "single-speaker" | "speaker" | "grid";

export interface StageCandidate {
  key: string;
  identity: string;
  kind: StageKind;
}

export function streamKey(identity: string, screen: boolean): string {
  return screen ? `${identity}#screen` : identity;
}

export function candidate(identity: string, screen: boolean): StageCandidate {
  return {
    key: streamKey(identity, screen),
    identity,
    kind: screen ? "screen" : identity === PLAYBACK_IDENTITY ? "playback" : "camera",
  };
}

export const BOARD_CANDIDATE: StageCandidate = { key: BOARD_KEY, identity: BOARD_KEY, kind: "board" };

/**
 * The main stream: the presenter when it is on the stage, else a screen share,
 * the playback, the Meet++ board, the active speaker, any camera.
 */
export function pickMainKey(
  candidates: readonly StageCandidate[],
  presenter: string | null,
  activeSpeaker: string | null,
): string | null {
  if (presenter && candidates.some((c) => c.key === presenter)) return presenter;
  for (const kind of ["screen", "playback", "board"] as const) {
    const c = candidates.find((x) => x.kind === kind);
    if (c) return c.key;
  }
  if (activeSpeaker) {
    const c = candidates.find((x) => x.kind === "camera" && x.identity === activeSpeaker);
    if (c) return c.key;
  }
  return candidates.find((x) => x.kind === "camera")?.key ?? null;
}

/**
 * The room layout as rendered: a grid gives way to the speaker layout while a
 * screen is shared or while the main stream is the board or a screen share
 * (a presented camera keeps the grid, as before).
 */
export function effectiveRoomLayout(
  layout: RoomLayout,
  mainKind: StageKind | null,
  hasScreenShare: boolean,
): RoomLayout {
  if (layout === "grid" && (hasScreenShare || mainKind === "board" || mainKind === "screen")) return "speaker";
  return layout;
}

export function kindOfKey(key: string | null): StageKind | null {
  if (!key) return null;
  if (key === BOARD_KEY) return "board";
  if (key.endsWith("#screen")) return "screen";
  if (key === PLAYBACK_IDENTITY) return "playback";
  return "camera";
}

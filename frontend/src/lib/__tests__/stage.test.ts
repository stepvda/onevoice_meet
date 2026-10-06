import { describe, expect, it } from "vitest";
import {
  BOARD_CANDIDATE,
  BOARD_KEY,
  candidate,
  effectiveRoomLayout,
  kindOfKey,
  pickMainKey,
} from "../stage";

const alice = candidate("user-alice", false);
const bob = candidate("user-bob", false);
const bobScreen = candidate("user-bob", true);
const playback = candidate("playback", false);

describe("pickMainKey", () => {
  it("keys streams by identity, screen shares with #screen", () => {
    expect(bobScreen.key).toBe("user-bob#screen");
    expect(playback.kind).toBe("playback");
    expect(kindOfKey("user-bob#screen")).toBe("screen");
    expect(kindOfKey(BOARD_KEY)).toBe("board");
  });

  it("the board is presenter by default and a presented camera replaces it", () => {
    const all = [BOARD_CANDIDATE, alice, bob];
    expect(pickMainKey(all, BOARD_KEY, "user-bob")).toBe(BOARD_KEY);
    expect(pickMainKey(all, "user-alice", "user-bob")).toBe("user-alice");
  });

  it("without a presenter: screen share, playback, board, active speaker, any camera", () => {
    expect(pickMainKey([alice, BOARD_CANDIDATE, playback, bobScreen], null, null)).toBe("user-bob#screen");
    expect(pickMainKey([alice, BOARD_CANDIDATE, playback], null, null)).toBe("playback");
    expect(pickMainKey([alice, bob, BOARD_CANDIDATE], null, "user-bob")).toBe(BOARD_KEY);
    expect(pickMainKey([alice, bob], null, "user-bob")).toBe("user-bob");
    expect(pickMainKey([alice, bob], null, null)).toBe("user-alice");
    expect(pickMainKey([], null, null)).toBeNull();
  });

  it("an explicit presenter wins over a screen share; a gone presenter falls through", () => {
    expect(pickMainKey([BOARD_CANDIDATE, bobScreen], BOARD_KEY, null)).toBe(BOARD_KEY);
    expect(pickMainKey([alice, bobScreen], "user-alice", null)).toBe("user-alice");
    // Meet++ ended (no board) or the presenter left.
    expect(pickMainKey([alice, bob], BOARD_KEY, "user-bob")).toBe("user-bob");
    expect(pickMainKey([alice, bobScreen], "user-carol", null)).toBe("user-bob#screen");
  });
});

describe("effectiveRoomLayout", () => {
  it("grid gives way to speaker for a shared screen or the board on the main slot", () => {
    expect(effectiveRoomLayout("grid", "board", false)).toBe("speaker");
    expect(effectiveRoomLayout("grid", "screen", true)).toBe("speaker");
    expect(effectiveRoomLayout("grid", "camera", true)).toBe("speaker");
    expect(effectiveRoomLayout("grid", "camera", false)).toBe("grid");
    expect(effectiveRoomLayout("single-speaker", "board", false)).toBe("single-speaker");
  });
});

import { describe, expect, it } from "vitest";
import { roomFromToken } from "../livekitToken";

const b64url = (o: unknown) => btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

describe("roomFromToken", () => {
  it("reads the room grant of an egress token", () => {
    const token = `${b64url({ alg: "HS256" })}.${b64url({ sub: "EG_x", video: { roomJoin: true, room: "lemur-breeze-sunny", recorder: true } })}.sig`;
    expect(roomFromToken(token)).toBe("lemur-breeze-sunny");
  });
  it("is empty for a missing or broken token", () => {
    expect(roomFromToken("")).toBe("");
    expect(roomFromToken(null)).toBe("");
    expect(roomFromToken("not-a-jwt")).toBe("");
    expect(roomFromToken(`${b64url({})}.${b64url({ video: {} })}.s`)).toBe("");
  });
});

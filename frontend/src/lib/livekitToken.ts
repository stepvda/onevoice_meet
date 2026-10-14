/**
 * The room a LiveKit access token is for (its `video.room` grant), read
 * without verifying it — LiveKit and meeting-api do that.
 *
 * The recording / livestream page needs the room name to find the Meet++
 * session, and LiveKit only hands a custom egress template `url`, `token`
 * and `layout`: the room is in the token.
 */
export function roomFromToken(token: string | null | undefined): string {
  if (!token) return "";
  try {
    let part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    part += "=".repeat((4 - (part.length % 4)) % 4);
    const claims = JSON.parse(atob(part)) as { video?: { room?: unknown } };
    return typeof claims.video?.room === "string" ? claims.video.room : "";
  } catch {
    return "";
  }
}

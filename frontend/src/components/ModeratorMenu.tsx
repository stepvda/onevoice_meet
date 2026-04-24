import { useState } from "react";
import { useLocalParticipant, useRemoteParticipants } from "@livekit/components-react";
import { api } from "../lib/api";

interface Props {
  meetingId: string;
}

/**
 * Visible to the owner only — enforced by `localParticipant.permissions?.canPublishSources`
 * but really by the fact that the LiveKit token carries `roomAdmin`. The backend
 * does the final authorization.
 */
export default function ModeratorMenu({ meetingId }: Props) {
  const { localParticipant } = useLocalParticipant();
  const remotes = useRemoteParticipants();
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Cheap check: the owner's token sets room_admin=True. The
  // permissions.canPublishSources is one signal that maps to elevated grants.
  const isAdmin = localParticipant?.permissions?.canPublishData && remotes !== undefined;
  if (!isAdmin) return null;
  if (remotes.length === 0) return null;

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <details style={{ padding: "0.5rem", background: "#1a1a22", borderRadius: 6, marginTop: "0.5rem" }}>
      <summary style={{ cursor: "pointer" }}>Moderator ({remotes.length})</summary>
      <ul style={{ listStyle: "none", padding: 0, margin: "0.5rem 0" }}>
        {remotes.map((p) => (
          <li key={p.identity} style={{ display: "flex", gap: "0.25rem", marginBottom: "0.25rem", alignItems: "center" }}>
            <span style={{ flex: 1 }}>{p.name || p.identity}</span>
            <button
              className="btn"
              disabled={!!busy}
              onClick={() => run(`mute:${p.identity}`, () => api.mute(meetingId, { participant_identity: p.identity, mute: true }))}
            >
              Mute
            </button>
            <button
              className="btn"
              disabled={!!busy}
              onClick={() => run(`present:${p.identity}`, () => api.setPresenter(meetingId, p.identity))}
            >
              Present
            </button>
            <button
              className="btn"
              disabled={!!busy}
              onClick={() => run(`kick:${p.identity}`, () => api.kick(meetingId, p.identity))}
              style={{ background: "#b91c1c" }}
            >
              Kick
            </button>
          </li>
        ))}
      </ul>
      <button
        className="btn"
        disabled={!!busy}
        onClick={() => run("clear-presenter", () => api.setPresenter(meetingId, null))}
      >
        Back to grid view
      </button>
      {err && <div className="error">{err}</div>}
    </details>
  );
}

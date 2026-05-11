import { useState } from "react";
import { api } from "../lib/api";

interface Props {
  meetingId: string;
  recordingActive: boolean;
}

type Layout = "speaker" | "grid" | "single-speaker";

export default function OwnerControls({ meetingId, recordingActive }: Props) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [layout, setLayout] = useState<Layout>("speaker");

  async function toggle() {
    setBusy(true);
    setErr(null);
    try {
      if (recordingActive) await api.stopRecording(meetingId);
      else await api.startRecording(meetingId, { layout });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "inline-flex", gap: "0.5rem", alignItems: "center" }}>
      {!recordingActive && (
        <label style={{ display: "inline-flex", gap: "0.25rem", alignItems: "center", fontSize: "0.875rem" }}>
          Layout:
          <select
            value={layout}
            onChange={(e) => setLayout(e.target.value as Layout)}
            disabled={busy}
          >
            <option value="speaker">Speaker (spotlight + thumbnails)</option>
            <option value="grid">Grid</option>
            <option value="single-speaker">Single speaker</option>
          </select>
        </label>
      )}
      <button className="btn" disabled={busy} onClick={toggle} style={{ background: recordingActive ? "#b91c1c" : "#2563eb" }}>
        {busy ? "…" : recordingActive ? "Stop recording" : "Start recording"}
      </button>
      {err && <span className="error">{err}</span>}
    </div>
  );
}

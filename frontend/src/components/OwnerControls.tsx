import { useState } from "react";
import { api } from "../lib/api";

interface Props {
  meetingId: string;
  recordingActive: boolean;
}

export default function OwnerControls({ meetingId, recordingActive }: Props) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function toggle() {
    setBusy(true);
    setErr(null);
    try {
      if (recordingActive) await api.stopRecording(meetingId);
      else await api.startRecording(meetingId);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "inline-flex", gap: "0.5rem", alignItems: "center" }}>
      <button className="btn" disabled={busy} onClick={toggle} style={{ background: recordingActive ? "#b91c1c" : "#2563eb" }}>
        {busy ? "…" : recordingActive ? "Stop recording" : "Start recording"}
      </button>
      {err && <span className="error">{err}</span>}
    </div>
  );
}

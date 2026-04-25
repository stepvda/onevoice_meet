import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useRoomContext } from "@livekit/components-react";
import { RoomEvent } from "livekit-client";

export default function RecordingIndicator() {
  const { t } = useTranslation();
  const room = useRoomContext();
  const [active, setActive] = useState(false);

  useEffect(() => {
    const read = () => {
      try {
        const md = JSON.parse(room.metadata || "{}");
        setActive(!!md.recording_active);
      } catch {
        setActive(false);
      }
    };
    read();
    room.on(RoomEvent.RoomMetadataChanged, read);
    return () => {
      room.off(RoomEvent.RoomMetadataChanged, read);
    };
  }, [room]);

  if (!active) return null;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.3rem",
        padding: "0.2rem 0.6rem",
        background: "#7f1d1d",
        color: "white",
        borderRadius: 4,
        fontSize: "0.85em",
        fontWeight: 600,
      }}
      aria-live="polite"
    >
      <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#f87171" }} />
      {t("recordingIndicator.label")}
    </span>
  );
}

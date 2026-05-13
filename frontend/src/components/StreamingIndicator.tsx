import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useRoomContext } from "@livekit/components-react";
import { RoomEvent } from "livekit-client";

/**
 * Sibling of `RecordingIndicator`. Mirrors the same visual style — a
 * compact coloured pill with a pulsing dot — but reads the
 * `streaming_active` flag off the room metadata (set by `egress_mgr`
 * whenever a LiveKit egress has a stream_output and cleared on stop /
 * egress_ended webhook). Colour distinguishes it from the recording
 * pill: blue/cyan instead of red.
 */
export default function StreamingIndicator() {
  const { t } = useTranslation();
  const room = useRoomContext();
  const [active, setActive] = useState(false);

  useEffect(() => {
    const read = () => {
      try {
        const md = JSON.parse(room.metadata || "{}");
        setActive(!!md.streaming_active);
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
        background: "#1e3a8a",
        color: "white",
        borderRadius: 4,
        fontSize: "0.85em",
        fontWeight: 600,
      }}
      aria-live="polite"
      data-testid="streaming-indicator"
    >
      <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#60a5fa" }} />
      {t("streamingIndicator.label", { defaultValue: "Streaming" })}
    </span>
  );
}

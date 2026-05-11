import { useEffect, useRef, useState } from "react";
import type { Room } from "livekit-client";
import { RoomEvent } from "livekit-client";

const TOPIC = "meet-reaction";
const ENCODER = new TextEncoder();
const DECODER = new TextDecoder();
const LIFESPAN_MS = 4000;

interface Reaction {
  id: number;
  emoji: string;
  name: string;
  lane: number;
}

export interface ReactionPayload {
  v: 1;
  emoji: string;
  name: string;
}

/** Broadcast a transient reaction to everyone in the room. */
export async function broadcastReaction(room: Room, emoji: string) {
  const payload: ReactionPayload = {
    v: 1,
    emoji,
    name: room.localParticipant.name || room.localParticipant.identity || "",
  };
  await room.localParticipant.publishData(
    ENCODER.encode(JSON.stringify(payload)),
    { reliable: false, topic: TOPIC },
  );
}

interface Props {
  room: Room;
}

/**
 * Renders incoming + local reactions as emojis that float up the right edge
 * of the meeting stage and fade out. Uses LiveKit's data channel; no DB.
 */
export default function FloatingReactions({ room }: Props) {
  const [reactions, setReactions] = useState<Reaction[]>([]);
  const nextIdRef = useRef(1);
  const nextLaneRef = useRef(0);

  function add(emoji: string, name: string) {
    const id = nextIdRef.current++;
    const lane = nextLaneRef.current++ % 5;
    setReactions((cur) => [...cur, { id, emoji, name, lane }]);
    window.setTimeout(() => {
      setReactions((cur) => cur.filter((r) => r.id !== id));
    }, LIFESPAN_MS);
  }

  // Expose a way for the local Reactions button to push the user's own emoji
  // into the same overlay (they wouldn't otherwise see it via DataReceived).
  useEffect(() => {
    const onLocal = (e: Event) => {
      const detail = (e as CustomEvent<{ emoji: string; name: string }>).detail;
      if (!detail) return;
      add(detail.emoji, detail.name);
    };
    window.addEventListener("meet:local-reaction", onLocal as EventListener);
    return () => window.removeEventListener("meet:local-reaction", onLocal as EventListener);
  }, []);

  useEffect(() => {
    const onData = (
      payload: Uint8Array,
      _participant: unknown,
      _kind: unknown,
      topic?: string,
    ) => {
      if (topic !== TOPIC) return;
      try {
        const obj = JSON.parse(DECODER.decode(payload)) as ReactionPayload;
        if (obj?.v === 1 && typeof obj.emoji === "string") {
          add(obj.emoji, obj.name || "");
        }
      } catch {
        /* ignore malformed */
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room]);

  return (
    <div
      data-testid="floating-reactions"
      className="pointer-events-none absolute inset-0 overflow-hidden z-30"
    >
      {reactions.map((r) => (
        <span
          key={r.id}
          className="reaction-float absolute select-none"
          style={{
            right: `${8 + r.lane * 56}px`,
            bottom: "20%",
          }}
        >
          <span className="text-4xl drop-shadow">{r.emoji}</span>
          {r.name && (
            <span className="block text-[10px] text-white/80 text-center -mt-1 whitespace-nowrap">
              {r.name}
            </span>
          )}
        </span>
      ))}
    </div>
  );
}

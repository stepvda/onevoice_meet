import { useRoomContext } from "@livekit/components-react";
import BoardErrorBoundary from "./BoardErrorBoundary";
import MeetppBoard from "./MeetppBoard";
import { useMeetppReadOnly } from "./useMeetppSession";

/**
 * Read-only board for the recording / livestream page (/egress-layout/pip):
 * always follows, never pauses, transcript column narrowed to 260 px (FDD
 * §5.10). The egress page owns the session wiring via useMeetppReadOnly().
 */
export default function MeetppBoardView({ className }: { className?: string }) {
  // A board error must never unmount the egress page: that ends the recording.
  return (
    <BoardErrorBoundary>
      <MeetppBoard variant="egress" transcriptWidth={260} className={className ?? "h-full w-full rounded-none"} />
    </BoardErrorBoundary>
  );
}

/** Public view: loads the session with the viewer token and keeps it live.
 * PresenterSpotlight shows the board only when `settings.show_public`. */
export function MeetppPublicSync({ roomName, token }: { roomName: string; token: string }) {
  const room = useRoomContext();
  useMeetppReadOnly({ room, roomName, token, mode: "public" });
  return null;
}

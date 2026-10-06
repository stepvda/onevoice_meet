import { useEffect, useRef } from "react";
import { RoomEvent, type Room } from "livekit-client";
import { setMeetppRoomToken } from "../../lib/meetpp/api";
import { attachRoom, discover, postConsent, refetchState, resetSessionLayer } from "../../lib/meetpp/session";
import { configureFocus, setCtx, useMeetpp, type ClientMode } from "../../lib/meetpp/store";
import { usePreferences } from "../../lib/preferences";
import type { AnnounceMsg } from "../../lib/meetpp/types";

/**
 * Room participant wiring: room token for the REST calls, client context,
 * the meet-ai data channel, active speakers, reconnect handling (snapshot +
 * incremental transcript + consent re-post) and session discovery.
 */
export function useMeetppRoom({
  room,
  roomName,
  meetingId,
  isChair,
  roomToken,
}: {
  room: Room;
  roomName: string;
  meetingId: string | null;
  isChair: boolean;
  roomToken: string | null;
}): void {
  useEffect(() => {
    setMeetppRoomToken(roomToken);
  }, [roomToken]);

  useEffect(() => {
    configureFocus(false);
    setCtx({
      mode: "participant",
      isChair,
      meetingId,
      roomName,
      localIdentity: room.localParticipant.identity || null,
      localName: room.localParticipant.name || null,
    });
  }, [room, isChair, meetingId, roomName]);

  // The local identity is only known once connected; consent needs it.
  useEffect(() => {
    const onConnected = () => {
      setCtx({ localIdentity: room.localParticipant.identity || null, localName: room.localParticipant.name || null });
      void postConsent();
    };
    if (room.localParticipant.identity) onConnected();
    room.on(RoomEvent.Connected, onConnected);
    return () => {
      room.off(RoomEvent.Connected, onConnected);
    };
  }, [room]);

  // Leaving the room (or switching to another) drops its Meet++ state, so
  // the next meeting never shows the previous one's board.
  useEffect(() => {
    const detach = attachRoom(room, roomName);
    return () => {
      detach();
      resetSessionLayer();
    };
  }, [room, roomName]);

  // Discover on mount, then a slow safety poll (a missed `session` message
  // must not leave the board hidden or stale).
  useEffect(() => {
    if (!roomName) return;
    void discover(roomName);
    const timer = window.setInterval(() => {
      const s = useMeetpp.getState();
      if (s.active) void refetchState();
      else void discover(roomName);
    }, 30000);
    return () => window.clearInterval(timer);
  }, [roomName]);

  useAnnouncementAudio(true);
}

/**
 * Read-only wiring for the egress page and the public view. Returns whether
 * the board should be shown there (egress: unless the session excludes it
 * from recordings; public: only when the session allows it).
 */
export function useMeetppReadOnly({
  room,
  roomName,
  token,
  mode,
}: {
  room: Room | null;
  roomName: string;
  token: string | null;
  mode: Extract<ClientMode, "egress" | "public">;
}): boolean {
  useEffect(() => {
    setMeetppRoomToken(token);
  }, [token]);

  useEffect(() => {
    configureFocus(mode === "egress");
    setCtx({ mode, isChair: false, roomName, meetingId: null, localIdentity: null, localName: null });
  }, [mode, roomName]);

  useEffect(() => {
    if (!room) return undefined;
    const detach = attachRoom(room, roomName);
    return () => {
      detach();
      resetSessionLayer();
    };
  }, [room, roomName]);

  useEffect(() => {
    if (!roomName || !token) return;
    void discover(roomName);
    const timer = window.setInterval(() => {
      const s = useMeetpp.getState();
      if (s.active) void refetchState();
      else void discover(roomName);
    }, 15000);
    return () => window.clearInterval(timer);
  }, [roomName, token]);

  const active = useMeetpp((s) => s.active);
  const settings = useMeetpp((s) => s.snap?.session.settings);
  const allowed = active && (mode === "egress" ? settings?.in_recordings !== false : settings?.show_public === true);
  // The egress page plays the clip so recordings include it; the public view
  // only when the board is shown there.
  useAnnouncementAudio(mode !== "egress", allowed);
  return allowed;
}

// ── Announcement audio ────────────────────────────────────────────────────

function speak(text: string): void {
  if (typeof window === "undefined" || !("speechSynthesis" in window) || !text) return;
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-GB";
    const voice = window.speechSynthesis.getVoices().find((v) => v.lang?.toLowerCase().startsWith("en"));
    if (voice) u.voice = voice;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  } catch {
    /* no speech available */
  }
}

/** Play the Kokoro clip (`audio_url`); fall back to the browser's speech
 * synthesis (English) when the clip is missing or fails. */
export function playAnnouncement(a: Pick<AnnounceMsg, "audio_url" | "title" | "subtitle">): void {
  const text = [a.title, a.subtitle].filter(Boolean).join(". ");
  if (!a.audio_url) {
    speak(text);
    return;
  }
  let fellBack = false;
  const fallback = () => {
    if (fellBack) return;
    fellBack = true;
    speak(text);
  };
  try {
    const audio = new Audio(a.audio_url);
    audio.onerror = fallback;
    void audio.play().catch(fallback);
  } catch {
    fallback();
  }
}

/** Plays each announcement once (respecting the user's mute preference and
 * the session's `speak` setting). */
export function useAnnouncementAudio(respectMute: boolean, enabled = true): void {
  const lastAid = useRef<string | null>(null);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  useEffect(
    () =>
      useMeetpp.subscribe((s) => {
        const a = s.announcement;
        if (!a || a.aid === lastAid.current) return;
        lastAid.current = a.aid;
        if (!enabledRef.current) return;
        if (respectMute && !usePreferences.getState().notifications.speakAnnouncements) return;
        if (s.snap?.session.settings?.speak === false) return;
        playAnnouncement(a);
      }),
    [respectMute],
  );
}

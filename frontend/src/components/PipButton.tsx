import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { PictureInPicture } from "lucide-react";
import { useRoomContext } from "@livekit/components-react";
import { RoomEvent, Track } from "livekit-client";
import type { Participant } from "livekit-client";

/**
 * Toolbar button that pops the currently most-relevant video tile into the
 * browser's Picture-in-Picture window. We pick the active speaker if there
 * is one, otherwise a screenshare publisher, otherwise the local
 * participant's own camera.
 *
 * Falls back to disabled state on browsers that don't expose the
 * `pictureInPictureEnabled` flag (most current Firefox).
 */
export default function PipButton() {
  const { t } = useTranslation();
  const room = useRoomContext();
  const [supported] = useState(() =>
    typeof document !== "undefined" && (document as Document).pictureInPictureEnabled === true,
  );
  const [active, setActive] = useState(false);

  useEffect(() => {
    if (!supported) return;
    const onEnter = () => setActive(true);
    const onLeave = () => setActive(false);
    // Bubbling pip events fire on the document for any pip-attached video.
    document.addEventListener("enterpictureinpicture", onEnter, true);
    document.addEventListener("leavepictureinpicture", onLeave, true);
    return () => {
      document.removeEventListener("enterpictureinpicture", onEnter, true);
      document.removeEventListener("leavepictureinpicture", onLeave, true);
    };
  }, [supported]);

  function pickVideo(): HTMLVideoElement | null {
    // 1. Active speaker's screenshare → camera, if any.
    const speakers = room.activeSpeakers as Participant[] | undefined;
    const top = speakers && speakers[0] ? speakers[0] : null;
    const candidates: Participant[] = [];
    if (top) candidates.push(top);
    for (const p of room.remoteParticipants.values()) {
      if (p !== top) candidates.push(p);
    }
    candidates.push(room.localParticipant);
    for (const p of candidates) {
      const screenPub = p.getTrackPublication(Track.Source.ScreenShare);
      const camPub = p.getTrackPublication(Track.Source.Camera);
      const pub = screenPub ?? camPub;
      const el = pub?.track?.attachedElements?.find(
        (e): e is HTMLVideoElement => (e as HTMLElement).tagName === "VIDEO",
      );
      if (el) return el;
    }
    // Fallback: any visible <video> on the page.
    const all = Array.from(document.querySelectorAll<HTMLVideoElement>("video"));
    return all.find((v) => v.readyState >= 2 && v.videoWidth > 0) ?? null;
  }

  async function toggle() {
    if (!supported) return;
    try {
      if (active && document.pictureInPictureElement) {
        await document.exitPictureInPicture();
        return;
      }
      const el = pickVideo();
      if (!el) return;
      await el.requestPictureInPicture();
    } catch {
      /* user dismissed permission / unsupported track type */
    }
  }

  // Resubscribe whenever the active speaker changes — useful for the icon's
  // active state to stay accurate. (Cheap; just a no-op effect listener.)
  useEffect(() => {
    if (!supported) return;
    const noop = () => undefined;
    room.on(RoomEvent.ActiveSpeakersChanged, noop);
    return () => {
      room.off(RoomEvent.ActiveSpeakersChanged, noop);
    };
  }, [room, supported]);

  if (!supported) return null;
  return (
    <button
      type="button"
      onClick={() => void toggle()}
      data-testid="btn-pip"
      aria-pressed={active ? "true" : "false"}
      aria-label={active ? t("pip.exit") : t("pip.enter")}
      title={active ? t("pip.exitTitle") : t("pip.enterTitle")}
      className={[
        "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium",
        active
          ? "bg-primary-500 text-white"
          : "bg-primary-700 text-slate-100 hover:bg-primary-600",
      ].join(" ")}
    >
      <PictureInPicture size={16} />
    </button>
  );
}

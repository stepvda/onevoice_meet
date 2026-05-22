import { useEffect, useRef, useState } from "react";
import {
  Room,
  RoomEvent,
  Track,
  type RemoteTrack,
  type RemoteVideoTrack,
} from "livekit-client";

/**
 * Custom egress layout template for the Picture-in-Picture composition.
 *
 * LiveKit's headless Chrome (started by `RoomCompositeEgressRequest` with
 * `custom_base_url`) fetches this page and captures it as the recording
 * / livestream output. The egress appends `url`, `token`, `room`, and
 * `layout` query params; we accept an `overlay` query param as the
 * bootstrap value, but the live source of truth is the LiveKit room
 * metadata (`pip_enabled` + `pip_overlay_identity`). That way, the host
 * can toggle PiP / change the overlay person mid-stream and the page
 * adjusts without an egress restart.
 *
 * Composition:
 *   - Main (full-bleed): screenshare > "playback" participant > active
 *     speaker > any subscribed cam. Always rendered.
 *   - Overlay (bottom-right ~22% width, 16:9): the
 *     `pip_overlay_identity` participant's camera. Hidden when PiP is
 *     off or that participant isn't publishing.
 *
 * Audio: every subscribed audio track is attached to a hidden `<audio>`
 * element so LiveKit's egress captures it. The main video's own audio
 * is muted to prevent double playback.
 *
 * This page is never visible to humans — egress's Chrome loads it
 * headlessly, the page subscribes to the room, and the egress encodes
 * the composite. Caddy serves it under `/egress-layout/*` with a
 * permissive CSP (`frame-ancestors *`) because the egress connects
 * without cookies or origin.
 */
export default function EgressLayoutPiP() {
  const params = new URLSearchParams(window.location.search);
  const url = params.get("url") || "";
  const token = params.get("token") || "";
  const overlayHint = params.get("overlay") || "";

  const mainVideoRef = useRef<HTMLVideoElement>(null);
  const overlayVideoRef = useRef<HTMLVideoElement>(null);
  const audioContainerRef = useRef<HTMLDivElement>(null);
  const [overlayVisible, setOverlayVisible] = useState(false);
  const [status, setStatus] = useState<"connecting" | "connected" | "error">("connecting");

  useEffect(() => {
    if (!url || !token) {
      setStatus("error");
      return;
    }

    const room = new Room({
      adaptiveStream: false,
      dynacast: false,
    });

    const attachedAudios = new Map<string, HTMLAudioElement>();
    let currentMainSid: string | null = null;
    let currentOverlaySid: string | null = null;
    // Live PiP state, updated from room metadata. Initial values come
    // from URL params (the egress fires this page before metadata is
    // necessarily readable) and `pip_enabled` flips on the first
    // metadata read if the meeting has it set.
    let pipEnabled = false;
    let pipOverlayIdentity = overlayHint;

    function pickMain(): RemoteVideoTrack | null {
      // 1. If the server-side compositor has published a composite
      //    track, it's already the final composition (main + corner
      //    overlay baked in). Use it directly and skip the egress-side
      //    overlay draw.
      for (const p of room.remoteParticipants.values()) {
        if (!p.identity.startsWith("composite-")) continue;
        for (const pub of p.videoTrackPublications.values()) {
          // See note below on `pub.isSubscribed`: skip on track-absence
          // only.
          if (pub.videoTrack) {
            return pub.videoTrack as RemoteVideoTrack;
          }
        }
      }
      // 2. Fallback priority ladder for when no compositor is running
      //    (PiP off, or compositor still starting up). The egress is a
      //    subscribe-only client so every track we see is remote — the
      //    cast keeps TS happy without runtime cost.
      let best: RemoteVideoTrack | null = null;
      let bestPriority = -1;
      const speakers = new Set(room.activeSpeakers.map((p) => p.identity));
      room.remoteParticipants.forEach((p) => {
        if (p.identity.startsWith("composite-")) return;
        p.videoTrackPublications.forEach((pub) => {
          const t = pub.videoTrack as RemoteVideoTrack | undefined;
          // Don't skip on `!pub.isSubscribed` — that flag flickers
          // false during subscription renegotiations (e.g. when
          // someone publishes a new audio track mid-stream) while
          // `videoTrack` is still around. LiveKit sets `videoTrack`
          // to undefined on a real unsubscribe, so `!t` is enough.
          // Skipping on the flag caused the egress to drop playback
          // as "main" momentarily and fall through to the active
          // speaker (the person who just turned their mic on),
          // requiring a livestream restart to recover.
          if (!t) return;
          let priority = 0;
          if (pub.source === Track.Source.ScreenShare) priority = 3;
          else if (p.identity === "playback") priority = 2;
          else if (speakers.has(p.identity)) priority = 1;
          if (priority > bestPriority) {
            bestPriority = priority;
            best = t;
          }
        });
      });
      return best;
    }

    function isCompositeMain(track: RemoteVideoTrack | null): boolean {
      if (!track) return false;
      const ident = track.sid
        ? room.remoteParticipants
            ? [...room.remoteParticipants.values()].find((p) =>
                [...p.videoTrackPublications.values()].some(
                  (pub) => pub.videoTrack?.sid === track.sid,
                ),
              )?.identity
            : undefined
        : undefined;
      return !!ident && ident.startsWith("composite-");
    }

    function pickOverlay(): RemoteVideoTrack | null {
      // When the composite track is the main, don't draw the egress's
      // own overlay — the composite already has it.
      if (isCompositeMain(pickMain())) return null;
      // Off entirely unless room metadata says PiP is on.
      if (!pipEnabled) return null;
      if (!pipOverlayIdentity) return null;
      const p = room.remoteParticipants.get(pipOverlayIdentity);
      if (!p) return null;
      for (const pub of p.videoTrackPublications.values()) {
        // Same robustness rule as `pickMain`: trust `videoTrack`
        // presence, not the `isSubscribed` flag which flickers
        // during subscription renegotiations.
        if (pub.source === Track.Source.Camera && pub.videoTrack) {
          return pub.videoTrack as RemoteVideoTrack;
        }
      }
      return null;
    }

    function readMetadata() {
      try {
        const md = JSON.parse(room.metadata || "{}");
        pipEnabled = !!md.pip_enabled;
        // `pip_overlay_identity` may be null/undefined when no choice
        // has been made yet — fall back to nothing (overlay hides).
        pipOverlayIdentity =
          typeof md.pip_overlay_identity === "string"
            ? md.pip_overlay_identity
            : "";
      } catch {
        // ignore — keep last-known values
      }
    }

    function refresh() {
      const main = pickMain();
      const mainSid = main?.sid ?? null;
      if (mainSid !== currentMainSid) {
        if (mainVideoRef.current) {
          if (currentMainSid) {
            mainVideoRef.current.srcObject = null;
          }
          if (main) main.attach(mainVideoRef.current);
        }
        currentMainSid = mainSid;
      }

      const ov = pickOverlay();
      const ovSid = ov?.sid ?? null;
      if (ovSid !== currentOverlaySid) {
        if (overlayVideoRef.current) {
          if (currentOverlaySid) {
            overlayVideoRef.current.srcObject = null;
          }
          if (ov) ov.attach(overlayVideoRef.current);
        }
        currentOverlaySid = ovSid;
      }
      setOverlayVisible(!!ov);
    }

    function attachAudio(track: RemoteTrack) {
      if (track.kind !== Track.Kind.Audio) return;
      if (!audioContainerRef.current) return;
      const sid = track.sid;
      if (!sid || attachedAudios.has(sid)) return;
      const el = document.createElement("audio");
      el.autoplay = true;
      track.attach(el);
      audioContainerRef.current.appendChild(el);
      attachedAudios.set(sid, el);
    }

    function detachAudio(track: RemoteTrack) {
      const sid = track.sid;
      if (!sid) return;
      const el = attachedAudios.get(sid);
      if (!el) return;
      track.detach(el);
      el.remove();
      attachedAudios.delete(sid);
    }

    function onTrackSubscribed(track: RemoteTrack) {
      if (track.kind === Track.Kind.Audio) attachAudio(track);
      refresh();
    }
    function onTrackUnsubscribed(track: RemoteTrack) {
      if (track.kind === Track.Kind.Audio) detachAudio(track);
      refresh();
    }

    function onMetadataChanged() {
      readMetadata();
      refresh();
    }

    room.on(RoomEvent.TrackSubscribed, onTrackSubscribed);
    room.on(RoomEvent.TrackUnsubscribed, onTrackUnsubscribed);
    room.on(RoomEvent.ActiveSpeakersChanged, refresh);
    room.on(RoomEvent.ParticipantConnected, refresh);
    room.on(RoomEvent.ParticipantDisconnected, refresh);
    room.on(RoomEvent.RoomMetadataChanged, onMetadataChanged);

    // Safety net: even with the `isSubscribed`-flicker fix above, any
    // future subscription transition we forgot to subscribe an event
    // for would otherwise leave the picker stuck on the wrong main
    // until the egress is restarted. A 2-second re-pick is cheap and
    // self-corrects within a couple of frames.
    const tick = setInterval(refresh, 2000);

    room
      .connect(url, token, { autoSubscribe: true })
      .then(() => {
        setStatus("connected");
        // Seed PiP state from whatever metadata the room already has,
        // then render. The connect() promise resolves after the SDK
        // has the first metadata snapshot.
        readMetadata();
        refresh();
        // **CRITICAL** LiveKit Egress's headless Chrome watches the
        // browser console for the exact string `START_RECORDING` and
        // blocks the capture pipeline until that line appears. Without
        // it the egress times out with code 412 "Start signal not
        // received" and aborts the recording / livestream within
        // ~10 s of creation. See:
        //   livekit/egress/pkg/pipeline/source/web.go (const
        //   startRecordingLog = "START_RECORDING")
        // The matching END_RECORDING signal is emitted on unmount so
        // egress finalises the file cleanly.
        console.log("START_RECORDING");
      })
      .catch(() => {
        setStatus("error");
      });

    return () => {
      clearInterval(tick);
      attachedAudios.forEach((el) => el.remove());
      attachedAudios.clear();
      console.log("END_RECORDING");
      void room.disconnect();
    };
  }, [url, token, overlayHint]);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "#000",
        overflow: "hidden",
        margin: 0,
        padding: 0,
      }}
    >
      <video
        ref={mainVideoRef}
        autoPlay
        muted
        playsInline
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          objectFit: "contain",
          background: "#000",
        }}
      />
      <video
        ref={overlayVideoRef}
        autoPlay
        muted
        playsInline
        style={{
          position: "absolute",
          right: "2%",
          bottom: "2%",
          width: "22%",
          aspectRatio: "16 / 9",
          objectFit: "cover",
          borderRadius: 8,
          border: "2px solid rgba(255,255,255,0.9)",
          background: "#111",
          boxShadow: "0 6px 18px rgba(0,0,0,0.55)",
          opacity: overlayVisible ? 1 : 0,
          transition: "opacity 200ms ease",
        }}
      />
      <div ref={audioContainerRef} style={{ display: "none" }} />
      {status === "error" && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#ddd",
            fontFamily: "sans-serif",
            fontSize: 18,
            background: "#000",
          }}
        >
          Egress layout failed to connect to the room.
        </div>
      )}
    </div>
  );
}

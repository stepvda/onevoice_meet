import { useEffect, useRef, useState } from "react";
import {
  Room,
  RoomEvent,
  Track,
  type RemoteTrack,
  type RemoteVideoTrack,
} from "livekit-client";

/**
 * Server-side PiP compositor (Pass 1, simplified).
 *
 * Runs inside the compositor service's Puppeteer Chrome (headed Chrome
 * on Xvfb). The page joins the LiveKit room as the reserved identity
 * `composite-<room>`, subscribes to every remote video track, and
 * **renders the PiP composition with plain DOM video elements**:
 *
 *   - `mainVideo`  : fills the entire viewport, holds the chosen "main"
 *                    track (screenshare > playback > active speaker > any cam).
 *   - `overlayVideo`: positioned in the bottom-right corner at ~22%
 *                    width, 16:9, holds the meeting's
 *                    `pip_overlay_identity` camera.
 *
 * The page then calls `navigator.mediaDevices.getDisplayMedia()` to
 * capture the page surface (same OS-level pipeline screensharing uses
 * — robust across containerised Chrome where `canvas.captureStream`
 * has been observed to silently produce no frames) and publishes the
 * resulting track back to the room as `Track.Source.ScreenShare`.
 *
 * Why no canvas? Earlier iterations drew main + overlay into a 2D
 * canvas and used `canvas.captureStream(30)` to produce the publish
 * track. That works in a real browser but produces a black track in
 * headless / Xvfb Chrome. Even with workarounds (setInterval instead
 * of rAF, anti-throttling flags, `headless:false` + Xvfb) the canvas
 * → captureStream → encode pipeline never delivered subscriber-visible
 * frames. `getDisplayMedia` captures the rendered surface, which is
 * exactly what we want — the same pixels Chrome paints to the Xvfb
 * framebuffer end up in the published track.
 */
export default function EgressLayoutComposite() {
  const params = new URLSearchParams(window.location.search);
  const url = params.get("url") || "";
  const token = params.get("token") || "";
  const roomName = params.get("room") || "";
  const overlayHint = params.get("overlay") || "";

  const mainVideoRef = useRef<HTMLVideoElement>(null);
  const overlayVideoRef = useRef<HTMLVideoElement>(null);
  const [status, setStatus] = useState<
    "connecting" | "publishing" | "error"
  >("connecting");
  const [statusMessage, setStatusMessage] = useState("Connecting…");

  useEffect(() => {
    if (!url || !token) {
      setStatus("error");
      setStatusMessage("Missing url or token");
      return;
    }

    const room = new Room({ adaptiveStream: false, dynacast: false });

    let mainTrackSid: string | null = null;
    let overlayTrackSid: string | null = null;
    let overlayIdentity = overlayHint;

    function pickMain(): RemoteVideoTrack | null {
      let best: RemoteVideoTrack | null = null;
      let bestPriority = -1;
      const speakers = new Set(room.activeSpeakers.map((p) => p.identity));
      room.remoteParticipants.forEach((p) => {
        if (p.identity.startsWith("composite-")) return;
        p.videoTrackPublications.forEach((pub) => {
          const t = pub.videoTrack as RemoteVideoTrack | undefined;
          if (!t || !pub.isSubscribed) return;
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

    function pickOverlay(): RemoteVideoTrack | null {
      if (!overlayIdentity) return null;
      const p = room.remoteParticipants.get(overlayIdentity);
      if (!p) return null;
      for (const pub of p.videoTrackPublications.values()) {
        if (
          pub.source === Track.Source.Camera &&
          pub.videoTrack &&
          pub.isSubscribed
        ) {
          return pub.videoTrack as RemoteVideoTrack;
        }
      }
      return null;
    }

    function attachToRef(
      ref: HTMLVideoElement | null,
      track: RemoteVideoTrack | null,
      currentSid: string | null,
    ): string | null {
      const sid = track?.sid ?? null;
      if (!ref) return currentSid;
      if (sid === currentSid) return currentSid;
      if (currentSid) ref.srcObject = null;
      if (track) {
        track.attach(ref);
        void ref.play().catch(() => undefined);
      }
      return sid;
    }

    function refresh() {
      const m = pickMain();
      const o = pickOverlay();
      // If the picked overlay IS the picked main (same person + same
      // source, can happen when only the overlay identity is in the
      // room), skip the overlay tile — viewers would see two copies of
      // the same face on top of itself.
      const showOverlay = o && m && !(o.sid === m.sid);
      mainTrackSid = attachToRef(mainVideoRef.current, m, mainTrackSid);
      overlayTrackSid = attachToRef(
        overlayVideoRef.current,
        showOverlay ? o : null,
        overlayTrackSid,
      );
      const me = mainVideoRef.current;
      const oe = overlayVideoRef.current;
      console.log(
        `compositor pick: main=${m?.sid ?? "none"} overlay=${
          showOverlay ? o?.sid : "none"
        } | mainEl=${me?.readyState}/${me?.videoWidth}x${me?.videoHeight}` +
          ` overlayEl=${oe?.readyState}/${oe?.videoWidth}x${oe?.videoHeight}`,
      );
    }

    function readMetadata() {
      try {
        const md = JSON.parse(room.metadata || "{}");
        if (typeof md.pip_overlay_identity === "string") {
          overlayIdentity = md.pip_overlay_identity;
        }
      } catch {
        /* keep last */
      }
    }

    room.on(RoomEvent.TrackSubscribed, (_t: RemoteTrack) => refresh());
    room.on(RoomEvent.TrackUnsubscribed, (_t: RemoteTrack) => refresh());
    room.on(RoomEvent.ActiveSpeakersChanged, refresh);
    room.on(RoomEvent.ParticipantConnected, refresh);
    room.on(RoomEvent.ParticipantDisconnected, refresh);
    room.on(RoomEvent.RoomMetadataChanged, () => {
      readMetadata();
      refresh();
    });

    room
      .connect(url, token, { autoSubscribe: true })
      .then(async () => {
        console.log("compositor: room connected, waiting layout tick");
        setStatusMessage("Connected, capturing page…");
        readMetadata();
        refresh();
        // Wait one tick so the freshly-attached video elements have
        // had a chance to lay out and start decoding before we ask the
        // browser to capture the page.
        await new Promise((r) => setTimeout(r, 250));
        console.log("compositor: calling getDisplayMedia");
        let screenStream: MediaStream;
        try {
          // `preferCurrentTab: true` (Chrome-only) requests current-tab
          // capture, bypassing the desktop picker. Combined with
          // `--use-fake-ui-for-media-stream` on the Chrome launch flags
          // the prompt is auto-accepted; we don't need
          // `--auto-select-desktop-capture-source` (which failed on Xvfb
          // because the source name didn't match). The TypeScript
          // libdom types don't include `preferCurrentTab` yet so we
          // cast through `Record<string, unknown>`.
          screenStream = await navigator.mediaDevices.getDisplayMedia({
            video: { frameRate: 30 },
            audio: false,
            preferCurrentTab: true,
          } as unknown as DisplayMediaStreamOptions);
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          console.error(`compositor: getDisplayMedia failed: ${msg}`);
          setStatus("error");
          setStatusMessage(`getDisplayMedia failed: ${msg}`);
          return;
        }
        const videoTrack = screenStream.getVideoTracks()[0];
        console.log(
          `compositor: getDisplayMedia returned ${
            screenStream.getVideoTracks().length
          } video track(s); first sid=${videoTrack?.id ?? "none"} settings=${
            videoTrack ? JSON.stringify(videoTrack.getSettings()) : "n/a"
          }`,
        );
        if (!videoTrack) {
          setStatus("error");
          setStatusMessage("getDisplayMedia returned no video track");
          return;
        }
        try {
          console.log("compositor: about to publishTrack");
          await room.localParticipant.publishTrack(videoTrack, {
            name: "composite",
            source: Track.Source.ScreenShare,
          });
          console.log("compositor: publishTrack resolved");
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          console.error(`compositor: publishTrack failed: ${msg}`);
          setStatus("error");
          setStatusMessage(`publishTrack failed: ${msg}`);
          return;
        }
        setStatus("publishing");
        setStatusMessage(
          `Publishing as ${room.localParticipant.identity}`,
        );
      })
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        console.error(`compositor: connect chain failed: ${msg}`);
        setStatus("error");
        setStatusMessage(`failed: ${msg}`);
      });

    return () => {
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
      {/* Main video, full-bleed, object-fit:contain so screenshare
          aspect ratio is preserved on widescreen sources. */}
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

      {/* Overlay, bottom-right, ~22% width, 16:9.
          The element is **always** mounted, fully opaque, full-size —
          no `display: none`, no `opacity: 0`, no `visibility: hidden`
          toggles, because all three make Chrome skip allocating /
          keeping a decode pipeline for the element, leaving it stuck
          at readyState=0 even after a track is attached. When no
          overlay track is attached the dark border + black background
          show through (a small dark corner box for a few hundred ms),
          which is preferable to a non-decoding video that never
          recovers. The picker just attaches the track when one
          becomes available and detaches when it doesn't. */}
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
        }}
      />

      {/* Status banner only visible to a human manually opening the
          URL; in compositor-driven Puppeteer headed mode this is
          rendered but never seen. */}
      <div
        style={{
          position: "absolute",
          left: 8,
          top: 8,
          color: "rgba(255,255,255,0.7)",
          fontFamily: "monospace",
          fontSize: 12,
          padding: "4px 8px",
          background: "rgba(0,0,0,0.4)",
          borderRadius: 4,
          pointerEvents: "none",
        }}
      >
        Compositor · {roomName || "(no room)"} · {status}: {statusMessage}
      </div>
    </div>
  );
}

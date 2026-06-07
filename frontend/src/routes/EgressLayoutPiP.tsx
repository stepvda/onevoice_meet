import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  Room,
  RoomEvent,
  Track,
  type RemoteTrack,
  type RemoteVideoTrack,
} from "livekit-client";

type EgressLayout = "single-speaker" | "speaker" | "grid";

/**
 * Custom egress layout template for recordings and livestreams.
 *
 * LiveKit's headless Chrome (started by `RoomCompositeEgressRequest` with
 * `custom_base_url`) fetches this page and captures it as the recording
 * / livestream output. The egress appends `url`, `token`, `room`, and
 * `layout` query params; we accept an `overlay` query param as the
 * bootstrap value for the room-metadata-driven PiP corner.
 *
 * Three layouts, switched by the `?layout=` query param the egress
 * appends (driven by the host's toolbar dropdown in Room.tsx):
 *
 *   - "single-speaker": one full-bleed video (screenshare > "playback"
 *     participant > active speaker > any subscribed cam) plus the
 *     optional room-metadata PiP corner overlay. This is the playback
 *     default — the compositor's composite track, when present, owns
 *     the frame and the egress-side overlay is skipped.
 *
 *   - "speaker": main video as in single-speaker, but with a bottom
 *     thumbnail strip showing every OTHER camera-publishing
 *     participant. No corner PiP overlay (the strip serves that role).
 *
 *   - "grid": equal-tile grid of every camera and screenshare track in
 *     the room (no main, no overlay). Columns scale with participant
 *     count.
 *
 * Audio: every subscribed audio track is attached to a hidden `<audio>`
 * element so LiveKit's egress captures it. Visible video elements are
 * muted to prevent double playback.
 *
 * This page is never visible to humans — egress's Chrome loads it
 * headlessly, the page subscribes to the room, and the egress encodes
 * the composite. Caddy serves it under `/egress-layout/*` with a
 * permissive CSP (`frame-ancestors *`) because the egress connects
 * without cookies or origin.
 */
function parseLayout(v: string | null): EgressLayout {
  return v === "grid" || v === "speaker" || v === "single-speaker" ? v : "grid";
}

/** Square-ish grid: 1, 2, 3-4, 5-6, 7-9, 10-12, 13-16, 17-20 cols → 1,2,2,3,3,4,4,5 */
function gridColumns(n: number): number {
  if (n <= 1) return 1;
  return Math.ceil(Math.sqrt(n));
}

/** Attaches a single LiveKit video track to its own <video> element and
 * detaches on unmount. Keyed by track sid in the caller. */
function VideoTile({ track, style }: { track: RemoteVideoTrack; style?: CSSProperties }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    track.attach(el);
    return () => {
      track.detach(el);
    };
  }, [track]);
  return (
    <video
      ref={ref}
      autoPlay
      muted
      playsInline
      style={{
        width: "100%",
        height: "100%",
        objectFit: "cover",
        background: "#111",
        borderRadius: 6,
        ...style,
      }}
    />
  );
}

export default function EgressLayoutPiP() {
  const { url, token, overlayHint, initialLayout } = useMemo(() => {
    const p = new URLSearchParams(window.location.search);
    return {
      url: p.get("url") || "",
      token: p.get("token") || "",
      overlayHint: p.get("overlay") || "",
      // The URL `layout` is the layout the egress was *started* with —
      // used only as the initial render before LiveKit room metadata
      // arrives. Live layout changes are then driven by room metadata
      // (`room_layout`), so a host clicking the toolbar picker mid-
      // recording switches the composite without restarting the egress.
      initialLayout: parseLayout(p.get("layout")),
    };
  }, []);

  const mainVideoRef = useRef<HTMLVideoElement>(null);
  const overlayVideoRef = useRef<HTMLVideoElement>(null);
  const audioContainerRef = useRef<HTMLDivElement>(null);
  // Holds the inner `refresh` closure so a separate useEffect (firing
  // when `layout` / `hasScreenshare` change) can trigger an attach
  // immediately after React commits the new DOM. Without that, the
  // grid → speaker flip (caused by someone starting screenshare) would
  // wait up to 2 s for the periodic tick before the screenshare
  // actually shows up in the recording.
  const refreshRef = useRef<() => void>(() => {});
  const [overlayVisible, setOverlayVisible] = useState(false);
  // Live layout, seeded from URL, then driven by room metadata.
  const [layout, setLayout] = useState<EgressLayout>(initialLayout);
  // True when any participant has an active screenshare. In "grid" mode
  // we auto-promote the screenshare to the main tile and reduce other
  // cameras to a thumbnail strip — matches Meet/Zoom and prevents a
  // shared screen from being shrunk into one of N equal cells alongside
  // little webcam thumbnails of the people watching it.
  const [hasScreenshare, setHasScreenshare] = useState(false);
  // True when the meeting has PiP enabled (room metadata `pip_enabled`).
  // PiP wins over the room layout: the JSX always renders single-speaker
  // (main full-bleed + corner overlay), regardless of `layout` or the
  // grid+screenshare promotion. Without this, a host enabling PiP while
  // in grid mode with a screenshare would lose the corner overlay in the
  // recording because the layout would otherwise resolve to "speaker"
  // and skip the overlay attach.
  const [pipActive, setPipActive] = useState(false);
  // Tracks for the layout-specific extras. `speaker` populates this with
  // every non-main cam; `grid` populates it with every cam + screenshare.
  const [extraTracks, setExtraTracks] = useState<RemoteVideoTrack[]>([]);
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
    // Live state, updated from room metadata. Initial values come from
    // URL params (the egress page renders before metadata is necessarily
    // readable). `pickMain`, `pickExtras`, etc. read these closure vars
    // — keeping them in scope avoids the React-stale-closure trap when
    // the layout changes mid-recording.
    let pipEnabled = false;
    let pipOverlayIdentity = overlayHint;
    let currentLayout: EgressLayout = initialLayout;
    let presenterId: string | null = null;

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
      // 2. Fallback priority ladder for when no compositor is running.
      //    Mirrors PresenterSpotlight so live + recording pick the same
      //    person: screenshare > playback > presenter (Take stage) >
      //    active speaker > any cam.
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
          if (!t) return;
          let priority = 0;
          if (pub.source === Track.Source.ScreenShare) priority = 4;
          else if (p.identity === "playback") priority = 3;
          else if (presenterId && p.identity === presenterId && pub.source === Track.Source.Camera) priority = 2;
          else if (speakers.has(p.identity) && pub.source === Track.Source.Camera) priority = 1;
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

    // True iff at least one participant is publishing a screenshare track
    // we can render. Used to switch the effective layout from "grid" to
    // "speaker" so the screenshare gets the main tile.
    function detectScreenshare(): boolean {
      for (const p of room.remoteParticipants.values()) {
        if (p.identity.startsWith("composite-")) continue;
        for (const pub of p.videoTrackPublications.values()) {
          if (pub.source === Track.Source.ScreenShare && pub.videoTrack) {
            return true;
          }
        }
      }
      return false;
    }

    function effectiveLayout(): EgressLayout {
      // PiP wins over everything: render as single-speaker (main full-
      // bleed + corner overlay). The compositor-track case is
      // automatically handled by pickMain (returns the composite track)
      // and by pickOverlay (returns null when composite is main, so the
      // overlay element stays hidden — composite already has the PiP
      // baked in). Same precedence as PresenterSpotlight on the live side.
      if (pipEnabled) return "single-speaker";
      // Grid + active screenshare → behave as "speaker" so the shared
      // screen owns the main tile and the webcams form a thumbnail strip.
      // Otherwise the screenshare gets squashed into one of N equal
      // cells next to little webcam thumbnails — useless for the viewer.
      if (currentLayout === "grid" && detectScreenshare()) return "speaker";
      return currentLayout;
    }

    function pickExtras(mainSid: string | null): RemoteVideoTrack[] {
      // For "speaker" (or effective speaker): every camera that isn't the main.
      // For "grid": every camera + screenshare in the room.
      // Composite-main short-circuit: when the compositor is publishing
      // the final frame already, we don't add side tiles in any layout —
      // the composite stays full-bleed (matches the playback path).
      if (isCompositeMain(pickMain())) return [];
      const eff = effectiveLayout();
      const out: RemoteVideoTrack[] = [];
      const seen = new Set<string>();
      room.remoteParticipants.forEach((p) => {
        if (p.identity.startsWith("composite-")) return;
        p.videoTrackPublications.forEach((pub) => {
          const t = pub.videoTrack as RemoteVideoTrack | undefined;
          if (!t) return;
          const sid = t.sid;
          if (!sid || seen.has(sid)) return;
          if (eff === "grid") {
            if (pub.source !== Track.Source.Camera && pub.source !== Track.Source.ScreenShare) return;
            seen.add(sid);
            out.push(t);
          } else {
            // "speaker" (explicit or grid-with-screenshare): cams only,
            // skip the main one (which is the screenshare in the
            // promoted case).
            if (pub.source !== Track.Source.Camera) return;
            if (sid === mainSid) return;
            seen.add(sid);
            out.push(t);
          }
        });
      });
      return out;
    }

    function readMetadata() {
      try {
        const md = JSON.parse(room.metadata || "{}");
        pipEnabled = !!md.pip_enabled;
        // Mirror to React state so the JSX (which can't see the closure
        // var) picks single-speaker layout while PiP is on. Comparison
        // avoids a re-render when the flag didn't actually change.
        setPipActive((cur) => (cur === pipEnabled ? cur : pipEnabled));
        // `pip_overlay_identity` may be null/undefined when no choice
        // has been made yet — fall back to nothing (overlay hides).
        pipOverlayIdentity =
          typeof md.pip_overlay_identity === "string"
            ? md.pip_overlay_identity
            : "";
        presenterId =
          typeof md.presenter_identity === "string"
            ? md.presenter_identity
            : null;
        // Live layout updates — mirror the host's picker click. We update
        // BOTH the closure var (consumed by pickMain/pickExtras/refresh
        // each tick) AND the React state (drives JSX branching). When
        // layout flips, calling refresh() afterward swaps the DOM in one
        // pass without an egress restart.
        const layoutFromMeta = parseLayout(
          typeof md.room_layout === "string" ? md.room_layout : null,
        );
        if (md.room_layout && currentLayout !== layoutFromMeta) {
          currentLayout = layoutFromMeta;
          setLayout(layoutFromMeta);
        }
      } catch {
        // ignore — keep last-known values
      }
    }

    function refresh() {
      const eff = effectiveLayout();
      // Mirror the screenshare-detection result to React state so the JSX
      // (which runs outside this closure) can pick the same effective
      // branch. Only flip the bit when it actually changes, to avoid
      // re-rendering on every tick.
      const ss = detectScreenshare();
      setHasScreenshare((cur) => (cur === ss ? cur : ss));

      // The main <video> is attached in "single-speaker" and "speaker"
      // (or grid-promoted-to-speaker) layouts. In pure grid we render
      // every track as an extra tile and don't use the main element.
      // CRITICAL: only commit `currentMainSid` after a successful attach.
      // When the layout flips from grid → speaker (e.g. someone starts
      // screen-sharing), the main <video> element isn't in the DOM yet
      // — React hasn't re-rendered. If we updated `currentMainSid`
      // anyway, the next refresh would see the sids match and skip the
      // attach, leaving the recording without the screenshare. The
      // `layout`/`hasScreenshare` useEffect below also schedules a
      // post-commit refresh so the attach happens immediately.
      if (eff !== "grid") {
        const main = pickMain();
        const mainSid = main?.sid ?? null;
        if (mainSid !== currentMainSid) {
          if (mainVideoRef.current) {
            if (currentMainSid) {
              mainVideoRef.current.srcObject = null;
            }
            if (main) main.attach(mainVideoRef.current);
            currentMainSid = mainSid;
          }
        }
      } else if (currentMainSid) {
        // Layout just flipped to grid — drop the main attachment so the
        // grid tiles own the rendering.
        if (mainVideoRef.current) mainVideoRef.current.srcObject = null;
        currentMainSid = null;
      }

      // PiP corner overlay attaches whenever the effective layout is
      // single-speaker — that includes both the explicit single-speaker
      // pick and the "PiP-active overrides everything" branch in
      // effectiveLayout(). pickOverlay returns null when no overlay
      // should appear (composite track is main, PiP off, or no overlay
      // identity), so this is harmless when the overlay isn't wanted.
      if (eff === "single-speaker") {
        const ov = pickOverlay();
        const ovSid = ov?.sid ?? null;
        if (ovSid !== currentOverlaySid) {
          if (overlayVideoRef.current) {
            if (currentOverlaySid) {
              overlayVideoRef.current.srcObject = null;
            }
            if (ov) ov.attach(overlayVideoRef.current);
            currentOverlaySid = ovSid;
          }
        }
        setOverlayVisible(!!ov);
      } else {
        if (currentOverlaySid && overlayVideoRef.current) {
          overlayVideoRef.current.srcObject = null;
        }
        currentOverlaySid = null;
        setOverlayVisible(false);
      }

      // Extras: thumbnails for effective "speaker", tiles for grid,
      // none for single-speaker (which also covers PiP — overlay element
      // takes the corner; we don't want a strip on top of that).
      if (eff === "single-speaker") {
        setExtraTracks((cur) => (cur.length === 0 ? cur : []));
      } else {
        const mainSidForExtras = eff === "speaker" ? (pickMain()?.sid ?? null) : null;
        const next = pickExtras(mainSidForExtras);
        setExtraTracks((cur) => {
          if (cur.length !== next.length) return next;
          for (let i = 0; i < cur.length; i++) {
            if (cur[i].sid !== next[i].sid) return next;
          }
          return cur;
        });
      }
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

    // Expose `refresh` to outer scope so a separate effect can trigger
    // it right after React commits a layout-driven DOM change.
    refreshRef.current = refresh;

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
      refreshRef.current = () => {};
      console.log("END_RECORDING");
      void room.disconnect();
    };
    // `layout` is intentionally NOT in deps — it's mirrored to a closure
    // variable (`currentLayout`) updated from `readMetadata`, so changes
    // take effect without tearing down the LiveKit connection.
  }, [url, token, overlayHint]);

  // Whenever the effective layout flips (e.g. someone starts/stops
  // screen-sharing in grid mode), React mounts/unmounts the main
  // <video> / overlay / thumbnail elements. The `refresh()` call that
  // CAUSED the flip ran before React committed, so `mainVideoRef.current`
  // was still null and the track attach was skipped. This effect fires
  // AFTER the commit, so a follow-up refresh wires the now-mounted
  // element to its track immediately (otherwise we'd wait up to 2 s
  // for the periodic tick).
  useEffect(() => {
    // Microtask delay so React's ref-attach has run by the time we read.
    const id = setTimeout(() => refreshRef.current(), 0);
    return () => clearTimeout(id);
  }, [layout, hasScreenshare, pipActive]);

  // Compute the effective layout for rendering. Same precedence as the
  // imperative `effectiveLayout()` closure used by refresh():
  //   1. PiP active → single-speaker (main + corner overlay).
  //   2. Grid + active screenshare → speaker (screenshare main +
  //      thumbnail strip).
  //   3. Otherwise the host's picked layout.
  const effectiveRenderLayout: EgressLayout = pipActive
    ? "single-speaker"
    : layout === "grid" && hasScreenshare
      ? "speaker"
      : layout;
  // In "speaker" mode the main video shrinks to leave the bottom ~22%
  // for the thumbnail strip; in "single-speaker" it fills the frame.
  // Sizing has to be explicit — <video> is a replaced element, so
  // width/height: auto falls back to the stream's intrinsic dimensions
  // and the element sits at native size inside the inset box instead of
  // filling it.
  const isSpeaker = effectiveRenderLayout === "speaker";
  const mainHeightPct = isSpeaker ? "78%" : "100%";
  const cols = gridColumns(extraTracks.length);
  const rows = Math.max(1, Math.ceil(extraTracks.length / Math.max(1, cols)));

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
      {effectiveRenderLayout !== "grid" && (
        <video
          ref={mainVideoRef}
          autoPlay
          muted
          playsInline
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            width: "100%",
            height: mainHeightPct,
            objectFit: "contain",
            background: "#000",
          }}
        />
      )}

      {effectiveRenderLayout === "single-speaker" && (
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
      )}

      {effectiveRenderLayout === "speaker" && (
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: 0,
            height: "22%",
            display: "flex",
            gap: 8,
            padding: "8px 12px",
            background: "rgba(0,0,0,0.35)",
            backdropFilter: "blur(4px)",
            alignItems: "stretch",
            justifyContent: "center",
          }}
        >
          {extraTracks.map((t) => (
            <div
              key={t.sid}
              style={{
                aspectRatio: "16 / 9",
                height: "100%",
                flex: "0 0 auto",
              }}
            >
              <VideoTile track={t} />
            </div>
          ))}
        </div>
      )}

      {effectiveRenderLayout === "grid" && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "grid",
            gridTemplateColumns: `repeat(${cols}, 1fr)`,
            gridTemplateRows: `repeat(${rows}, 1fr)`,
            gap: 8,
            padding: 8,
          }}
        >
          {extraTracks.map((t) => (
            <VideoTile key={t.sid} track={t} style={{ borderRadius: 6 }} />
          ))}
        </div>
      )}

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

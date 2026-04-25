import { useEffect, useMemo, useRef } from "react";
import { useTracks } from "@livekit/components-react";
import { Track } from "livekit-client";

interface Props {
  width?: number;
  height?: number;
  className?: string;
}

/**
 * Real-time oscilloscope of the room's combined audio. Subscribes to every
 * mic + screen-share-audio track in the room (local + remote), pipes them
 * into a single AnalyserNode via Web Audio API, and draws the time-domain
 * waveform onto a small canvas at ~60 fps.
 *
 * The analyser is a sink — we never connect it to `audioCtx.destination`,
 * so this visualizer doesn't add to playback (RoomAudioRenderer handles
 * that). Audio just gets read for visualization.
 */
export default function AudioWaveform({
  width = 120,
  height = 24,
  className = "",
}: Props) {
  const tracks = useTracks(
    [Track.Source.Microphone, Track.Source.ScreenShareAudio],
    { onlySubscribed: false }
  );
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);

  // useTracks returns a freshly-allocated array on each parent re-render,
  // so depending on `tracks` directly would tear down and rebuild the
  // entire AudioContext+MediaStreamSources on every render — thrashing
  // Web Audio's small context budget and causing audible glitches.
  // Derive a stable signature from the underlying MediaStreamTrack ids and
  // re-run only when that string actually changes.
  const audioTracks = useMemo(() => {
    const out: MediaStreamTrack[] = [];
    for (const t of tracks) {
      const mst = t.publication?.track?.mediaStreamTrack;
      if (mst && mst.kind === "audio") out.push(mst);
    }
    return out;
  }, [tracks]);
  const trackKey = audioTracks.map((mst) => mst.id).sort().join("|");

  // Build / rebuild the audio graph whenever the set of audio tracks changes.
  useEffect(() => {
    if (audioTracks.length === 0) {
      analyserRef.current = null;
      return;
    }

    const Ctor: typeof AudioContext =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext!;
    if (!Ctor) return;
    const ctx = new Ctor();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.4;
    audioCtxRef.current = ctx;
    analyserRef.current = analyser;

    const sources: MediaStreamAudioSourceNode[] = [];
    for (const mst of audioTracks) {
      try {
        const stream = new MediaStream([mst]);
        const src = ctx.createMediaStreamSource(stream);
        src.connect(analyser);
        sources.push(src);
      } catch {
        /* track may not be ready; skip */
      }
    }

    // Browsers gate AudioContext on a user gesture; the user already clicked
    // "Join", but resume just in case Chrome left it suspended.
    if (ctx.state === "suspended") {
      ctx.resume().catch(() => {
        /* ignore */
      });
    }

    return () => {
      for (const s of sources) {
        try {
          s.disconnect();
        } catch {
          /* noop */
        }
      }
      try {
        analyser.disconnect();
      } catch {
        /* noop */
      }
      ctx.close().catch(() => {
        /* noop */
      });
      if (audioCtxRef.current === ctx) audioCtxRef.current = null;
      if (analyserRef.current === analyser) analyserRef.current = null;
    };
    // Stable signature: only re-run when the actual set of media tracks
    // changes, not on every parent re-render. Including `audioTracks` would
    // re-trigger every render because useTracks returns a fresh array.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trackKey]);

  // Drawing loop — runs as long as the canvas is mounted; reads from
  // whatever analyser the audio-graph effect last installed.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx2d = canvas.getContext("2d");
    if (!ctx2d) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx2d.scale(dpr, dpr);

    let raf = 0;
    let buf = new Uint8Array(1024);

    const draw = () => {
      raf = requestAnimationFrame(draw);
      ctx2d.clearRect(0, 0, width, height);
      const analyser = analyserRef.current;
      if (!analyser) {
        // Idle: flat centre line.
        ctx2d.beginPath();
        ctx2d.strokeStyle = "rgba(76, 175, 80, 0.5)";
        ctx2d.lineWidth = 1;
        ctx2d.moveTo(0, height / 2);
        ctx2d.lineTo(width, height / 2);
        ctx2d.stroke();
        return;
      }
      if (buf.length !== analyser.fftSize) {
        buf = new Uint8Array(analyser.fftSize);
      }
      analyser.getByteTimeDomainData(buf);
      ctx2d.beginPath();
      ctx2d.strokeStyle = "#4CAF50";
      ctx2d.lineWidth = 1.5;
      const slice = width / buf.length;
      let x = 0;
      for (let i = 0; i < buf.length; i += 1) {
        const v = buf[i] / 128.0; // 0–2, centred at 1
        const y = (v * height) / 2;
        if (i === 0) ctx2d.moveTo(x, y);
        else ctx2d.lineTo(x, y);
        x += slice;
      }
      ctx2d.stroke();
    };
    draw();
    return () => cancelAnimationFrame(raf);
  }, [width, height]);

  return (
    <canvas
      ref={canvasRef}
      data-testid="audio-waveform"
      aria-hidden="true"
      className={className}
      style={{ width, height }}
    />
  );
}

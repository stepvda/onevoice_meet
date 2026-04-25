/**
 * TI Café audio context — a single, app-global LiveKit Room that survives
 * navigation between meet pages. The Room object is created lazily on
 * `connect()` and torn down only on `disconnect()` or logout.
 *
 * Why a Provider and not a hook?
 *   - The connection must outlive the /ti-cafe route (the user can be live
 *     in the café while browsing /recordings).
 *   - We want a single ConnectionState + per-tab mute persistence.
 *
 * Mute persistence: mic/speaker mute booleans are stored in sessionStorage,
 * so they survive route changes and reloads within the same tab session
 * but reset on a new browser session — matching the spec.
 *
 * The Room is `audio: true, video: false`; nothing publishes a camera here.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  ConnectionState,
  Room,
  RoomEvent,
  Track,
  type RemoteAudioTrack,
  type RemoteParticipant,
  type RemoteTrack,
  type RemoteTrackPublication,
} from "livekit-client";
import { api } from "./api";
import { isAuthenticated } from "./auth";

const SS_MIC = "ti-cafe:mic-on";
const SS_SPEAKER = "ti-cafe:speaker-on";

function readBool(key: string, fallback: boolean): boolean {
  try {
    const v = sessionStorage.getItem(key);
    if (v === null) return fallback;
    return v === "1";
  } catch {
    return fallback;
  }
}

function writeBool(key: string, value: boolean): void {
  try {
    sessionStorage.setItem(key, value ? "1" : "0");
  } catch {
    /* private mode or full storage — ignore */
  }
}

/** Number of spectrum bands the bar's waveform consumes. */
export const SPECTRUM_BANDS = 24;

interface TICafeContextShape {
  connected: boolean;
  connecting: boolean;
  /** Mic publishing — false = muted (slash overlay in UI). */
  micOn: boolean;
  /** Output audio enabled — false = muted (slash overlay). */
  speakerOn: boolean;
  /** Smoothed log-scaled spectrum (0..1 per band) of the current loudest
   *  speaker's audio. Length is always SPECTRUM_BANDS. */
  spectrum: number[];
  /** Total participant count (including self when connected). */
  participantCount: number;
  /** Self LiveKit identity, e.g. "user-42", once connected. */
  selfIdentity: string | null;
  connect: () => Promise<void>;
  disconnect: () => void;
  setMic: (on: boolean) => void;
  setSpeaker: (on: boolean) => void;
}

const TICafeContext = createContext<TICafeContextShape | null>(null);

export function useTICafe(): TICafeContextShape {
  const ctx = useContext(TICafeContext);
  if (!ctx) throw new Error("useTICafe must be used inside <TICafeProvider>");
  return ctx;
}

export function TICafeProvider({ children }: { children: ReactNode }) {
  const roomRef = useRef<Room | null>(null);
  const rafRef = useRef<number | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);

  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [micOn, setMicOn] = useState<boolean>(() => readBool(SS_MIC, false));
  const [speakerOn, setSpeakerOn] = useState<boolean>(() => readBool(SS_SPEAKER, true));
  const [spectrum, setSpectrum] = useState<number[]>(() => new Array(SPECTRUM_BANDS).fill(0));
  const [participantCount, setParticipantCount] = useState(0);
  const [selfIdentity, setSelfIdentity] = useState<string | null>(null);
  // Mirror of speakerOn that the TrackSubscribed handler can read without
  // capturing a stale closure each time the listener was registered.
  const speakerOnRef = useRef<boolean>(speakerOn);

  // Persist mute toggles across in-app navigation (same tab session).
  useEffect(() => writeBool(SS_MIC, micOn), [micOn]);
  useEffect(() => writeBool(SS_SPEAKER, speakerOn), [speakerOn]);
  useEffect(() => {
    speakerOnRef.current = speakerOn;
  }, [speakerOn]);

  // ── Spectrum analyser. The AnalyserNode is rewired on each
  // ActiveSpeakersChanged so it tracks whoever's loudest — local or remote.
  // We use frequency-domain data for a real spectrum-bar look, log-scale it
  // (so quiet speech still moves the bars), and pre-bin into SPECTRUM_BANDS
  // here so the canvas draw loop is cheap.
  const wireAnalyser = useCallback((stream: MediaStream | null) => {
    if (sourceRef.current) {
      try {
        sourceRef.current.disconnect();
      } catch {
        /* ignore */
      }
      sourceRef.current = null;
    }
    if (!stream) return;
    if (!audioCtxRef.current) {
      const Ctx =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      audioCtxRef.current = new Ctx();
    }
    if (!analyserRef.current) {
      const a = audioCtxRef.current.createAnalyser();
      a.fftSize = 1024;
      a.smoothingTimeConstant = 0.6;
      a.minDecibels = -85; // sensitivity floor: lower = more sensitive
      a.maxDecibels = -25;
      analyserRef.current = a;
    }
    const src = audioCtxRef.current.createMediaStreamSource(stream);
    src.connect(analyserRef.current);
    sourceRef.current = src;
  }, []);

  // RAF loop: pull frequency data, bin into SPECTRUM_BANDS, log-scale, and
  // smooth toward zero so the bars decay gracefully when the speaker pauses.
  useEffect(() => {
    if (!connected) {
      setSpectrum(new Array(SPECTRUM_BANDS).fill(0));
      return;
    }
    const smoothed = new Array(SPECTRUM_BANDS).fill(0);
    const tick = () => {
      const a = analyserRef.current;
      if (a) {
        const bins = new Uint8Array(a.frequencyBinCount);
        a.getByteFrequencyData(bins);
        // Log-spaced binning: human voice is concentrated in the lower
        // frequencies, so a log scale gives a more visually useful spread.
        const N = bins.length;
        const next: number[] = new Array(SPECTRUM_BANDS);
        for (let i = 0; i < SPECTRUM_BANDS; i++) {
          const lo = Math.floor(Math.pow(i / SPECTRUM_BANDS, 2) * N);
          const hi = Math.max(lo + 1, Math.floor(Math.pow((i + 1) / SPECTRUM_BANDS, 2) * N));
          let sum = 0;
          for (let j = lo; j < hi; j++) sum += bins[j];
          next[i] = sum / (hi - lo) / 255;
        }
        // One-pole low-pass per band — fast attack, slow decay so bars fall
        // smoothly rather than snapping to zero on silence frames.
        for (let i = 0; i < SPECTRUM_BANDS; i++) {
          const target = next[i];
          smoothed[i] = target > smoothed[i]
            ? smoothed[i] + (target - smoothed[i]) * 0.55
            : smoothed[i] * 0.86;
        }
        setSpectrum(smoothed.slice());
      } else {
        // Decay toward zero even when we're not wired to a stream.
        for (let i = 0; i < SPECTRUM_BANDS; i++) smoothed[i] *= 0.9;
        if (smoothed.some((v) => v > 0.001)) setSpectrum(smoothed.slice());
      }
      rafRef.current = window.requestAnimationFrame(tick);
    };
    rafRef.current = window.requestAnimationFrame(tick);
    return () => {
      if (rafRef.current) window.cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [connected]);

  const connect = useCallback(async () => {
    if (!isAuthenticated()) return;
    if (roomRef.current && roomRef.current.state !== ConnectionState.Disconnected) return;
    setConnecting(true);
    try {
      const cfg = await api.tiCafeToken();
      const room = new Room({
        adaptiveStream: false,
        dynacast: true,
        // Browser-native feedback prevention. Without these the bare
        // livekit-client SDK publishes raw mic audio, which is what caused
        // the howl when two participants were on the same machine.
        audioCaptureDefaults: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        publishDefaults: {
          // Opus DTX (discontinuous transmission) suppresses packets during
          // silence — extra protection against ambient noise being sent.
          dtx: true,
          red: true,
          audioPreset: { maxBitrate: 32_000 },
        },
      });
      roomRef.current = room;

      room.on(RoomEvent.ConnectionStateChanged, (state) => {
        setConnected(state === ConnectionState.Connected);
        setConnecting(state === ConnectionState.Connecting || state === ConnectionState.Reconnecting);
      });
      const refreshCount = () => {
        const total = (room.numParticipants ?? room.remoteParticipants.size) + 1;
        setParticipantCount(total);
      };
      room.on(RoomEvent.ParticipantConnected, refreshCount);
      room.on(RoomEvent.ParticipantDisconnected, refreshCount);

      // Render remote audio. The bare livekit-client SDK does NOT auto-attach
      // tracks the way @livekit/components-react does; we have to call
      // track.attach() ourselves and put the resulting <audio> in the DOM
      // (otherwise no sound plays). Each element gets a data-attribute so
      // setSpeaker() can mute them all in one query.
      const onTrackSubscribed = (
        track: RemoteTrack,
        _pub: RemoteTrackPublication,
        participant: RemoteParticipant
      ) => {
        if (track.kind !== Track.Kind.Audio) return;
        const el = track.attach() as HTMLAudioElement;
        el.dataset.tiCafeAudio = "1";
        el.dataset.tiCafeIdentity = participant.identity;
        el.autoplay = true;
        el.muted = !speakerOnRef.current;
        // Detach from layout — these are pure audio elements.
        el.style.display = "none";
        document.body.appendChild(el);
      };
      const onTrackUnsubscribed = (
        track: RemoteTrack,
        _pub: RemoteTrackPublication,
        _participant: RemoteParticipant
      ) => {
        if (track.kind !== Track.Kind.Audio) return;
        for (const el of track.detach()) {
          el.remove();
        }
      };
      room.on(RoomEvent.TrackSubscribed, onTrackSubscribed);
      room.on(RoomEvent.TrackUnsubscribed, onTrackUnsubscribed);

      room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
        // Pick the loudest current speaker — local OR remote — and tap their
        // mic stream into the analyser. LiveKit's sortedActiveSpeakers is
        // ordered by audioLevel, so taking [0] gives the loudest.
        const top = speakers.find((s) => s.isSpeaking);
        if (!top) {
          wireAnalyser(null);
          return;
        }
        const pub = top.getTrackPublication(Track.Source.Microphone);
        const track = pub?.track as RemoteAudioTrack | undefined;
        const ms = track?.mediaStream;
        if (ms) wireAnalyser(ms);
      });
      room.on(RoomEvent.Disconnected, () => {
        setConnected(false);
        setParticipantCount(0);
        setSelfIdentity(null);
        wireAnalyser(null);
        // Sweep up any remaining audio elements we created.
        document
          .querySelectorAll<HTMLAudioElement>("audio[data-ti-cafe-audio]")
          .forEach((el) => el.remove());
      });

      await room.connect(cfg.livekit_url, cfg.token, {
        autoSubscribe: true,
      });
      setSelfIdentity(room.localParticipant.identity);
      // Apply current mic/speaker preferences.
      await room.localParticipant.setMicrophoneEnabled(micOn);
      // Speaker = global mute on the audio renderer; LiveKit doesn't expose a
      // single switch, so we leave actual <audio> elements playing and toggle
      // their volume via setSpeaker().
      refreshCount();
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("TI Café connect failed:", e);
      try {
        await roomRef.current?.disconnect();
      } catch {
        /* ignore */
      }
      roomRef.current = null;
      setConnected(false);
      setSelfIdentity(null);
    } finally {
      setConnecting(false);
    }
  }, [micOn, wireAnalyser]);

  const disconnect = useCallback(() => {
    const room = roomRef.current;
    roomRef.current = null;
    setConnected(false);
    setParticipantCount(0);
    setSelfIdentity(null);
    wireAnalyser(null);
    if (room) {
      void room.disconnect().catch(() => {
        /* ignore */
      });
    }
  }, [wireAnalyser]);

  const setMic = useCallback(
    (on: boolean) => {
      setMicOn(on);
      const room = roomRef.current;
      if (room && room.state === ConnectionState.Connected) {
        void room.localParticipant.setMicrophoneEnabled(on).catch(() => {
          /* if it fails (e.g. permission denied), keep the user-visible state */
        });
      }
    },
    []
  );

  const setSpeaker = useCallback((on: boolean) => {
    setSpeakerOn(on);
    // Mute every <audio> element we attached for TI Café tracks.
    document
      .querySelectorAll<HTMLAudioElement>("audio[data-ti-cafe-audio]")
      .forEach((el) => {
        el.muted = !on;
      });
  }, []);

  // Listen for the explicit logout event so we tear down the connection only
  // on log-off (per spec).
  useEffect(() => {
    const onLogout = () => disconnect();
    window.addEventListener("ti-cafe-logout", onLogout);
    return () => window.removeEventListener("ti-cafe-logout", onLogout);
  }, [disconnect]);

  // Tear down on unmount of the entire app (rare; reload counts).
  useEffect(() => {
    return () => {
      const room = roomRef.current;
      if (room) {
        void room.disconnect().catch(() => {
          /* ignore */
        });
      }
      const ctx = audioCtxRef.current;
      if (ctx) {
        void ctx.close().catch(() => {
          /* ignore */
        });
      }
    };
  }, []);

  const value = useMemo<TICafeContextShape>(
    () => ({
      connected,
      connecting,
      micOn,
      speakerOn,
      spectrum,
      participantCount,
      selfIdentity,
      connect,
      disconnect,
      setMic,
      setSpeaker,
    }),
    [connected, connecting, micOn, speakerOn, spectrum, participantCount, selfIdentity, connect, disconnect, setMic, setSpeaker]
  );

  return <TICafeContext.Provider value={value}>{children}</TICafeContext.Provider>;
}

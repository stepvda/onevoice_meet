/**
 * Café audio context — a single, app-global LiveKit Room that survives
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
const SS_VOLUME = "ti-cafe:volume"; // 0..1; "0" means muted
// Persisted across browser sessions: did the user toggle Café "on"? If
// so, we re-establish the connection on every page load (and on reconnects
// after a network drop). Cleared only on explicit user disconnect or logout.
// localStorage rather than sessionStorage so it survives a full browser quit
// — the audio room is meant to feel "always on" while the user is signed in.
const LS_DESIRED = "ti-cafe:desired-on";

// Reconnect backoff schedule (ms). The last value caps repeats.
const RECONNECT_BACKOFF_MS = [800, 1500, 3000, 6000, 12000, 30000];
// How long after Provider mount we'll keep polling for an SSO token before
// giving up the auto-rejoin attempt.
const AUTH_WAIT_MS = 5000;
const AUTH_POLL_MS = 250;

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

function readVolume(fallback: number): number {
  try {
    const v = sessionStorage.getItem(SS_VOLUME);
    if (v === null) return fallback;
    const n = Number(v);
    if (!Number.isFinite(n)) return fallback;
    return Math.max(0, Math.min(1, n));
  } catch {
    return fallback;
  }
}

function writeVolume(value: number): void {
  try {
    sessionStorage.setItem(SS_VOLUME, String(value));
  } catch {
    /* ignore */
  }
}

function readDesired(): boolean {
  try {
    return localStorage.getItem(LS_DESIRED) === "1";
  } catch {
    return false;
  }
}

function writeDesired(value: boolean): void {
  try {
    if (value) localStorage.setItem(LS_DESIRED, "1");
    else localStorage.removeItem(LS_DESIRED);
  } catch {
    /* ignore */
  }
}

/** Number of spectrum bands the bar's waveform consumes. */
export const SPECTRUM_BANDS = 24;

interface TICafeContextShape {
  connected: boolean;
  connecting: boolean;
  /** Mic publishing — false = muted (slash overlay in UI). */
  micOn: boolean;
  /** Output audio level. 0 = muted, 1 = full volume. The bar's vertical
   *  slider drives this. */
  volume: number;
  /** Total participant count (including self when connected). */
  participantCount: number;
  /** Self LiveKit identity, e.g. "user-42", once connected. */
  selfIdentity: string | null;
  connect: () => Promise<void>;
  disconnect: () => void;
  setMic: (on: boolean) => void;
  setVolume: (v: number) => void;
}

const TICafeContext = createContext<TICafeContextShape | null>(null);
// The spectrum updates ~60×/sec while audio is flowing. Keep it on its own
// context so the bar's visualizer is the only consumer that re-renders at
// that rate; the rest of the app uses the stable control context above.
const TICafeSpectrumContext = createContext<number[] | null>(null);

export function useTICafe(): TICafeContextShape {
  const ctx = useContext(TICafeContext);
  if (!ctx) throw new Error("useTICafe must be used inside <TICafeProvider>");
  return ctx;
}

export function useTICafeSpectrum(): number[] {
  const ctx = useContext(TICafeSpectrumContext);
  if (!ctx) throw new Error("useTICafeSpectrum must be used inside <TICafeProvider>");
  return ctx;
}

export function TICafeProvider({ children }: { children: ReactNode }) {
  const roomRef = useRef<Room | null>(null);
  const rafRef = useRef<number | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  // Reconnect bookkeeping. attemptRef tracks the next backoff index; both are
  // reset to zero on a clean Connected and on user-initiated disconnect.
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef<number>(0);

  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [micOn, setMicOn] = useState<boolean>(() => readBool(SS_MIC, false));
  const [volume, setVolumeState] = useState<number>(() => readVolume(1));
  const [spectrum, setSpectrum] = useState<number[]>(() => new Array(SPECTRUM_BANDS).fill(0));
  const [participantCount, setParticipantCount] = useState(0);
  const [selfIdentity, setSelfIdentity] = useState<string | null>(null);
  // Mirror of volume that the TrackSubscribed handler can read without
  // capturing a stale closure each time the listener was registered.
  const volumeRef = useRef<number>(volume);
  // micOn mirror — kept so connect() can read the latest mic preference
  // without itself depending on micOn (which would rebuild the callback each
  // time the user toggles mic, churning the reconnect timer).
  const micOnRef = useRef<boolean>(micOn);

  // Persist preferences across in-app navigation (same tab session).
  useEffect(() => writeBool(SS_MIC, micOn), [micOn]);
  useEffect(() => writeVolume(volume), [volume]);
  useEffect(() => {
    volumeRef.current = volume;
  }, [volume]);
  useEffect(() => {
    micOnRef.current = micOn;
  }, [micOn]);

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

  // Schedule a reconnect with exponential backoff. Bails if the user has
  // disconnected (desired flag cleared) or signed out in the meantime.
  // Defined as a regular function (not a useCallback) so it always reads the
  // latest refs without churning the event handlers that capture it.
  function scheduleReconnect() {
    if (!readDesired() || !isAuthenticated()) return;
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
    }
    const idx = Math.min(reconnectAttemptRef.current, RECONNECT_BACKOFF_MS.length - 1);
    const delay = RECONNECT_BACKOFF_MS[idx];
    reconnectAttemptRef.current += 1;
    reconnectTimerRef.current = window.setTimeout(() => {
      reconnectTimerRef.current = null;
      // Re-check desired in case the user clicked Disconnect while we waited.
      if (readDesired()) void connect();
    }, delay);
  }

  const connect = useCallback(async () => {
    if (!isAuthenticated()) return;
    if (roomRef.current && roomRef.current.state !== ConnectionState.Disconnected) return;
    // Mark intent immediately. If this attempt fails mid-flight, the
    // backoff loop should still try again — user clicking Connect already
    // committed to the room.
    writeDesired(true);
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    setConnecting(true);
    try {
      const cfg = await api.tiCafeToken();
      // The user may have clicked Disconnect during the token round-trip.
      // If so, abandon — don't construct a Room we'd then have to tear down.
      if (!readDesired()) {
        setConnecting(false);
        return;
      }
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
        if (state === ConnectionState.Connected) {
          // Successful (re)connect — reset the backoff counter so the next
          // accidental drop starts fresh.
          reconnectAttemptRef.current = 0;
        }
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
        el.volume = volumeRef.current;
        el.muted = volumeRef.current === 0;
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
        // The Room is dead. If the user still wants to be in the café,
        // schedule a reconnect; if they explicitly clicked Disconnect (or
        // logged out), the desired flag is already cleared and we bail.
        roomRef.current = null;
        scheduleReconnect();
      });

      await room.connect(cfg.livekit_url, cfg.token, {
        autoSubscribe: true,
      });
      // Late-disconnect check: if disconnect() ran while we were inside
      // room.connect(), tear this room down instead of leaving it live.
      if (!readDesired() || roomRef.current !== room) {
        try {
          await room.disconnect();
        } catch {
          /* ignore */
        }
        if (roomRef.current === room) roomRef.current = null;
        return;
      }
      setSelfIdentity(room.localParticipant.identity);
      // Apply current mic/speaker preferences. Use the ref so reconnects
      // (which run inside a callback captured at room-creation time) read
      // the latest user preference rather than a stale value.
      await room.localParticipant.setMicrophoneEnabled(micOnRef.current);
      // Speaker = global mute on the audio renderer; LiveKit doesn't expose a
      // single switch, so we leave actual <audio> elements playing and toggle
      // their volume via setSpeaker().
      refreshCount();
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Café connect failed:", e);
      try {
        await roomRef.current?.disconnect();
      } catch {
        /* ignore */
      }
      roomRef.current = null;
      setConnected(false);
      setSelfIdentity(null);
      // Schedule a backoff retry. Desired is still set (we set it at the
      // top of connect), so this kicks the auto-reconnect loop.
      scheduleReconnect();
    } finally {
      setConnecting(false);
    }
  }, [wireAnalyser]);

  const disconnect = useCallback(() => {
    // User-initiated disconnect: clear the persisted intent FIRST so the
    // Room.Disconnected handler that fires below sees desired=false and
    // doesn't schedule a reconnect.
    writeDesired(false);
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    reconnectAttemptRef.current = 0;
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

  const setVolume = useCallback((v: number) => {
    const clamped = Math.max(0, Math.min(1, v));
    setVolumeState(clamped);
    // Apply to every <audio> element we attached for Café tracks. Setting
    // both `volume` and `muted` means a slider at 0 produces guaranteed
    // silence even on browsers that floor very low volumes.
    document
      .querySelectorAll<HTMLAudioElement>("audio[data-ti-cafe-audio]")
      .forEach((el) => {
        el.volume = clamped;
        el.muted = clamped === 0;
      });
  }, []);

  // Listen for the explicit logout event so we tear down the connection only
  // on log-off (per spec).
  useEffect(() => {
    const onLogout = () => disconnect();
    window.addEventListener("ti-cafe-logout", onLogout);
    return () => window.removeEventListener("ti-cafe-logout", onLogout);
  }, [disconnect]);

  // Auto-rejoin on app mount if the user was last seen connected. Polls for
  // an SSO token for up to AUTH_WAIT_MS so the rejoin works even when
  // bootstrapFromOneWitysk in App.tsx hasn't finished writing the token to
  // localStorage yet on first paint.
  useEffect(() => {
    if (!readDesired()) return;
    let cancelled = false;
    const start = Date.now();
    let timer: number | null = null;

    const tryConnect = () => {
      if (cancelled) return;
      if (isAuthenticated()) {
        void connect();
        return;
      }
      if (Date.now() - start > AUTH_WAIT_MS) return; // give up; user can click toggle
      timer = window.setTimeout(tryConnect, AUTH_POLL_MS);
    };

    // Tiny delay so other mount effects (App-level SSO bootstrap) get a head
    // start. The poll above will pick up the token as soon as it appears.
    timer = window.setTimeout(tryConnect, 200);
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
    // Intentionally one-shot on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Tear down on unmount of the entire app (rare; reload counts).
  useEffect(() => {
    return () => {
      if (reconnectTimerRef.current) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
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
      volume,
      participantCount,
      selfIdentity,
      connect,
      disconnect,
      setMic,
      setVolume,
    }),
    [connected, connecting, micOn, volume, participantCount, selfIdentity, connect, disconnect, setMic, setVolume]
  );

  return (
    <TICafeContext.Provider value={value}>
      <TICafeSpectrumContext.Provider value={spectrum}>
        {children}
      </TICafeSpectrumContext.Provider>
    </TICafeContext.Provider>
  );
}

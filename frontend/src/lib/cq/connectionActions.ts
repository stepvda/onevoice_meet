import {
  LocalVideoTrack,
  Room,
  Track,
  VideoQuality,
  type RemoteTrackPublication,
  type Room as RoomType,
} from "livekit-client";
import type { TrackReference } from "@livekit/components-react";
import { getCqConnection, beginCqReconnect, consumeCqReconnecting } from "./reconnect";
import { useCqStore } from "./connectionQualityStore";
import type { CompressionPreset, QualityMode, TileQualityOverride } from "./types";

export class CqActionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CqActionError";
  }
}

export type CqAction =
  | { kind: "resync" }
  | { kind: "reconnect" }
  | { kind: "quality"; quality: QualityMode; size?: { width: number; height: number } }
  | { kind: "audioOnly"; enabled: boolean }
  | { kind: "resolution"; width: number; height: number; frameRate?: number }
  | { kind: "maxBitrate"; kbps: number }
  | { kind: "compression"; preset: CompressionPreset }
  | { kind: "colorDepth"; depth: 8 | 10 };

const QUALITY_MAP: Record<Exclude<QualityMode, "auto">, VideoQuality> = {
  low: VideoQuality.LOW,
  medium: VideoQuality.MEDIUM,
  high: VideoQuality.HIGH,
};

const COMPRESSION_PROFILE: Record<
  CompressionPreset,
  { hint: "motion" | "detail"; degradation: RTCDegradationPreference; factor: number }
> = {
  low: { hint: "motion", degradation: "maintain-framerate", factor: 0.6 },
  medium: { hint: "motion", degradation: "balanced", factor: 1 },
  high: { hint: "detail", degradation: "maintain-resolution", factor: 1.15 },
};

interface AdaptiveTrack {
  adaptiveStreamSettings?: unknown;
  on: (event: string, handler: (...args: unknown[]) => void) => void;
  off: (event: string, handler: (...args: unknown[]) => void) => void;
  updateDimensions?: () => void;
  updateVisibility?: () => void;
}

interface SavedAdaptive {
  pub: RemoteTrackPublication;
  track: AdaptiveTrack;
  settings: unknown;
  dimensionsHandler?: (...args: unknown[]) => void;
  visibilityHandler?: (...args: unknown[]) => void;
}

const savedAdaptive = new Map<string, SavedAdaptive>();

/**
 * Manual subscription controls (setVideoQuality / setEnabled) are refused by
 * livekit-client while adaptiveStream drives the track. Pinning detaches the
 * adaptive handlers for this one publication so the user's choice sticks;
 * releaseVideoControl restores adaptive streaming when the tile returns to
 * "Auto".
 */
export function pinVideoControl(trackKey: string, pub: RemoteTrackPublication): boolean {
  const track = pub.track as unknown as AdaptiveTrack | undefined;
  if (!track) return false;
  const existing = savedAdaptive.get(trackKey);
  if (existing && existing.pub === pub && existing.track === track) return false;
  if (existing) savedAdaptive.delete(trackKey);
  if (track.adaptiveStreamSettings === undefined) return false;

  const pubAny = pub as unknown as {
    handleVideoDimensionsChange?: (...args: unknown[]) => void;
    handleVisibilityChange?: (...args: unknown[]) => void;
    videoDimensionsAdaptiveStream?: unknown;
    videoDimensions?: unknown;
  };
  const saved: SavedAdaptive = {
    pub,
    track,
    settings: track.adaptiveStreamSettings,
    dimensionsHandler: pubAny.handleVideoDimensionsChange,
    visibilityHandler: pubAny.handleVisibilityChange,
  };
  try {
    if (saved.dimensionsHandler) track.off("videoDimensionsChanged", saved.dimensionsHandler);
    if (saved.visibilityHandler) track.off("visibilityChanged", saved.visibilityHandler);
    track.adaptiveStreamSettings = undefined;
    pubAny.videoDimensionsAdaptiveStream = undefined;
    pubAny.videoDimensions = undefined;
  } catch {
    return false;
  }
  savedAdaptive.set(trackKey, saved);
  return true;
}

export function releaseVideoControl(trackKey: string, pub: RemoteTrackPublication): void {
  const saved = savedAdaptive.get(trackKey);
  if (!saved || saved.pub !== pub) return;
  savedAdaptive.delete(trackKey);
  const track = pub.track as unknown as AdaptiveTrack | undefined;
  if (!track || saved.track !== track) return;
  try {
    track.adaptiveStreamSettings = saved.settings;
    if (saved.dimensionsHandler) track.on("videoDimensionsChanged", saved.dimensionsHandler);
    if (saved.visibilityHandler) track.on("visibilityChanged", saved.visibilityHandler);
    track.updateDimensions?.();
    track.updateVisibility?.();
  } catch {
    return;
  }
}

function remotePublication(ref: TrackReference): RemoteTrackPublication {
  const pub = ref.publication as RemoteTrackPublication | undefined;
  if (!pub || typeof pub.setVideoQuality !== "function") {
    throw new CqActionError("This tile is not a subscribable video track");
  }
  return pub;
}

function attachedSize(pub: RemoteTrackPublication): { width: number; height: number } | null {
  const element = pub.track?.attachedElements?.[0];
  if (!element) return null;
  const el = element as HTMLElement & { videoWidth?: number; videoHeight?: number };
  const width = el.clientWidth || el.videoWidth || 0;
  const height = el.clientHeight || el.videoHeight || 0;
  if (width < 16 || height < 16) return null;
  return { width, height };
}

function localVideoTrack(room: RoomType, ref: TrackReference): LocalVideoTrack {
  const pub = room.localParticipant.getTrackPublication(Track.Source.Camera);
  const track = pub?.track;
  if (ref.participant.isLocal && track instanceof LocalVideoTrack) return track;
  throw new CqActionError("No active camera track on your tile");
}

async function setSenderEncoding(
  track: LocalVideoTrack,
  patch: (params: RTCRtpSendParameters) => void,
): Promise<void> {
  const sender = track.sender;
  if (!sender) throw new CqActionError("This browser does not expose encoder controls");
  const params = sender.getParameters();
  if (!params.encodings || params.encodings.length === 0) params.encodings = [{}];
  patch(params);
  await sender.setParameters(params);
}

async function waitForConnected(room: Room, timeoutMs: number): Promise<boolean> {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (room.state === "connected") return true;
    await new Promise((resolve) => window.setTimeout(resolve, 200));
  }
  return room.state === "connected";
}

export async function runAction(
  room: Room,
  ref: TrackReference,
  action: CqAction,
): Promise<void> {
  const started = Date.now();
  const trackKey = `${ref.participant.identity}-${ref.source ?? ""}`;
  const store = useCqStore.getState();
  try {
    switch (action.kind) {
      case "resync": {
        const pub = remotePublication(ref);
        pub.setSubscribed(false);
        await new Promise((resolve) => window.setTimeout(resolve, 350));
        pub.setSubscribed(true);
        break;
      }
      case "reconnect": {
        const conn = getCqConnection();
        if (!conn) throw new CqActionError("Connection details are no longer available");
        beginCqReconnect();
        try {
          await room.disconnect();
          await room.connect(conn.serverUrl, conn.token, conn.connectOptions);
        } catch {
          // Fall through to the state check below; a reload re-joins cleanly.
        }
        const connected = await waitForConnected(room, 5000);
        consumeCqReconnecting();
        if (!connected) {
          window.location.reload();
          return;
        }
        break;
      }
      case "quality": {
        const pub = remotePublication(ref);
        if (action.quality === "auto") {
          const size = attachedSize(pub);
          if (size) pub.setVideoDimensions(size);
          else {
            const resettable = pub as unknown as { setVideoQuality?: (q?: VideoQuality) => void };
            resettable.setVideoQuality?.(undefined);
          }
          releaseVideoControl(trackKey, pub);
          store.clearOverride(trackKey);
          break;
        }
        pinVideoControl(trackKey, pub);
        const target = QUALITY_MAP[action.quality];
        pub.setVideoQuality(target);
        if (pub.videoQuality !== target) {
          throw new CqActionError("The stream is not ready yet — try again in a moment");
        }
        const override: TileQualityOverride = {
          trackKey,
          videoQuality: action.quality,
          source: "manual",
          updatedAt: Date.now(),
        };
        store.setOverride(override);
        break;
      }
      case "audioOnly": {
        const pub = remotePublication(ref);
        pinVideoControl(trackKey, pub);
        pub.setEnabled(!action.enabled);
        if (pub.isEnabled !== !action.enabled) {
          throw new CqActionError("The stream is not ready yet — try again in a moment");
        }
        break;
      }
      case "resolution": {
        const track = localVideoTrack(room, ref);
        const ms = track.mediaStreamTrack;
        await ms.applyConstraints({
          width: { ideal: action.width },
          height: { ideal: action.height },
          frameRate: action.frameRate ? { ideal: action.frameRate } : undefined,
        });
        const settings = ms.getSettings();
        const override: TileQualityOverride = {
          trackKey,
          videoQuality: "auto",
          source: "manual",
          resolution: {
            width: settings.width ?? action.width,
            height: settings.height ?? action.height,
            frameRate: settings.frameRate,
          },
          updatedAt: Date.now(),
        };
        store.setOverride(override);
        break;
      }
      case "maxBitrate": {
        const track = localVideoTrack(room, ref);
        await setSenderEncoding(track, (params) => {
          for (const enc of params.encodings) enc.maxBitrate = action.kbps * 1000;
        });
        const override: TileQualityOverride = {
          trackKey,
          videoQuality: "auto",
          source: "manual",
          maxBitrateKbps: action.kbps,
          updatedAt: Date.now(),
        };
        store.setOverride(override);
        break;
      }
      case "compression": {
        const track = localVideoTrack(room, ref);
        const profile = COMPRESSION_PROFILE[action.preset];
        try {
          track.mediaStreamTrack.contentHint = profile.hint;
        } catch {
          // contentHint is advisory; some browsers ignore it.
        }
        await setSenderEncoding(track, (params) => {
          params.degradationPreference = profile.degradation;
          for (const enc of params.encodings) {
            const base = enc.maxBitrate ?? 1_200_000;
            enc.maxBitrate = Math.round(base * profile.factor);
          }
        });
        const override: TileQualityOverride = {
          trackKey,
          videoQuality: "auto",
          source: "manual",
          compression: action.preset,
          updatedAt: Date.now(),
        };
        store.setOverride(override);
        break;
      }
      case "colorDepth": {
        if (action.depth === 10) {
          const caps = RTCRtpSender.getCapabilities?.("video");
          const hasTenBitCandidate = !!caps?.codecs?.some(
            (c) => c.mimeType === "video/AV1" || c.mimeType === "video/VP9",
          );
          if (!hasTenBitCandidate) {
            throw new CqActionError("10-bit is not available on this device or codec");
          }
          throw new CqActionError("10-bit capture is not enabled in this build");
        }
        const override: TileQualityOverride = {
          trackKey,
          videoQuality: "auto",
          source: "manual",
          colorDepth: 8,
          updatedAt: Date.now(),
        };
        store.setOverride(override);
        break;
      }
    }
    store.log({ at: Date.now(), action: action.kind, result: "ok", durationMs: Date.now() - started });
  } catch (error) {
    store.log({
      at: Date.now(),
      action: action.kind,
      result: "error",
      detail: error instanceof Error ? error.message : String(error),
      durationMs: Date.now() - started,
    });
    throw error;
  }
}

export function reapplyVideoOverride(trackKey: string, ref: TrackReference): void {
  const override = useCqStore.getState().overrides[trackKey];
  if (!override || override.videoQuality === "auto") return;
  const pub = ref.publication as RemoteTrackPublication | undefined;
  if (!pub || typeof pub.setVideoQuality !== "function") return;
  pinVideoControl(trackKey, pub);
  pub.setVideoQuality(QUALITY_MAP[override.videoQuality]);
}

export function clearTileOverride(trackKey: string): void {
  useCqStore.getState().clearOverride(trackKey);
}

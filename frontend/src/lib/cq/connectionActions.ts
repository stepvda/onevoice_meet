import {
  LocalVideoTrack,
  Room,
  Track,
  VideoQuality,
  type RemoteTrackPublication,
  type Room as RoomType,
} from "livekit-client";
import type { TrackReference } from "@livekit/components-react";
import { getCqConnection, beginCqReconnect } from "./reconnect";
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

function remotePublication(ref: TrackReference): RemoteTrackPublication {
  const pub = ref.publication as RemoteTrackPublication | undefined;
  if (!pub || typeof pub.setVideoQuality !== "function") {
    throw new CqActionError("not a remote video publication");
  }
  return pub;
}

function localVideoTrack(room: RoomType, ref: TrackReference): LocalVideoTrack {
  const pub = room.localParticipant.getTrackPublication(Track.Source.Camera);
  const track = pub?.track;
  if (ref.participant.isLocal && track instanceof LocalVideoTrack) return track;
  throw new CqActionError("no local camera track");
}

async function setSenderEncoding(
  track: LocalVideoTrack,
  patch: (params: RTCRtpSendParameters) => void,
): Promise<void> {
  const sender = track.sender;
  if (!sender) throw new CqActionError("no sender");
  const params = sender.getParameters();
  if (!params.encodings || params.encodings.length === 0) params.encodings = [{}];
  patch(params);
  await sender.setParameters(params);
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
        if (!conn) throw new CqActionError("missing connection credentials");
        beginCqReconnect();
        await room.disconnect();
        await room.connect(conn.serverUrl, conn.token, conn.connectOptions);
        break;
      }
      case "quality": {
        if (action.quality === "auto") {
          const pub = remotePublication(ref);
          if (action.size) pub.setVideoDimensions(action.size);
          store.clearOverride(trackKey);
          break;
        }
        const pub = remotePublication(ref);
        pub.setVideoQuality(QUALITY_MAP[action.quality]);
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
        pub.setEnabled(!action.enabled);
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

export function clearTileOverride(trackKey: string): void {
  useCqStore.getState().clearOverride(trackKey);
}

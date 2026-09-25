import { AudioPresets, LocalVideoTrack, Room, Track } from "livekit-client";
import { usePreferences } from "../preferences";

export interface CameraCapabilities {
  maxWidth?: number;
  maxHeight?: number;
  maxFrameRate?: number;
  aspectRatio?: number;
}

export interface CaptureSettings {
  width?: number;
  height?: number;
  frameRate?: number;
}

export interface MediaDeviceOption {
  deviceId: string;
  label: string;
}

export interface DeviceLists {
  cameras: MediaDeviceOption[];
  microphones: MediaDeviceOption[];
}

export type MicChannelMode = "mono" | "stereo";
export type AudioQualityMode = "voice" | "music";

export function localCameraTrack(room: Room): LocalVideoTrack | null {
  const pub = room.localParticipant.getTrackPublication(Track.Source.Camera);
  const track = pub?.track;
  return track instanceof LocalVideoTrack ? track : null;
}

export function readCameraCapabilities(room: Room): CameraCapabilities {
  const track = localCameraTrack(room);
  if (!track) return {};
  const caps = track.mediaStreamTrack.getCapabilities() as MediaTrackCapabilities & {
    aspectRatio?: { max?: number };
  };
  return {
    maxWidth: caps.width?.max,
    maxHeight: caps.height?.max,
    maxFrameRate: caps.frameRate?.max,
    aspectRatio: caps.aspectRatio?.max,
  };
}

export function readCaptureSettings(room: Room): CaptureSettings {
  const track = localCameraTrack(room);
  if (!track) return {};
  const settings = track.mediaStreamTrack.getSettings();
  return {
    width: settings.width,
    height: settings.height,
    frameRate: settings.frameRate,
  };
}

export async function applyCameraPreset(
  room: Room,
  preset: { width: number; height: number; frameRate?: number },
): Promise<CaptureSettings> {
  const track = localCameraTrack(room);
  if (!track) throw new Error("no local camera track");
  await track.mediaStreamTrack.applyConstraints({
    width: { ideal: preset.width },
    height: { ideal: preset.height },
    frameRate: preset.frameRate ? { ideal: preset.frameRate } : undefined,
  });
  return readCaptureSettings(room);
}

export async function listMediaDevices(): Promise<DeviceLists> {
  if (!navigator.mediaDevices?.enumerateDevices) return { cameras: [], microphones: [] };
  const devices = await navigator.mediaDevices.enumerateDevices();
  const shape = (kind: MediaDeviceKind): MediaDeviceOption[] =>
    devices
      .filter((d) => d.kind === kind)
      .map((d, index) => ({
        deviceId: d.deviceId,
        label: d.label || `${kind === "videoinput" ? "Camera" : "Microphone"} ${index + 1}`,
      }));
  return { cameras: shape("videoinput"), microphones: shape("audioinput") };
}

export async function switchDevice(
  room: Room,
  kind: "videoinput" | "audioinput",
  deviceId: string,
): Promise<void> {
  const ok = await room.switchActiveDevice(kind, deviceId);
  if (!ok) throw new Error("device switch failed");
  const prefs = usePreferences.getState();
  if (kind === "videoinput") prefs.setAv({ preferredCameraId: deviceId });
  else prefs.setAv({ preferredMicId: deviceId });
}

export async function setMicrophoneMode(
  room: Room,
  mode: MicChannelMode,
  quality: AudioQualityMode,
): Promise<void> {
  const stereo = mode === "stereo";
  const hifi = quality === "music" || stereo;
  await room.localParticipant.setMicrophoneEnabled(false);
  await room.localParticipant.setMicrophoneEnabled(
    true,
    {
      channelCount: stereo ? 2 : 1,
      echoCancellation: !hifi,
      noiseSuppression: !hifi,
      autoGainControl: !hifi,
    },
    hifi
      ? {
          audioPreset: AudioPresets.musicHighQualityStereo,
          dtx: false,
          red: false,
          forceStereo: stereo,
        }
      : {
          audioPreset: AudioPresets.speech,
          dtx: true,
          red: true,
          forceStereo: false,
        },
  );
  usePreferences.getState().setAv({ noiseSuppression: !hifi, echoCancellation: !hifi });
}

export function readMicrophoneSettings(room: Room): {
  channelCount?: number;
  sampleRate?: number;
} {
  const pub = room.localParticipant.getTrackPublication(Track.Source.Microphone);
  const track = pub?.track;
  if (!track) return {};
  const settings = track.mediaStreamTrack.getSettings() as MediaTrackSettings & {
    sampleRate?: number;
  };
  return { channelCount: settings.channelCount, sampleRate: settings.sampleRate };
}

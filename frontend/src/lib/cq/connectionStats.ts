import type { TrackReference } from "@livekit/components-react";
import type { ConnectionStatsSample } from "./types";

interface RawCounters {
  at: number;
  videoBytes?: number;
  videoPacketsLost?: number;
  videoPacketsReceived?: number;
  videoFramesDecoded?: number;
  videoFramesDropped?: number;
  audioBytes?: number;
  audioPacketsLost?: number;
  audioPacketsReceived?: number;
  audioConcealedSamples?: number;
  audioConcealed?: number;
  audioTotalSamplesDuration?: number;
  audioJitterBufferDelay?: number;
  audioJitterBufferEmitted?: number;
  sentBytes?: number;
}

interface Entry {
  ref: TrackReference;
  cadenceMs: number;
  refCount: number;
  timer: number | null;
  prev: RawCounters | null;
  samples: ConnectionStatsSample[];
  listeners: Set<(sample: ConnectionStatsSample) => void>;
}

const WATCH_CADENCE_MS = 1000;
const IDLE_CADENCE_MS = 2000;
const RING_SIZE = 120;

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function pct(delta: number, total: number): number {
  if (total <= 0) return 0;
  return Math.max(0, Math.min(100, (delta / total) * 100));
}

class StatsCollector {
  private entries = new Map<string, Entry>();
  private listeners = new Set<(sample: ConnectionStatsSample) => void>();

  subscribe(listener: (sample: ConnectionStatsSample) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  watch(trackKey: string, ref: TrackReference, cadenceMs = IDLE_CADENCE_MS): void {
    const existing = this.entries.get(trackKey);
    if (existing) {
      existing.ref = ref;
      existing.refCount += 1;
      this.setCadence(trackKey, cadenceMs);
      return;
    }
    const entry: Entry = {
      ref,
      cadenceMs,
      refCount: 1,
      timer: null,
      prev: null,
      samples: [],
      listeners: new Set(),
    };
    this.entries.set(trackKey, entry);
    this.schedule(trackKey, entry, 0);
  }

  unwatch(trackKey: string): void {
    const entry = this.entries.get(trackKey);
    if (!entry) return;
    entry.refCount -= 1;
    if (entry.refCount > 0) return;
    if (entry.timer !== null) window.clearTimeout(entry.timer);
    this.entries.delete(trackKey);
  }

  setCadence(trackKey: string, cadenceMs: number): void {
    const entry = this.entries.get(trackKey);
    if (!entry) return;
    entry.cadenceMs = cadenceMs;
    if (entry.timer !== null) {
      window.clearTimeout(entry.timer);
      entry.timer = null;
    }
    this.schedule(trackKey, entry, 200);
  }

  getSamples(trackKey: string): ConnectionStatsSample[] {
    return this.entries.get(trackKey)?.samples ?? [];
  }

  sampleOnce(trackKey: string): ConnectionStatsSample | undefined {
    const samples = this.entries.get(trackKey)?.samples;
    return samples && samples.length > 0 ? samples[samples.length - 1] : undefined;
  }

  async probeSamples(trackKey: string, durationMs: number): Promise<ConnectionStatsSample[]> {
    const entry = this.entries.get(trackKey);
    if (!entry) return [];
    const collected: ConnectionStatsSample[] = [];
    return new Promise((resolve) => {
      const listener = (sample: ConnectionStatsSample) => collected.push(sample);
      entry.listeners.add(listener);
      window.setTimeout(() => {
        entry.listeners.delete(listener);
        resolve(collected);
      }, durationMs);
    });
  }

  private schedule(trackKey: string, entry: Entry, delayMs: number): void {
    entry.timer = window.setTimeout(() => {
      void this.poll(trackKey, entry);
    }, delayMs);
  }

  private async poll(trackKey: string, entry: Entry): Promise<void> {
    if (!this.entries.has(trackKey)) return;
    if (typeof document !== "undefined" && document.hidden) {
      entry.timer = null;
      this.schedule(trackKey, entry, entry.cadenceMs);
      return;
    }
    try {
      const track = entry.ref.publication?.track;
      if (!track || track.isMuted) {
        entry.timer = null;
        this.schedule(trackKey, entry, entry.cadenceMs);
        return;
      }
      const sample = await this.collect(trackKey, entry, track);
      if (sample) {
        entry.samples.push(sample);
        if (entry.samples.length > RING_SIZE) entry.samples.shift();
        for (const listener of entry.listeners) listener(sample);
        for (const listener of this.listeners) listener(sample);
      }
    } catch {
      // Stats are best-effort; a failed report must never disturb the call.
    }
    entry.timer = null;
    this.schedule(trackKey, entry, entry.cadenceMs);
  }

  private async collect(
    trackKey: string,
    entry: Entry,
    rawTrack: NonNullable<TrackReference["publication"]>["track"],
  ): Promise<ConnectionStatsSample | null> {
    const track = rawTrack as unknown as {
      getRTCStatsReport?: () => Promise<RTCStatsReport | undefined>;
    };
    if (!rawTrack || typeof track.getRTCStatsReport !== "function") return null;
    const report = await track.getRTCStatsReport();
    if (!report) return null;

    const at = Date.now();
    const next: RawCounters = { at };
    let video:
      | {
          bytes?: number;
          packetsLost?: number;
          packetsReceived?: number;
          framesDecoded?: number;
          framesDropped?: number;
          jitterSec?: number;
          frameWidth?: number;
          frameHeight?: number;
          fps?: number;
          codecId?: string;
        }
      | undefined;
    let audio:
      | {
          bytes?: number;
          packetsLost?: number;
          packetsReceived?: number;
          concealedSamples?: number;
          totalSamplesDuration?: number;
          jitterBufferDelay?: number;
          jitterBufferEmitted?: number;
          jitterSec?: number;
          codecId?: string;
        }
      | undefined;
    let rttSec: number | undefined;
    let localCandidateId: string | undefined;
    const codecs = new Map<string, string>();
    const localCandidates = new Map<string, string>();

    (report as unknown as { forEach: (cb: (stat: unknown, key: string) => void) => void }).forEach((stat) => {
      const s = stat as Record<string, unknown>;
      const type = s.type as string | undefined;
      if (type === "codec" && typeof s.id === "string" && typeof s.mimeType === "string") {
        codecs.set(s.id, s.mimeType);
      } else if (type === "local-candidate" && typeof s.id === "string") {
        localCandidates.set(s.id, (s.candidateType as string | undefined) ?? "unknown");
      } else if (
        type === "inbound-rtp" &&
        s.kind === "video" &&
        (entry.ref.publication?.kind === "video" || !rawTrack.kind || rawTrack.kind === "video")
      ) {
        video = {
          bytes: num(s.bytesReceived),
          packetsLost: num(s.packetsLost),
          packetsReceived: num(s.packetsReceived),
          framesDecoded: num(s.framesDecoded),
          framesDropped: num(s.framesDropped),
          jitterSec: num(s.jitter),
          frameWidth: num(s.frameWidth),
          frameHeight: num(s.frameHeight),
          fps: num(s.framesPerSecond),
          codecId: s.codecId as string | undefined,
        };
      } else if (type === "inbound-rtp" && s.kind === "audio") {
        audio = {
          bytes: num(s.bytesReceived),
          packetsLost: num(s.packetsLost),
          packetsReceived: num(s.packetsReceived),
          concealedSamples: num(s.concealedSamples),
          totalSamplesDuration: num(s.totalSamplesDuration),
          jitterBufferDelay: num(s.jitterBufferDelay),
          jitterBufferEmitted: num(s.jitterBufferEmittedCount),
          jitterSec: num(s.jitter),
          codecId: s.codecId as string | undefined,
        };
      } else if (type === "outbound-rtp" && s.kind === "video" && !video) {
        video = {
          bytes: num(s.bytesSent),
          packetsLost: num(s.packetsLost),
          fps: num(s.framesPerSecond),
          frameWidth: num(s.frameWidth),
          frameHeight: num(s.frameHeight),
          codecId: s.codecId as string | undefined,
        };
      } else if (type === "outbound-rtp" && s.kind === "audio" && !audio) {
        audio = {
          bytes: num(s.bytesSent),
          packetsLost: num(s.packetsLost),
          codecId: s.codecId as string | undefined,
        };
      } else if (type === "candidate-pair" && (s.selected === true || s.nominated === true)) {
        rttSec = num(s.currentRoundTripTime) ?? rttSec;
        localCandidateId = (s.localCandidateId as string | undefined) ?? localCandidateId;
      } else if (type === "remote-inbound-rtp" && s.kind === "video" && rttSec === undefined) {
        rttSec = num(s.roundTripTime);
      }
    });

    const prev = entry.prev;
    const dtSec = prev ? Math.max(0, (at - prev.at) / 1000) : 0;
    const ok = dtSec >= 0.2;
    const delta = (curr?: number, old?: number): number | undefined =>
      curr !== undefined && old !== undefined && curr >= old ? curr - old : undefined;

    const videoBytes = video?.bytes;
    const videoBytesDelta = ok ? delta(videoBytes, prev?.videoBytes) : undefined;
    const videoLostDelta = ok ? delta(video?.packetsLost, prev?.videoPacketsLost) : undefined;
    const videoReceivedDelta = ok ? delta(video?.packetsReceived, prev?.videoPacketsReceived) : undefined;
    const framesDecodedDelta = ok ? delta(video?.framesDecoded, prev?.videoFramesDecoded) : undefined;
    const framesDroppedDelta = ok ? delta(video?.framesDropped, prev?.videoFramesDropped) : undefined;

    const audioBytes = audio?.bytes;
    const audioBytesDelta = ok ? delta(audioBytes, prev?.audioBytes) : undefined;
    const audioLostDelta = ok ? delta(audio?.packetsLost, prev?.audioPacketsLost) : undefined;
    const audioReceivedDelta = ok ? delta(audio?.packetsReceived, prev?.audioPacketsReceived) : undefined;
    const concealedDelta = ok ? delta(audio?.concealedSamples, prev?.audioConcealedSamples) : undefined;
    const totalDurationDelta = ok
      ? delta(audio?.totalSamplesDuration, prev?.audioTotalSamplesDuration)
      : undefined;
    const jbEmittedDelta = ok
      ? delta(audio?.jitterBufferEmitted, prev?.audioJitterBufferEmitted)
      : undefined;

    next.videoBytes = videoBytes;
    next.videoPacketsLost = video?.packetsLost;
    next.videoPacketsReceived = video?.packetsReceived;
    next.videoFramesDecoded = video?.framesDecoded;
    next.videoFramesDropped = video?.framesDropped;
    next.audioBytes = audioBytes;
    next.audioPacketsLost = audio?.packetsLost;
    next.audioPacketsReceived = audio?.packetsReceived;
    next.audioConcealedSamples = audio?.concealedSamples;
    next.audioTotalSamplesDuration = audio?.totalSamplesDuration;
    next.audioJitterBufferDelay = audio?.jitterBufferDelay;
    next.audioJitterBufferEmitted = audio?.jitterBufferEmitted;
    entry.prev = next;

    const publisher = await this.publisherStats(rawTrack);

    const audioConcealedDeltaSeconds =
      concealedDelta !== undefined && concealedDelta >= 0 ? concealedDelta / 48_000 : undefined;
    let concealmentPct = 0;
    if (totalDurationDelta !== undefined && concealedDelta !== undefined && totalDurationDelta > 0) {
      concealmentPct = pct(concealedDelta, totalDurationDelta * 48_000);
    } else if (jbEmittedDelta !== undefined && concealedDelta !== undefined && jbEmittedDelta > 0) {
      concealmentPct = pct(concealedDelta, jbEmittedDelta);
    }

    const sample: ConnectionStatsSample = {
      trackKey,
      at,
      inbound: entry.ref.participant?.isLocal !== true,
      connectionQuality: entry.ref.participant?.connectionQuality,
    };

    if (video && (videoBytesDelta !== undefined || video.framesDecoded !== undefined)) {
      const fps =
        framesDecodedDelta !== undefined
          ? framesDecodedDelta / Math.max(dtSec, 0.2)
          : (video.fps ?? 0);
      const droppedBase =
        (framesDecodedDelta ?? 0) + (framesDroppedDelta ?? 0);
      sample.video = {
        bitrateBps: videoBytesDelta !== undefined ? (videoBytesDelta * 8) / dtSec : 0,
        jitterMs: (video.jitterSec ?? 0) * 1000,
        packetLossPct:
          videoLostDelta !== undefined
            ? pct(videoLostDelta, videoLostDelta + (videoReceivedDelta ?? 0))
            : 0,
        fps: Math.max(0, fps),
        droppedPct: droppedBase > 0 ? pct(framesDroppedDelta ?? 0, droppedBase) : 0,
        frameWidth: video.frameWidth,
        frameHeight: video.frameHeight,
      };
    }

    if (audio && (audioBytesDelta !== undefined || concealedDelta !== undefined)) {
      sample.audio = {
        bitrateBps: audioBytesDelta !== undefined ? (audioBytesDelta * 8) / dtSec : 0,
        packetLossPct:
          audioLostDelta !== undefined
            ? pct(audioLostDelta, audioLostDelta + (audioReceivedDelta ?? 0))
            : 0,
        jitterMs: (audio.jitterSec ?? 0) * 1000,
        concealmentPct,
        concealedSecondsPerMinute: 0,
      };
      sample.audioConcealedDeltaSeconds = audioConcealedDeltaSeconds;
    }

    const candidateType = localCandidateId ? localCandidates.get(localCandidateId) : undefined;
    const codecId = video?.codecId ?? audio?.codecId;
    const codec = codecId ? codecs.get(codecId) : undefined;
    const rttMs = rttSec !== undefined ? rttSec * 1000 : publisher?.rttMs;
    if (rttMs !== undefined || candidateType || codec) {
      sample.transport = { rttMs, candidateType, codec };
    }
    if (publisher) sample.publisher = publisher;

    this.applyAudioWindow(entry, sample);
    return sample;
  }

  private applyAudioWindow(entry: Entry, sample: ConnectionStatsSample): void {
    if (!sample.audio) return;
    const windowMs = 60_000;
    const cutoff = sample.at - windowMs;
    let sum = 0;
    let oldest = sample.at;
    for (const s of entry.samples) {
      if (s.at >= cutoff && s.audioConcealedDeltaSeconds !== undefined) {
        sum += s.audioConcealedDeltaSeconds;
        if (s.at < oldest) oldest = s.at;
      }
    }
    if (sample.audioConcealedDeltaSeconds !== undefined) sum += sample.audioConcealedDeltaSeconds;
    const spanMs = Math.max(5_000, sample.at - oldest);
    sample.audio.concealedSecondsPerMinute = (sum * 60_000) / Math.min(windowMs, spanMs);
  }

  private async publisherStats(
    track: NonNullable<TrackReference["publication"]>["track"],
  ): Promise<ConnectionStatsSample["publisher"] | undefined> {
    const t = track as unknown as { getSenderStats?: () => Promise<unknown[]> };
    if (typeof t.getSenderStats !== "function") return undefined;
    try {
      const stats = (await t.getSenderStats()) as Record<string, unknown>[];
      if (!Array.isArray(stats) || stats.length === 0) return undefined;
      const primary = stats[0];
      const fps = stats.reduce<number>((max, s) => Math.max(max, num(s.framesPerSecond) ?? 0), 0);
      return {
        qualityLimitationReason:
          (primary.qualityLimitationReason as string | undefined) ?? "none",
        targetBitrateBps: num(primary.targetBitrate),
        fps,
        rttMs: num(primary.roundTripTime) !== undefined ? (num(primary.roundTripTime) as number) * 1000 : undefined,
      };
    } catch {
      return undefined;
    }
  }
}

export const statsCollector = new StatsCollector();
export const CADENCE = { watch: WATCH_CADENCE_MS, idle: IDLE_CADENCE_MS };

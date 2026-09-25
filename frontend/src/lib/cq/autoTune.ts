import { LocalVideoTrack, Room, Track, VideoQuality, type RemoteTrackPublication } from "livekit-client";
import type { TrackReference } from "@livekit/components-react";
import { statsCollector } from "./connectionStats";
import { useCqStore } from "./connectionQualityStore";
import type { AutoTuneResult, AutoTuneStep, ConnectionStatsSample, QualityMode } from "./types";

const SETTLE_MS = 1600;
const STEP_MS = 2600;
const BASELINE_MS = 4000;
const QUALITY_STEPS: Exclude<QualityMode, "auto">[] = ["low", "medium", "high"];
const LOCAL_BITRATE_STEPS: { quality: Exclude<QualityMode, "auto">; kbps: number }[] = [
  { quality: "low", kbps: 150 },
  { quality: "medium", kbps: 500 },
  { quality: "high", kbps: 2500 },
];

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

function summarise(samples: ConnectionStatsSample[]): { bitrate: number; loss: number } {
  const bitrates = samples
    .map((s) => s.video?.bitrateBps ?? s.audio?.bitrateBps ?? 0)
    .filter((v) => v > 0);
  const losses = samples
    .map((s) => s.video?.packetLossPct ?? s.audio?.packetLossPct)
    .filter((v): v is number => v !== undefined);
  return { bitrate: median(bitrates), loss: median(losses) };
}

function fingerprint(): string {
  const samples = useCqStore.getState().history;
  const latest = Object.values(samples)
    .map((h) => h[h.length - 1])
    .find((s) => s?.transport);
  if (!latest) return "unknown";
  const relay = latest.transport?.candidateType === "relay" ? "relay" : "direct";
  const rtt = latest.transport?.rttMs ?? 0;
  const rttBucket = rtt < 80 ? "r0" : rtt < 200 ? "r1" : "r2";
  return `${relay}-${rttBucket}`;
}

const QUALITY_TO_LK: Record<Exclude<QualityMode, "auto">, VideoQuality> = {
  low: VideoQuality.LOW,
  medium: VideoQuality.MEDIUM,
  high: VideoQuality.HIGH,
};

function applyRemote(pub: RemoteTrackPublication, quality: Exclude<QualityMode, "auto">): void {
  pub.setVideoQuality(QUALITY_TO_LK[quality]);
}

async function applyLocalBitrate(room: Room, kbps: number): Promise<void> {
  const pub = room.localParticipant.getTrackPublication(Track.Source.Camera);
  const track = pub?.track;
  if (!(track instanceof LocalVideoTrack) || !track.sender) throw new Error("no local sender");
  const params = track.sender.getParameters();
  if (!params.encodings || params.encodings.length === 0) params.encodings = [{}];
  for (const enc of params.encodings) enc.maxBitrate = kbps * 1000;
  await track.sender.setParameters(params);
}

export async function runAutoTune(room: Room, ref: TrackReference): Promise<AutoTuneResult> {
  const trackKey = `${ref.participant.identity}-${ref.source ?? ""}`;
  const store = useCqStore.getState();
  const remote = !ref.participant.isLocal;
  const pub = ref.publication as RemoteTrackPublication | undefined;
  if (remote && (!pub || typeof pub.setVideoQuality !== "function")) {
    throw new Error("no remote video publication");
  }

  const previousQuality: QualityMode = store.overrides[trackKey]?.videoQuality ?? "auto";
  store.setProbe({ trackKey, phase: "baseline", progress: 5, detailKey: "cq.autotune.baseline" });
  await statsCollector.probeSamples(trackKey, BASELINE_MS);

  const steps: AutoTuneStep[] = [];
  for (let i = 0; i < QUALITY_STEPS.length; i += 1) {
    const quality = QUALITY_STEPS[i];
    store.setProbe({
      trackKey,
      phase: "probing",
      progress: 10 + i * 25,
      detailKey: `cq.autotune.step.${quality}`,
    });
    if (remote && pub) {
      applyRemote(pub, quality);
    } else {
      try {
        await applyLocalBitrate(room, LOCAL_BITRATE_STEPS[i].kbps);
      } catch {
        // Local probing is best-effort; without sender access only remote steps run.
      }
    }
    await new Promise((resolve) => window.setTimeout(resolve, SETTLE_MS));
    const samples = await statsCollector.probeSamples(trackKey, STEP_MS);
    const summary = summarise(samples);
    steps.push({
      quality,
      medianBitrateBps: summary.bitrate,
      medianLossPct: summary.loss,
      samples: samples.length,
    });
  }

  if (remote && pub) {
    if (previousQuality === "auto") {
      const latest = store.history[trackKey]?.slice(-1)[0];
      const width = latest?.video?.frameWidth ?? 640;
      const height = latest?.video?.frameHeight ?? 360;
      pub.setVideoDimensions({ width, height });
    } else {
      applyRemote(pub, previousQuality);
    }
  }

  store.setProbe({
    trackKey,
    phase: "evaluating",
    progress: 90,
    detailKey: "cq.autotune.evaluating",
  });
  const viable = steps.filter((s) => s.samples >= 2 && s.medianLossPct <= 5);
  viable.sort((a, b) => QUALITY_STEPS.indexOf(a.quality) - QUALITY_STEPS.indexOf(b.quality));
  const best = viable[viable.length - 1];
  const recommended: Exclude<QualityMode, "auto"> = best?.quality ?? "low";
  const rejectedStep = steps.find((s) => s.medianLossPct > 5);
  const baseline = steps[0];
  const gain =
    best && baseline && baseline.medianBitrateBps > 0
      ? Math.round(((best.medianBitrateBps - baseline.medianBitrateBps) / baseline.medianBitrateBps) * 100)
      : 0;

  const result: AutoTuneResult = {
    trackKey,
    recommendedQuality: recommended,
    expected: gain > 0 ? `+${gain}% delivered bitrate` : "stable on this network",
    steps,
    rejected: rejectedStep
      ? {
          quality: rejectedStep.quality,
          reason: `${rejectedStep.medianLossPct.toFixed(1)} % loss`,
        }
      : undefined,
    at: Date.now(),
  };
  try {
    sessionStorage.setItem(`cq.tune.${trackKey}.${fingerprint()}`, JSON.stringify(result));
  } catch {
    // sessionStorage may be unavailable (private mode); caching is optional.
  }
  store.setProbe({ trackKey, phase: "done", progress: 100, detailKey: "cq.autotune.done" });
  return result;
}

export async function observeAndMaybeRollback(
  trackKey: string,
  baselineScore: number,
  restore: () => Promise<void>,
  onProgress?: (secondsLeft: number) => void,
): Promise<boolean> {
  let samples: ConnectionStatsSample[] = [];
  const unsubscribe = useCqStore.subscribe((state) => {
    const history = state.history[trackKey];
    if (history) samples = history;
  });
  const seconds = 15;
  for (let i = seconds; i > 0; i -= 1) {
    onProgress?.(i);
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  unsubscribe();

  const recent = samples.slice(-5);
  const scores = recent
    .map((s) => useCqStore.getState().verdicts[s.trackKey]?.score)
    .filter((v): v is number => v !== undefined);
  const losses = recent
    .map((s) => s.video?.packetLossPct ?? s.audio?.packetLossPct ?? 0)
    .filter((v) => v !== undefined);
  const medianScore = median(scores);
  const badLossStreak = losses.slice(-3).every((v) => v > 5) && losses.length >= 3;
  const worsened = scores.length >= 3 && medianScore < baselineScore * 0.85;

  useCqStore.getState().setProbe(null);
  if (worsened || badLossStreak) {
    await restore();
    useCqStore.getState().log({
      at: Date.now(),
      action: "auto-tune-rollback",
      result: "error",
      detail: `median score ${Math.round(medianScore)} vs ${baselineScore}`,
    });
    return true;
  }
  useCqStore.getState().log({ at: Date.now(), action: "auto-tune-keep", result: "ok" });
  return false;
}

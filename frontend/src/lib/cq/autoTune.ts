import { LocalVideoTrack, Room, Track, VideoQuality, type RemoteTrackPublication } from "livekit-client";
import type { TrackReference } from "@livekit/components-react";
import { statsCollector } from "./connectionStats";
import { useCqStore } from "./connectionQualityStore";
import { pinVideoControl, releaseVideoControl } from "./connectionActions";
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

const QUALITY_TO_LK: Record<Exclude<QualityMode, "auto">, VideoQuality> = {
  low: VideoQuality.LOW,
  medium: VideoQuality.MEDIUM,
  high: VideoQuality.HIGH,
};

let probeRunning = false;

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
  const history = useCqStore.getState().history;
  const latest = Object.values(history)
    .map((h) => h[h.length - 1])
    .find((s) => s?.transport);
  if (!latest) return "unknown";
  const relay = latest.transport?.candidateType === "relay" ? "relay" : "direct";
  const rtt = latest.transport?.rttMs ?? 0;
  const rttBucket = rtt < 80 ? "r0" : rtt < 200 ? "r1" : "r2";
  return `${relay}-${rttBucket}`;
}

function localSender(room: Room): RTCRtpSender | null {
  const pub = room.localParticipant.getTrackPublication(Track.Source.Camera);
  const track = pub?.track;
  if (!(track instanceof LocalVideoTrack) || !track.sender) return null;
  return track.sender;
}

function captureLocalBitrates(room: Room): number[] | null {
  const sender = localSender(room);
  if (!sender) return null;
  const params = sender.getParameters();
  return params.encodings.map((enc) => enc.maxBitrate ?? 0);
}

async function applyLocalBitrates(room: Room, bitrates: number[]): Promise<void> {
  const sender = localSender(room);
  if (!sender) throw new Error("No camera encoder available for probing");
  const params = sender.getParameters();
  if (!params.encodings || params.encodings.length === 0) params.encodings = [{}];
  params.encodings.forEach((enc, index) => {
    const value = bitrates[index];
    if (value !== undefined) enc.maxBitrate = value;
  });
  await sender.setParameters(params);
}

async function applyLocalBitrate(room: Room, kbps: number): Promise<void> {
  const sender = localSender(room);
  if (!sender) throw new Error("No camera encoder available for probing");
  const params = sender.getParameters();
  if (!params.encodings || params.encodings.length === 0) params.encodings = [{}];
  for (const enc of params.encodings) enc.maxBitrate = kbps * 1000;
  await sender.setParameters(params);
}

export async function runAutoTune(room: Room, ref: TrackReference): Promise<AutoTuneResult> {
  if (probeRunning) throw new Error("A connection test is already running");
  probeRunning = true;
  const trackKey = `${ref.participant.identity}-${ref.source ?? ""}`;
  const store = useCqStore.getState();
  const remote = !ref.participant.isLocal;
  const pub = ref.publication as RemoteTrackPublication | undefined;
  const pinnedByProbe = remote && pub ? pinVideoControl(trackKey, pub) : false;
  const previousQuality: QualityMode = store.overrides[trackKey]?.videoQuality ?? "auto";
  const previousLocalBitrates = remote ? null : captureLocalBitrates(room);

  try {
    if (remote && (!pub || typeof pub.setVideoQuality !== "function")) {
      throw new Error("This tile cannot be probed");
    }
    if (!remote && !previousLocalBitrates) {
      throw new Error("This browser does not expose camera encoder settings");
    }

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
        const target = QUALITY_TO_LK[quality];
        pub.setVideoQuality(target);
        if (pub.videoQuality !== target) throw new Error("The stream did not accept the probe setting");
      } else {
        await applyLocalBitrate(room, LOCAL_BITRATE_STEPS[i].kbps);
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
        const element = pub.track?.attachedElements?.[0] as HTMLElement | undefined;
        if (element && element.clientWidth > 0 && element.clientHeight > 0) {
          pub.setVideoDimensions({ width: element.clientWidth, height: element.clientHeight });
        }
      } else {
        pub.setVideoQuality(QUALITY_TO_LK[previousQuality]);
      }
    } else if (previousLocalBitrates) {
      await applyLocalBitrates(room, previousLocalBitrates);
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
        ? Math.round(
            ((best.medianBitrateBps - baseline.medianBitrateBps) / baseline.medianBitrateBps) * 100,
          )
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
    store.log({ at: Date.now(), action: "auto-tune", result: "ok" });
    return result;
  } catch (error) {
    store.log({
      at: Date.now(),
      action: "auto-tune",
      result: "error",
      detail: error instanceof Error ? error.message : String(error),
    });
    throw error;
  } finally {
    if (pinnedByProbe && pub && previousQuality === "auto") releaseVideoControl(trackKey, pub);
    probeRunning = false;
    useCqStore.getState().setProbe(null);
  }
}

export async function observeAndMaybeRollback(
  trackKey: string,
  baselineScore: number,
  restore: () => Promise<void>,
  onProgress?: (secondsLeft: number) => void,
): Promise<boolean> {
  const scores: number[] = [];
  const losses: number[] = [];
  let lastScore: number | undefined;
  const unsubscribe = useCqStore.subscribe((state) => {
    const verdict = state.verdicts[trackKey];
    if (verdict && verdict.score !== lastScore) {
      lastScore = verdict.score;
      scores.push(verdict.score);
      if (scores.length > 10) scores.shift();
    }
    const history = state.history[trackKey];
    const sample = history?.[history.length - 1];
    const loss = sample?.video?.packetLossPct ?? sample?.audio?.packetLossPct;
    if (loss !== undefined && loss !== losses[losses.length - 1]) {
      losses.push(loss);
      if (losses.length > 10) losses.shift();
    }
  });
  const seconds = 15;
  for (let i = seconds; i > 0; i -= 1) {
    onProgress?.(i);
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  unsubscribe();

  const medianScore = median(scores.slice(-5));
  const badLossStreak = losses.length >= 3 && losses.slice(-3).every((v) => v > 5);
  const worsened = scores.length >= 3 && medianScore < baselineScore * 0.85;

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

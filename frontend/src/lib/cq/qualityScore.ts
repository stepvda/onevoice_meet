import type { CqBand, CqLimiter, ConnectionStatsSample, QualityVerdict } from "./types";

interface Components {
  network: number;
  video: number;
  frame: number;
  audio: number;
  stability: number;
  publisher: number;
}

const WEIGHTS: Record<keyof Components, number> = {
  network: 25,
  video: 20,
  frame: 20,
  audio: 15,
  stability: 10,
  publisher: 10,
};

const HIGH_MIN = 75;
const MEDIUM_MIN = 45;
const HYSTERESIS_MARGIN = 5;
const HYSTERESIS_SAMPLES = 3;

function normLowIsBetter(value: number, good: number, bad: number): number {
  if (!Number.isFinite(value)) return 0.5;
  if (value <= good) return 1;
  if (value >= bad) return 0;
  return 1 - (value - good) / (bad - good);
}

function clamp01(v: number): number {
  return Math.max(0, Math.min(1, v));
}

function expectedBitrateBps(sample: ConnectionStatsSample): number {
  const v = sample.video;
  const height = v?.frameHeight ?? 0;
  const fps = v?.fps ?? 0;
  if (height >= 1000) return 2_200_000;
  if (height >= 700) return 1_200_000;
  if (height >= 500) return 700_000;
  if (height >= 340) return 350_000;
  if (height > 0) return 150_000;
  if (fps > 0) return 400_000;
  return 350_000;
}

function computeComponents(sample: ConnectionStatsSample): Components {
  const v = sample.video;
  const a = sample.audio;
  const t = sample.transport;
  const relay = t?.candidateType === "relay";
  const relayPenalty = relay ? 0.85 : 1;

  const loss = v?.packetLossPct ?? a?.packetLossPct ?? 0;
  const jitter = v?.jitterMs ?? a?.jitterMs ?? 0;
  const rtt = t?.rttMs ?? 0;
  const network = clamp01(
    relayPenalty *
      ((normLowIsBetter(loss, 1, 6) + normLowIsBetter(jitter, 20, 120) + normLowIsBetter(rtt, 80, 500)) / 3),
  );

  const bitrate = v?.bitrateBps ?? 0;
  const video = bitrate > 0 ? clamp01(bitrate / expectedBitrateBps(sample)) : 0.5;

  const dropped = v?.droppedPct ?? 0;
  const fps = v?.fps ?? 0;
  const frame = clamp01((normLowIsBetter(dropped, 1, 8) + normLowIsBetter(Math.max(0, 24 - fps), 0, 12)) / 2);

  const concealment = a?.concealmentPct ?? 0;
  const audioLoss = a?.concealedSecondsPerMinute ?? 0;
  const audio = clamp01(
    a ? (normLowIsBetter(concealment, 1, 5) + normLowIsBetter(audioLoss, 0.5, 3)) / 2 : 0.6,
  );

  const publisher = sample.publisher
    ? sample.publisher.qualityLimitationReason === "cpu" ||
      sample.publisher.qualityLimitationReason === "other"
      ? 0.2
      : sample.publisher.qualityLimitationReason === "bandwidth"
        ? 0.7
        : 1
    : -1;

  return { network, video, frame, audio, stability: 1, publisher };
}

export class ScoreEngine {
  private ewma: Components | null = null;
  private lastScores: number[] = [];
  private band: CqBand | null = null;
  private pendingBand: CqBand | null = null;
  private pendingCount = 0;

  update(sample: ConnectionStatsSample): QualityVerdict {
    const raw = computeComponents(sample);
    const alpha = this.ewma ? 0.4 : 1;
    const components: Components = this.ewma
      ? {
          network: this.ewma.network * (1 - alpha) + raw.network * alpha,
          video: this.ewma.video * (1 - alpha) + raw.video * alpha,
          frame: this.ewma.frame * (1 - alpha) + raw.frame * alpha,
          audio: this.ewma.audio * (1 - alpha) + raw.audio * alpha,
          stability: this.ewma.stability * (1 - alpha) + raw.stability * alpha,
          publisher: raw.publisher,
        }
      : { ...raw };
    this.ewma = components;

    let score = 0;
    const scoreNow = (c: Components): number => {
      const keys: (keyof Components)[] = raw.publisher >= 0
        ? ["network", "video", "frame", "audio", "stability", "publisher"]
        : ["network", "video", "frame", "audio", "stability"];
      let totalWeight = 0;
      let weighted = 0;
      for (const key of keys) {
        totalWeight += WEIGHTS[key];
        weighted += WEIGHTS[key] * c[key];
      }
      return Math.round(weighted / totalWeight);
    };

    score = scoreNow(components);
    this.lastScores.push(score);
    if (this.lastScores.length > 4) this.lastScores.shift();
    if (this.lastScores.length >= 3) {
      const mean = this.lastScores.reduce((s, x) => s + x, 0) / this.lastScores.length;
      const variance =
        this.lastScores.reduce((s, x) => s + (x - mean) * (x - mean), 0) / this.lastScores.length;
      components.stability = clamp01(1 - Math.sqrt(variance) / 25);
      score = scoreNow(components);
    }

    let worst: keyof Components = "network";
    let worstValue = 2;
    for (const key of Object.keys(components) as (keyof Components)[]) {
      if (key === "publisher" && raw.publisher < 0) continue;
      if (components[key] < worstValue) {
        worstValue = components[key];
        worst = key;
      }
    }

    const rawBand: CqBand = score >= HIGH_MIN ? "high" : score >= MEDIUM_MIN ? "medium" : "low";
    let band: CqBand = rawBand;
    if (this.band) {
      if (rawBand === this.band) {
        this.pendingBand = null;
        this.pendingCount = 0;
        band = this.band;
      } else {
        const margin =
          rawBand === "high"
            ? score - HIGH_MIN
            : rawBand === "low"
              ? MEDIUM_MIN - score
              : Math.min(score - MEDIUM_MIN, HIGH_MIN - score);
        if (this.pendingBand === rawBand) this.pendingCount += 1;
        else {
          this.pendingBand = rawBand;
          this.pendingCount = 1;
        }
        if (margin >= HYSTERESIS_MARGIN && this.pendingCount >= HYSTERESIS_SAMPLES) {
          this.band = rawBand;
          band = rawBand;
          this.pendingBand = null;
          this.pendingCount = 0;
        } else {
          band = this.band;
        }
      }
    } else {
      this.band = rawBand;
      band = rawBand;
    }

    let limiter: CqLimiter = "unknown";
    if (raw.publisher >= 0 && worst === "publisher") limiter = "publisher";
    else if (worst === "stability") limiter = "stability";
    else if (worst === "network") limiter = sample.transport?.candidateType === "relay" ? "relay" : "network";
    else if (worst === "audio" || worst === "frame" || worst === "video") limiter = "network";

    return {
      band,
      score,
      limiter,
      stable: this.pendingBand === null,
      lost: false,
      reasonKey: `cq.limiter.${limiter}`,
    };
  }

  reset(): void {
    this.ewma = null;
    this.lastScores = [];
    this.band = null;
    this.pendingBand = null;
    this.pendingCount = 0;
  }
}

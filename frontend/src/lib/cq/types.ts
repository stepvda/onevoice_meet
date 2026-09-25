export type CqBand = "low" | "medium" | "high";
export type CqLimiter = "network" | "publisher" | "relay" | "stability" | "unknown";
export type CompressionPreset = "low" | "medium" | "high";
export type QualityMode = "low" | "medium" | "high" | "auto";

export interface ConnectionStatsSample {
  trackKey: string;
  at: number;
  inbound: boolean;
  connectionQuality?: string;
  video?: {
    bitrateBps: number;
    jitterMs: number;
    packetLossPct: number;
    fps: number;
    droppedPct: number;
    frameWidth?: number;
    frameHeight?: number;
  };
  audio?: {
    bitrateBps: number;
    packetLossPct: number;
    jitterMs: number;
    concealmentPct: number;
    concealedSecondsPerMinute: number;
  };
  transport?: {
    rttMs?: number;
    candidateType?: string;
    codec?: string;
  };
  publisher?: {
    qualityLimitationReason?: string;
    targetBitrateBps?: number;
    fps?: number;
    rttMs?: number;
  };
  audioConcealedDeltaSeconds?: number;
}

export interface QualityVerdict {
  band: CqBand;
  score: number;
  limiter: CqLimiter;
  stable: boolean;
  lost: boolean;
  reasonKey: string;
}

export interface TileQualityOverride {
  trackKey: string;
  videoQuality: QualityMode;
  source: "manual" | "auto-tune";
  maxBitrateKbps?: number;
  resolution?: { width: number; height: number; frameRate?: number };
  compression?: CompressionPreset;
  colorDepth?: 8 | 10;
  updatedAt: number;
}

export interface ActionLogEntry {
  at: number;
  action: string;
  result: "ok" | "error";
  detail?: string;
  durationMs?: number;
}

export type ProbePhase =
  | "idle"
  | "baseline"
  | "probing"
  | "evaluating"
  | "observing"
  | "done"
  | "failed";

export interface AutoTuneStep {
  quality: Exclude<QualityMode, "auto">;
  medianBitrateBps: number;
  medianLossPct: number;
  samples: number;
}

export interface AutoTuneResult {
  trackKey: string;
  recommendedQuality: Exclude<QualityMode, "auto">;
  expected: string;
  steps: AutoTuneStep[];
  rejected?: { quality: Exclude<QualityMode, "auto">; reason: string };
  at: number;
}

export interface CqCapabilities {
  stats: boolean;
  senderStats: boolean;
  statsUnavailableReason?: string;
}

export function bandToScore(band: CqBand): number {
  return band === "high" ? 85 : band === "medium" ? 60 : 25;
}

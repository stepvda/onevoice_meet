import { create } from "zustand";
import { statsCollector } from "./connectionStats";
import { ScoreEngine } from "./qualityScore";
import type {
  ActionLogEntry,
  ConnectionStatsSample,
  ProbePhase,
  QualityVerdict,
  TileQualityOverride,
} from "./types";

const HISTORY_LIMIT = 120;
const ENGINE_LIMIT = 40;

interface ProbeState {
  trackKey: string;
  phase: ProbePhase;
  progress: number;
  detailKey: string;
}

interface CqState {
  openTrackKey: string | null;
  overrides: Record<string, TileQualityOverride>;
  verdicts: Record<string, QualityVerdict>;
  history: Record<string, ConnectionStatsSample[]>;
  probe: ProbeState | null;
  actionLog: ActionLogEntry[];
  open: (trackKey: string) => void;
  close: () => void;
  setOverride: (override: TileQualityOverride) => void;
  clearOverride: (trackKey: string) => void;
  recordSample: (sample: ConnectionStatsSample) => void;
  log: (entry: ActionLogEntry) => void;
  setProbe: (probe: ProbeState | null) => void;
}

const engines = new Map<string, ScoreEngine>();

function evictEngines(): void {
  while (engines.size > ENGINE_LIMIT) {
    const oldest = engines.keys().next().value as string | undefined;
    if (oldest === undefined) break;
    engines.delete(oldest);
  }
}

function pruneHistory(history: Record<string, ConnectionStatsSample[]>): Record<string, ConnectionStatsSample[]> {
  const keys = Object.keys(history);
  if (keys.length <= ENGINE_LIMIT) return history;
  const trimmed: Record<string, ConnectionStatsSample[]> = {};
  for (const key of keys.slice(keys.length - ENGINE_LIMIT)) trimmed[key] = history[key];
  return trimmed;
}

export const useCqStore = create<CqState>((set) => ({
  openTrackKey: null,
  overrides: {},
  verdicts: {},
  history: {},
  probe: null,
  actionLog: [],
  open: (trackKey) => set({ openTrackKey: trackKey }),
  close: () => set({ openTrackKey: null }),
  setOverride: (override) =>
    set((state) => ({ overrides: { ...state.overrides, [override.trackKey]: override } })),
  clearOverride: (trackKey) =>
    set((state) => {
      const next = { ...state.overrides };
      delete next[trackKey];
      return { overrides: next };
    }),
  recordSample: (sample) =>
    set((state) => {
      let engine = engines.get(sample.trackKey);
      if (!engine) {
        engine = new ScoreEngine();
        engines.set(sample.trackKey, engine);
        evictEngines();
      }
      const verdict = engine.update(sample);
      if (sample.connectionQuality === "lost") {
        verdict.band = "low";
        verdict.lost = true;
        verdict.limiter = "network";
        verdict.reasonKey = "cq.limiter.lost";
      }
      const prior = state.history[sample.trackKey] ?? [];
      const history = prior.concat(sample).slice(-HISTORY_LIMIT);
      return {
        verdicts: { ...state.verdicts, [sample.trackKey]: verdict },
        history: pruneHistory({ ...state.history, [sample.trackKey]: history }),
      };
    }),
  log: (entry) =>
    set((state) => ({ actionLog: [entry, ...state.actionLog].slice(0, 10) })),
  setProbe: (probe) => set({ probe }),
}));

statsCollector.subscribe((sample) => {
  useCqStore.getState().recordSample(sample);
});

export function activeOverride(trackKey: string): TileQualityOverride | undefined {
  return useCqStore.getState().overrides[trackKey];
}

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useRoomContext } from "@livekit/components-react";
import type { TrackReference } from "@livekit/components-react";
import type { RemoteTrackPublication } from "livekit-client";
import { RefreshCw, Wifi, X } from "lucide-react";
import { usePreferences } from "../lib/preferences";
import { useCqStore } from "../lib/cq/connectionQualityStore";
import { runAction, CqActionError } from "../lib/cq/connectionActions";
import { runAutoTune, observeAndMaybeRollback } from "../lib/cq/autoTune";
import {
  applyCameraPreset,
  listMediaDevices,
  readCameraCapabilities,
  readCaptureSettings,
  readMicrophoneSettings,
  setMicrophoneMode,
  switchDevice,
  type AudioQualityMode,
  type CameraCapabilities,
  type CaptureSettings,
  type DeviceLists,
  type MicChannelMode,
} from "../lib/cq/deviceConfig";
import type { AutoTuneResult, CompressionPreset, ConnectionStatsSample, QualityMode } from "../lib/cq/types";

type Tab = "overview" | "details" | "actions" | "device";

interface Props {
  trackKey: string;
  trackRef: TrackReference;
  width: number;
  height: number;
  onClose: () => void;
}

const TABS: Tab[] = ["overview", "details", "actions", "device"];

function formatBitrate(bps?: number): string {
  if (!bps) return "—";
  if (bps >= 1_000_000) return `${(bps / 1_000_000).toFixed(2)} Mbps`;
  return `${Math.round(bps / 1000)} kbps`;
}

function Btn({
  label,
  onClick,
  primary,
  disabled,
  danger,
}: {
  label: string;
  onClick: () => void;
  primary?: boolean;
  disabled?: boolean;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={[
        "px-2 py-1 rounded text-[11px] font-medium transition-colors",
        primary
          ? "bg-accent-500 text-white hover:bg-accent-600 disabled:opacity-50"
          : danger
            ? "bg-red-600 text-white hover:bg-red-700 disabled:opacity-50"
            : "bg-white/10 text-slate-100 hover:bg-white/20 disabled:opacity-50",
      ].join(" ")}
    >
      {label}
    </button>
  );
}

function Seg<T extends string>({
  options,
  value,
  onChange,
  disabled,
}: {
  options: { value: T; label: string; disabled?: boolean }[];
  value: T;
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  return (
    <div className="inline-flex rounded border border-white/15 bg-black/30 p-0.5">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          disabled={disabled || opt.disabled}
          onClick={() => onChange(opt.value)}
          className={[
            "px-2 py-0.5 text-[10px] rounded-sm",
            value === opt.value ? "bg-accent-500 text-white font-semibold" : "text-slate-300 hover:bg-white/10",
            opt.disabled ? "opacity-40 cursor-not-allowed" : "",
          ].join(" ")}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

function Row({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-white/5 py-[3px] text-[10.5px]">
      <span className="text-slate-400">{label}</span>
      <span className={["font-semibold tabular-nums", tone ?? "text-slate-100"].join(" ")}>{value}</span>
    </div>
  );
}

function Sparkline({ samples }: { samples: ConnectionStatsSample[] }) {
  const values = samples
    .slice(-60)
    .map((s) => s.video?.bitrateBps ?? s.audio?.bitrateBps ?? 0)
    .filter((v) => v > 0);
  if (values.length < 2) {
    return (
      <div className="h-9 rounded border border-white/10 bg-black/30 text-[10px] text-slate-500 flex items-center justify-center">
        —
      </div>
    );
  }
  const max = Math.max(...values, 1);
  const points = values
    .map((v, i) => `${(i / (values.length - 1)) * 100},${34 - (v / max) * 30}`)
    .join(" ");
  return (
    <svg viewBox="0 0 100 36" preserveAspectRatio="none" className="w-full h-9 rounded border border-white/10 bg-black/30">
      <polyline points={points} fill="none" stroke="#60a5fa" strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function bandTone(band: string): string {
  return band === "high" ? "text-emerald-400" : band === "medium" ? "text-amber-400" : "text-red-400";
}

function panelSection(title: string, children: ReactNode) {
  return (
    <div className="mb-2">
      <div className="mb-1 text-[9.5px] font-bold uppercase tracking-wide text-sky-300/90">{title}</div>
      {children}
    </div>
  );
}

export default function ConnectionQualityOverlay({ trackKey, trackRef, width, height, onClose }: Props) {
  const room = useRoomContext();
  const { t } = useTranslation();
  const verdict = useCqStore((s) => s.verdicts[trackKey]);
  const history = useCqStore((s) => s.history[trackKey]) ?? [];
  const override = useCqStore((s) => s.overrides[trackKey]);
  const actionLog = useCqStore((s) => s.actionLog);
  const probe = useCqStore((s) => s.probe);
  const clearOverride = useCqStore((s) => s.clearOverride);
  const setOverride = useCqStore((s) => s.setOverride);
  const log = useCqStore((s) => s.log);
  const monoAudioPref = usePreferences((s) => s.accessibility.monoAudio);

  const [tab, setTab] = useState<Tab>("overview");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tune, setTune] = useState<AutoTuneResult | null>(null);
  const [tuneStatus, setTuneStatus] = useState<string | null>(null);
  const [confirmReconnect, setConfirmReconnect] = useState(false);
  const [devices, setDevices] = useState<DeviceLists>({ cameras: [], microphones: [] });
  const [caps, setCaps] = useState<CameraCapabilities>({});
  const [settings, setSettings] = useState<CaptureSettings>({});
  const [channels, setChannels] = useState<MicChannelMode>("mono");
  const [quality, setQuality] = useState<AudioQualityMode>("voice");
  const [maxBitrate, setMaxBitrate] = useState(1200);
  const [videoEnabled, setVideoEnabled] = useState(true);
  const playbackMono = monoAudioPref;

  const isLocal = trackRef.participant.isLocal;
  const latest = history[history.length - 1];
  const video = latest?.video;
  const audio = latest?.audio;
  const transport = latest?.transport;

  useEffect(() => {
    const pub = trackRef.publication as RemoteTrackPublication | undefined;
    if (pub && typeof pub.isEnabled === "boolean") setVideoEnabled(pub.isEnabled);
  }, [tab, trackRef]);

  useEffect(() => {
    if (!isLocal || tab !== "device") return;
    let cancelled = false;
    void listMediaDevices().then((list) => {
      if (!cancelled) setDevices(list);
    });
    setCaps(readCameraCapabilities(room));
    setSettings(readCaptureSettings(room));
    const mic = readMicrophoneSettings(room);
    if (mic.channelCount) setChannels(mic.channelCount > 1 ? "stereo" : "mono");
    return () => {
      cancelled = true;
    };
  }, [isLocal, room, tab]);

  const invoke = useCallback(
    async (name: string, fn: () => Promise<void>) => {
      setBusy(name);
      setError(null);
      try {
        await fn();
      } catch (e) {
        setError(e instanceof CqActionError || e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const runTest = useCallback(() => {
    invoke("probe", async () => {
      setTuneStatus(t("cq.autotune.running", { defaultValue: "Testing the connection…" }));
      try {
        const result = await runAutoTune(room, trackRef);
        setTune(result);
      } finally {
        setTuneStatus(null);
      }
    });
  }, [invoke, room, t, trackRef]);

  const applyTune = useCallback(
    (result: AutoTuneResult) => {
      invoke("applyTune", async () => {
        const baseline = verdict?.score ?? 60;
        const previous: QualityMode = override?.videoQuality ?? "auto";
        const applyTarget = async (quality: QualityMode) => {
          if (isLocal) {
            const kbps = quality === "low" ? 150 : quality === "medium" ? 500 : 2500;
            await runAction(room, trackRef, { kind: "maxBitrate", kbps });
            return;
          }
          await runAction(room, trackRef, { kind: "quality", quality });
        };
        await applyTarget(result.recommendedQuality);
        setTuneStatus(t("cq.autotune.observing", { seconds: 15, defaultValue: "Observing for 15 s…" }));
        const rolledBack = await observeAndMaybeRollback(
          trackKey,
          baseline,
          async () => {
            await applyTarget(previous);
          },
          (secondsLeft) =>
            setTuneStatus(
              t("cq.autotune.observing", { seconds: secondsLeft, defaultValue: "Observing… {{seconds}} s" }),
            ),
        );
        setTuneStatus(
          rolledBack
            ? t("cq.autotune.rolledBack", { defaultValue: "Reverted — the new setting was worse on this network" })
            : t("cq.autotune.applied", { defaultValue: "Applied — settings kept" }),
        );
        if (!rolledBack) setTune(null);
      });
    },
    [invoke, isLocal, override?.videoQuality, room, t, trackKey, trackRef, verdict?.score],
  );

  const exportReport = useCallback(() => {
    const report = {
      schema: "onevoice.cq.report/1",
      generatedAt: new Date().toISOString(),
      tile: {
        role: isLocal ? "self" : "remote",
        source: trackRef.source ?? "camera",
        codec: transport?.codec,
        transport: {
          type: transport?.candidateType ?? "unknown",
          rttMs: transport?.rttMs,
        },
        verdict,
      },
      capture: isLocal
        ? {
            video: settings,
            audio: { channels, quality, playbackMono },
          }
        : undefined,
      history: history.map((s) => ({
        t: s.at,
        bitrateBps: s.video?.bitrateBps ?? s.audio?.bitrateBps,
        lossPct: s.video?.packetLossPct ?? s.audio?.packetLossPct,
        jitterMs: s.video?.jitterMs ?? s.audio?.jitterMs,
        rttMs: s.transport?.rttMs,
        fps: s.video?.fps,
        droppedPct: s.video?.droppedPct,
        audioLossSPerMin: s.audio?.concealedSecondsPerMinute,
      })),
      actions: actionLog,
    };
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `cq-report-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
    log({ at: Date.now(), action: "export", result: "ok" });
  }, [actionLog, channels, history, isLocal, log, playbackMono, quality, settings, t, trackRef.source, transport, verdict]);

  const qualityOptions = useMemo(
    () => [
      { value: "low" as QualityMode, label: t("cq.quality.low", { defaultValue: "Low" }) },
      { value: "medium" as QualityMode, label: t("cq.quality.medium", { defaultValue: "Med" }) },
      { value: "high" as QualityMode, label: t("cq.quality.high", { defaultValue: "High" }) },
      { value: "auto" as QualityMode, label: t("cq.quality.auto", { defaultValue: "Auto" }) },
    ],
    [t],
  );

  const resolutionOptions = useMemo(() => {
    const maxH = caps.maxHeight ?? 720;
    return [
      { value: "auto", label: t("cq.quality.auto", { defaultValue: "Auto" }) },
      { value: "1080", label: "1080p", disabled: maxH < 1080 },
      { value: "720", label: "720p", disabled: maxH < 720 },
      { value: "540", label: "540p", disabled: maxH < 540 },
      { value: "360", label: "360p", disabled: maxH < 360 },
    ];
  }, [caps.maxHeight, t]);

  const currentResolution = settings.height
    ? `${settings.height >= 1080 ? "1080" : settings.height >= 720 ? "720" : settings.height >= 540 ? "540" : "360"}`
    : "auto";

  const limiterText = verdict
    ? t(verdict.reasonKey, {
        defaultValue:
          verdict.limiter === "publisher"
            ? "Publisher device is limiting the stream"
            : verdict.limiter === "relay"
              ? "Relay path: elevated round-trip time"
              : verdict.limiter === "stability"
                ? "Unstable link: quality is oscillating"
                : "Network: packet loss and jitter",
      })
    : t("cq.band.measuring", { defaultValue: "Measuring…" });

  const tone = bandTone(verdict?.lost ? "low" : verdict?.band ?? "medium");

  return (
    <div
      role="dialog"
      aria-label={t("cq.panel.ariaLabel", { name: trackRef.participant.name || trackRef.participant.identity, defaultValue: "Connection quality" })}
      data-testid="cq-panel"
      style={{ width, maxHeight: height }}
      className="absolute left-0 top-9 flex flex-col rounded-xl border border-slate-500/60 bg-slate-950/85 backdrop-blur-md shadow-2xl text-slate-100 p-2.5"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="text-[12px] font-semibold truncate">{t("cq.panel.title", { defaultValue: "Connection Quality" })}</div>
          <div className="text-[10px] text-slate-400 truncate">
            {trackRef.participant.name || trackRef.participant.identity}
            {isLocal ? ` · ${t("cq.panel.self", { defaultValue: "your camera" })}` : ""}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("common.close", { defaultValue: "Close" })}
          className="p-1 rounded hover:bg-white/10 text-slate-300"
        >
          <X size={14} />
        </button>
      </div>

      <div className="mt-2 flex rounded border border-white/15 bg-black/30 p-0.5">
        {TABS.map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setTab(item)}
            aria-pressed={tab === item}
            className={[
              "flex-1 px-1 py-0.5 text-[10px] rounded-sm",
              tab === item ? "bg-accent-500 text-white font-semibold" : "text-slate-300 hover:bg-white/10",
            ].join(" ")}
          >
            {t(`cq.tab.${item}`, {
              defaultValue: item.charAt(0).toUpperCase() + item.slice(1),
            })}
          </button>
        ))}
      </div>

      <div className="mt-2 flex-1 min-h-0 overflow-y-auto pr-0.5">
        {tab === "overview" && (
          <div>
            <div className="flex items-center justify-between gap-2">
              <span className={["text-[11px] font-bold uppercase", tone].join(" ")}>
                {t(`cq.band.${verdict?.lost ? "lost" : verdict?.band ?? "measuring"}`, {
                  defaultValue: verdict?.band ?? "Measuring…",
                })}
              </span>
              <span className="text-[10px] text-slate-400 tabular-nums">
                {t("cq.overview.score", { score: verdict?.score ?? 0, defaultValue: "score {{score}}" })}
              </span>
            </div>
            <div className="text-[10px] text-slate-400 mb-1.5">{limiterText}</div>
            <Sparkline samples={history} />
            <div className="mt-2">
              <Row label={t("cq.metric.bitrate", { defaultValue: "Bitrate" })} value={formatBitrate(video?.bitrateBps ?? audio?.bitrateBps)} />
              <Row
                label={t("cq.metric.resolution", { defaultValue: "Resolution" })}
                value={video?.frameWidth && video?.frameHeight ? `${video.frameWidth} × ${video.frameHeight}` : "—"}
              />
              <Row label={t("cq.metric.fps", { defaultValue: "Frame rate" })} value={video ? `${video.fps.toFixed(1)} fps` : "—"} />
              <Row label={t("cq.metric.loss", { defaultValue: "Packet loss" })} value={`${(video?.packetLossPct ?? audio?.packetLossPct ?? 0).toFixed(1)} %`} />
              <Row
                label={t("cq.metric.jitterRtt", { defaultValue: "Jitter / RTT" })}
                value={`${Math.round(video?.jitterMs ?? 0)} ms / ${transport?.rttMs !== undefined ? Math.round(transport.rttMs) : "—"} ms`}
              />
              <Row label={t("cq.metric.dropped", { defaultValue: "Skipped frames" })} value={`${(video?.droppedPct ?? 0).toFixed(1)} %`} />
              <Row
                label={t("cq.metric.audioLoss", { defaultValue: "Audio loss (last min)" })}
                value={`${(audio?.concealedSecondsPerMinute ?? 0).toFixed(1)} s`}
              />
              <Row
                label={t("cq.metric.transport", { defaultValue: "Transport" })}
                value={transport?.candidateType === "relay" ? t("cq.metric.relay", { defaultValue: "Relay" }) : t("cq.metric.direct", { defaultValue: "Direct" })}
              />
            </div>
          </div>
        )}

        {tab === "details" && (
          <div>
            {panelSection(
              t("cq.details.log", { defaultValue: "Recent actions" }),
              actionLog.length === 0 ? (
                <div className="text-[10px] text-slate-500">{t("cq.details.none", { defaultValue: "No actions yet" })}</div>
              ) : (
                actionLog.map((entry, i) => (
                  <Row
                    key={`${entry.at}-${i}`}
                    label={entry.action}
                    value={entry.result === "ok" ? "ok" : entry.detail ?? "error"}
                    tone={entry.result === "ok" ? "text-emerald-400" : "text-red-400"}
                  />
                ))
              ),
            )}
            {video?.frameWidth && panelSection(
              t("cq.details.video", { defaultValue: "Video" }),
              <>
                <Row label="resolution" value={`${video.frameWidth} × ${video.frameHeight ?? 0}`} />
                <Row label="fps" value={video.fps.toFixed(1)} />
                <Row label="loss" value={`${video.packetLossPct.toFixed(2)} %`} />
                <Row label="dropped" value={`${video.droppedPct.toFixed(2)} %`} />
              </>,
            )}
            {audio && panelSection(
              t("cq.details.audio", { defaultValue: "Audio" }),
              <>
                <Row label="bitrate" value={formatBitrate(audio.bitrateBps)} />
                <Row label="loss" value={`${audio.packetLossPct.toFixed(2)} %`} />
                <Row label="concealment" value={`${audio.concealmentPct.toFixed(2)} %`} />
              </>,
            )}
            <Btn label={t("cq.action.export", { defaultValue: "Export report" })} onClick={exportReport} />
          </div>
        )}

        {tab === "actions" && (
          <div>
            {!isLocal && (
              <div className="flex items-center justify-between gap-2 mb-1">
                <span className="text-[10.5px] text-slate-400">{t("cq.action.quality", { defaultValue: "Quality (simulcast layer)" })}</span>
                <Seg
                  options={qualityOptions}
                  value={override?.videoQuality ?? "auto"}
                  disabled={busy !== null}
                  onChange={(value) =>
                    invoke("quality", () => runAction(room, trackRef, { kind: "quality", quality: value }))
                  }
                />
              </div>
            )}
            {isLocal && (
              <>
                <div className="flex items-center justify-between gap-2 mb-1">
                  <span className="text-[10.5px] text-slate-400">{t("cq.action.maxBitrate", { defaultValue: "Max bitrate" })}</span>
                  <span className="text-[10px] tabular-nums text-slate-200">{maxBitrate} kbps</span>
                </div>
                <input
                  type="range"
                  min={150}
                  max={3000}
                  step={50}
                  value={maxBitrate}
                  onChange={(e) => setMaxBitrate(Number(e.target.value))}
                  onMouseUp={() => invoke("maxBitrate", () => runAction(room, trackRef, { kind: "maxBitrate", kbps: maxBitrate }))}
                  onTouchEnd={() => invoke("maxBitrate", () => runAction(room, trackRef, { kind: "maxBitrate", kbps: maxBitrate }))}
                  className="w-full accent-sky-500"
                  aria-label={t("cq.action.maxBitrate", { defaultValue: "Max bitrate" })}
                />
                <div className="flex items-center justify-between gap-2 mb-1 mt-1">
                  <span className="text-[10.5px] text-slate-400">{t("cq.action.compression", { defaultValue: "Compression" })}</span>
                  <Seg
                    options={[
                      { value: "low" as CompressionPreset, label: t("cq.quality.low", { defaultValue: "Low" }) },
                      { value: "medium" as CompressionPreset, label: t("cq.quality.medium", { defaultValue: "Med" }) },
                      { value: "high" as CompressionPreset, label: t("cq.quality.high", { defaultValue: "High" }) },
                    ]}
                    value={override?.compression ?? "medium"}
                    disabled={busy !== null}
                    onChange={(value) => invoke("compression", () => runAction(room, trackRef, { kind: "compression", preset: value }))}
                  />
                </div>
                <div className="flex items-center justify-between gap-2 mb-1">
                  <span className="text-[10.5px] text-slate-400">{t("cq.action.colorDepth", { defaultValue: "Colour depth" })}</span>
                  <Seg
                    options={[
                      { value: "8" as const, label: "8-bit" },
                      { value: "10" as const, label: "10-bit", disabled: true },
                    ]}
                    value="8"
                    disabled={busy !== null}
                    onChange={() => undefined}
                  />
                </div>
                <div className="text-[9.5px] italic text-slate-500 mb-1">
                  {t("cq.action.colorDepthUnsupported", { defaultValue: "10-bit is not available on this device / codec" })}
                </div>
              </>
            )}
            {!isLocal && (
              <div className="flex items-center justify-between gap-2 mb-1">
                <span className="text-[10.5px] text-slate-400">{t("cq.action.audioOnly", { defaultValue: "Audio only for this tile" })}</span>
                <Seg
                  options={[
                    { value: "off" as const, label: t("common.off", { defaultValue: "Off" }) },
                    { value: "on" as const, label: t("common.on", { defaultValue: "On" }) },
                  ]}
                  value={videoEnabled ? "off" : "on"}
                  disabled={busy !== null}
                  onChange={(value) =>
                    invoke("audioOnly", async () => {
                      await runAction(room, trackRef, { kind: "audioOnly", enabled: value === "on" });
                      const pub = trackRef.publication as RemoteTrackPublication | undefined;
                      if (pub) setVideoEnabled(pub.isEnabled);
                    })
                  }
                />
              </div>
            )}
            <div className="mt-2 flex flex-wrap gap-1.5">
              <Btn
                label={t("cq.action.resync", { defaultValue: "Resync stream" })}
                disabled={busy !== null || isLocal}
                onClick={() => invoke("resync", () => runAction(room, trackRef, { kind: "resync" }))}
              />
              <Btn
                label={t("cq.action.resetAuto", { defaultValue: "Reset to auto" })}
                disabled={busy !== null}
                onClick={() => {
                  clearOverride(trackKey);
                  log({ at: Date.now(), action: "reset-override", result: "ok" });
                }}
              />
              {confirmReconnect ? (
                <>
                  <Btn
                    label={t("cq.action.reconnectConfirm", { defaultValue: "Confirm reconnect" })}
                    danger
                    disabled={busy !== null}
                    onClick={() => {
                      setConfirmReconnect(false);
                      invoke("reconnect", () => runAction(room, trackRef, { kind: "reconnect" }));
                    }}
                  />
                  <Btn label={t("common.cancel", { defaultValue: "Cancel" })} onClick={() => setConfirmReconnect(false)} />
                </>
              ) : (
                <Btn
                  label={t("cq.action.reconnect", { defaultValue: "Reconnect" })}
                  disabled={busy !== null}
                  onClick={() => setConfirmReconnect(true)}
                />
              )}
              <Btn
                label={t("cq.autotune.test", { defaultValue: "Test optimal settings" })}
                primary
                disabled={busy !== null || probe !== null}
                onClick={runTest}
              />
            </div>
            {!isLocal && (
              <div className="mt-1.5 text-[9.5px] italic text-slate-500">
                {t("cq.scope.receive", {
                  name: trackRef.participant.name || trackRef.participant.identity,
                  defaultValue: "Applies to what you receive — the publisher's camera stays untouched",
                })}
              </div>
            )}
            {tune && (
              <div className="mt-2 rounded border border-emerald-500/40 bg-emerald-950/30 p-1.5">
                <div className="text-[10.5px] font-semibold text-emerald-300">
                  {t("cq.autotune.recommended", { defaultValue: "Recommended for this network" })}: {tune.recommendedQuality.toUpperCase()}
                </div>
                <div className="text-[10px] text-slate-300">{tune.expected}</div>
                {tune.rejected && (
                  <div className="text-[9.5px] text-red-300">
                    {t("cq.autotune.rejected", {
                      layer: tune.rejected.quality.toUpperCase(),
                      reason: tune.rejected.reason,
                      defaultValue: "{{layer}} was rejected: {{reason}}",
                    })}
                  </div>
                )}
                <div className="mt-1 flex gap-1.5">
                  <Btn label={t("cq.action.apply", { defaultValue: "Apply" })} primary onClick={() => applyTune(tune)} />
                  <Btn
                    label={t("cq.autotune.retry", { defaultValue: "Retry" })}
                    disabled={busy !== null}
                    onClick={runTest}
                  />
                  <Btn label={t("common.dismiss", { defaultValue: "Dismiss" })} onClick={() => setTune(null)} />
                </div>
              </div>
            )}
            {tuneStatus && <div className="mt-1.5 text-[10px] text-sky-300">{tuneStatus}</div>}
          </div>
        )}

        {tab === "device" && !isLocal && (
          <div className="text-[10.5px] text-slate-400">
            {t("cq.device.remoteNote", {
              name: trackRef.participant.name || trackRef.participant.identity,
              defaultValue: "Camera and microphone settings belong to the publisher",
            })}
          </div>
        )}

        {tab === "device" && isLocal && (
          <div>
            {panelSection(
              t("cq.device.camera", { defaultValue: "Camera" }),
              <>
                <div className="mb-1">
                  <select
                    aria-label={t("cq.device.camera", { defaultValue: "Camera" })}
                    value={usePreferences.getState().av.preferredCameraId ?? ""}
                    onChange={(e) => invoke("camera-device", () => switchDevice(room, "videoinput", e.target.value))}
                    className="w-full rounded border border-white/15 bg-black/40 px-1.5 py-1 text-[10.5px]"
                  >
                    {devices.cameras.map((d) => (
                      <option key={d.deviceId} value={d.deviceId}>
                        {d.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex items-center justify-between gap-2 mb-1">
                  <span className="text-[10.5px] text-slate-400">{t("cq.device.resolution", { defaultValue: "Resolution" })}</span>
                  <Seg
                    options={resolutionOptions.map((o) => ({ ...o, value: o.value }))}
                    value={currentResolution}
                    disabled={busy !== null}
                    onChange={(value) => {
                      const map: Record<string, { width: number; height: number }> = {
                        "1080": { width: 1920, height: 1080 },
                        "720": { width: 1280, height: 720 },
                        "540": { width: 960, height: 540 },
                        "360": { width: 640, height: 360 },
                      };
                      if (value === "auto") return;
                      const preset = map[value];
                      if (!preset) return;
                      invoke("resolution", async () => {
                        const achieved = await applyCameraPreset(room, { ...preset, frameRate: settings.frameRate });
                        setSettings(achieved);
                        setOverride({
                          trackKey,
                          videoQuality: "auto",
                          source: "manual",
                          resolution: {
                            width: achieved.width ?? preset.width,
                            height: achieved.height ?? preset.height,
                            frameRate: achieved.frameRate,
                          },
                          updatedAt: Date.now(),
                        });
                      });
                    }}
                  />
                </div>
                <div className="flex items-center justify-between gap-2 mb-1">
                  <span className="text-[10.5px] text-slate-400">{t("cq.device.frameRate", { defaultValue: "Frame rate" })}</span>
                  <Seg
                    options={[
                      { value: "30", label: "30" },
                      { value: "24", label: "24" },
                      { value: "15", label: "15" },
                    ]}
                    value={String(Math.round(settings.frameRate ?? 30))}
                    disabled={busy !== null}
                    onChange={(value) =>
                      invoke("frameRate", async () => {
                        const achieved = await applyCameraPreset(room, {
                          width: settings.width ?? 1280,
                          height: settings.height ?? 720,
                          frameRate: Number(value),
                        });
                        setSettings(achieved);
                      })
                    }
                  />
                </div>
                <div className="text-[9.5px] italic text-slate-500">
                  {t("cq.device.cameraMax", {
                    width: caps.maxWidth ?? "?",
                    height: caps.maxHeight ?? "?",
                    fps: caps.maxFrameRate ? Math.round(caps.maxFrameRate) : "?",
                    defaultValue: "Camera supports up to {{width}} × {{height}} @ {{fps}} fps",
                  })}
                </div>
              </>,
            )}
            {panelSection(
              t("cq.device.microphone", { defaultValue: "Microphone" }),
              <>
                <div className="mb-1">
                  <select
                    aria-label={t("cq.device.microphone", { defaultValue: "Microphone" })}
                    value={usePreferences.getState().av.preferredMicId ?? ""}
                    onChange={(e) => invoke("mic-device", () => switchDevice(room, "audioinput", e.target.value))}
                    className="w-full rounded border border-white/15 bg-black/40 px-1.5 py-1 text-[10.5px]"
                  >
                    {devices.microphones.map((d) => (
                      <option key={d.deviceId} value={d.deviceId}>
                        {d.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex items-center justify-between gap-2 mb-1">
                  <span className="text-[10.5px] text-slate-400">{t("cq.device.channels", { defaultValue: "Channels" })}</span>
                  <Seg
                    options={[
                      { value: "mono" as MicChannelMode, label: t("cq.device.mono", { defaultValue: "Mono" }) },
                      { value: "stereo" as MicChannelMode, label: t("cq.device.stereo", { defaultValue: "Stereo" }) },
                    ]}
                    value={channels}
                    disabled={busy !== null}
                    onChange={(value) => setChannels(value)}
                  />
                </div>
                <div className="flex items-center justify-between gap-2 mb-1">
                  <span className="text-[10.5px] text-slate-400">{t("cq.device.audioQuality", { defaultValue: "Audio quality" })}</span>
                  <Seg
                    options={[
                      { value: "voice" as AudioQualityMode, label: t("cq.device.voice", { defaultValue: "Voice" }) },
                      { value: "music" as AudioQualityMode, label: t("cq.device.music", { defaultValue: "Music" }) },
                    ]}
                    value={quality}
                    disabled={busy !== null}
                    onChange={(value) => setQuality(value)}
                  />
                </div>
                {channels === "stereo" && (
                  <div className="rounded border border-amber-500/50 bg-amber-950/30 px-1.5 py-1 text-[9.5px] text-amber-200">
                    {t("cq.device.stereoWarning", {
                      defaultValue: "Stereo disables echo cancellation and noise suppression — use headphones; it also raises uplink bandwidth",
                    })}
                  </div>
                )}
                <div className="mt-1.5">
                  <Btn
                    label={t("cq.device.apply", { defaultValue: "Apply to my camera & mic" })}
                    primary
                    disabled={busy !== null}
                    onClick={() => invoke("mic-mode", () => setMicrophoneMode(room, channels, quality))}
                  />
                </div>
              </>,
            )}
          </div>
        )}
      </div>

      {(busy !== null || probe !== null) && (
        <div className="mt-1.5 flex items-center gap-1.5 text-[10px] text-sky-300">
          <RefreshCw size={11} className="animate-spin" />
          {probe
            ? t(`cq.autotune.phase.${probe.phase}`, { defaultValue: probe.phase })
            : t(`cq.busy.${busy}`, { defaultValue: busy ?? "" })}
        </div>
      )}
      {error && (
        <div className="mt-1.5 flex items-start gap-1.5 text-[10px] text-red-300">
          <Wifi size={11} className="mt-0.5" />
          <span>{error}</span>
        </div>
      )}
    </div>
  );
}

/**
 * TICafeBar — the ~70 px high control strip at the bottom of the
 * "Currently online" card.
 *
 * Layout (left → right):
 *   [logo] [main toggle] [mic toggle] [speaker toggle] [count] ... [waveform]
 *
 * The audio session is owned by <TICafeProvider>, not this component, so
 * navigating away from /ti-cafe doesn't drop the call. This bar just talks
 * to the provider via useTICafe().
 */
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Mic, Volume2 } from "lucide-react";
import { useTICafe } from "../lib/tiCafe";

export default function TICafeBar({ liveCount }: { liveCount: number }) {
  const { t } = useTranslation();
  const { connected, connecting, micOn, speakerOn, spectrum, connect, disconnect, setMic, setSpeaker } = useTICafe();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rafRef = useRef<number | null>(null);
  // Latest spectrum stays in a ref so the draw loop reads the current value
  // without React re-creating the effect every frame (60+ Hz updates).
  const spectrumRef = useRef<number[]>(spectrum);
  spectrumRef.current = spectrum;

  // Spectrum-bar visualizer. Heights come from the provider's pre-binned,
  // log-scaled, smoothed spectrum array. We render a vertical-pair (mirrored
  // around the centerline) for an audio-meter look, with a green→amber→red
  // gradient that intensifies with level so it's obvious when someone shouts.
  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const W = c.width;
    const H = c.height;
    // Pre-built gradient (cheap; one fillStyle per frame).
    const grad = ctx.createLinearGradient(0, H, 0, 0);
    grad.addColorStop(0, "#22c55e");
    grad.addColorStop(0.6, "#facc15");
    grad.addColorStop(1, "#ef4444");
    const idleGrad = ctx.createLinearGradient(0, H, 0, 0);
    idleGrad.addColorStop(0, "#334155");
    idleGrad.addColorStop(1, "#475569");

    const draw = () => {
      const bands = spectrumRef.current;
      const N = bands.length;
      ctx.clearRect(0, 0, W, H);
      const gap = 2;
      const w = (W - (N - 1) * gap) / N;
      ctx.fillStyle = connected ? grad : idleGrad;
      for (let i = 0; i < N; i++) {
        const v = bands[i];
        const h = Math.max(2 * dpr, v * (H - 4));
        const x = i * (w + gap);
        const y = (H - h) / 2;
        ctx.fillRect(x, y, w, h);
      }
      rafRef.current = window.requestAnimationFrame(draw);
    };
    draw();
    return () => {
      if (rafRef.current) window.cancelAnimationFrame(rafRef.current);
    };
  }, [connected]);

  const onToggle = () => {
    if (connected) disconnect();
    else void connect();
  };

  return (
    <div
      data-testid="ti-cafe-bar"
      className="mt-4 pt-3 border-t border-primary-700 flex items-center gap-3"
      style={{ minHeight: 70 }}
    >
      <img
        src="/ti_cafe_transparent.png"
        alt="TI Café"
        className="h-12 w-12 object-contain flex-shrink-0"
      />

      {/* Main connect / disconnect toggle */}
      <button
        type="button"
        role="switch"
        aria-checked={connected ? "true" : "false"}
        onClick={onToggle}
        disabled={connecting}
        data-testid="ti-cafe-main-toggle"
        title={
          connected
            ? t("tiCafe.bar.disconnectTitle")
            : t("tiCafe.bar.connectTitle")
        }
        className={[
          "relative inline-flex h-7 w-14 flex-shrink-0 rounded-full transition-colors",
          "focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-primary-900 focus:ring-accent-500",
          connecting
            ? "bg-amber-500/60 cursor-wait"
            : connected
            ? "bg-accent-500"
            : "bg-primary-700",
        ].join(" ")}
      >
        <span
          className={[
            "pointer-events-none inline-block h-6 w-6 transform rounded-full bg-white shadow transition mt-0.5",
            connected ? "translate-x-7" : "translate-x-0.5",
          ].join(" ")}
        />
      </button>

      <SlashedButton
        active={micOn}
        disabled={!connected}
        title={micOn ? t("tiCafe.bar.muteMic") : t("tiCafe.bar.unmuteMic")}
        testId="ti-cafe-mic"
        onClick={() => setMic(!micOn)}
      >
        <Mic size={18} />
      </SlashedButton>

      <SlashedButton
        active={speakerOn}
        disabled={!connected}
        title={speakerOn ? t("tiCafe.bar.muteSpeaker") : t("tiCafe.bar.unmuteSpeaker")}
        testId="ti-cafe-speaker"
        onClick={() => setSpeaker(!speakerOn)}
      >
        <Volume2 size={18} />
      </SlashedButton>

      <span
        data-testid="ti-cafe-live-count"
        className="text-sm font-medium text-slate-400"
      >
        {t("tiCafe.bar.liveCount", { count: liveCount })}
      </span>

      <div className="flex-1" />

      <canvas
        ref={canvasRef}
        width={140}
        height={40}
        data-testid="ti-cafe-waveform"
        className="rounded bg-primary-900/40 border border-primary-700"
      />
    </div>
  );
}

/**
 * Square icon button that draws a diagonal slash through the icon when
 * `active` is false — same convention used by mute icons everywhere else.
 */
function SlashedButton({
  active,
  disabled,
  title,
  testId,
  onClick,
  children,
}: {
  active: boolean;
  disabled?: boolean;
  title: string;
  testId: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      data-testid={testId}
      title={title}
      aria-pressed={active ? "true" : "false"}
      aria-label={title}
      className={[
        "relative inline-flex items-center justify-center h-9 w-9 rounded-lg border",
        "focus:outline-none focus:ring-2 focus:ring-accent-500",
        active
          ? "bg-primary-700 text-slate-100 border-primary-600 hover:bg-primary-600"
          : "bg-primary-900/60 text-slate-400 border-primary-700 hover:bg-primary-800",
        disabled ? "opacity-50 cursor-not-allowed" : "",
      ].join(" ")}
    >
      {children}
      {!active && (
        // Diagonal slash — pure SVG so it sits on top of the icon and scales.
        <svg
          viewBox="0 0 24 24"
          aria-hidden="true"
          className="absolute inset-0 m-auto h-full w-full pointer-events-none text-red-500"
          stroke="currentColor"
          strokeWidth={2.4}
          strokeLinecap="round"
        >
          <line x1="4" y1="20" x2="20" y2="4" />
        </svg>
      )}
    </button>
  );
}

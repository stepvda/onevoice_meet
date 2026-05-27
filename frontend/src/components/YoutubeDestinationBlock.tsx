import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, Copy, Eye, ExternalLink, Youtube } from "lucide-react";
import { api } from "../lib/api";
import { Button, Field, Input, Toggle } from "./ui";
import type { LivestreamDestination } from "../lib/livestreamDestinations";

interface Props {
  dest: LivestreamDestination;
  enabled: boolean;
  url: string;
  streamKey: string;
  onChange: (next: { enabled: boolean; url: string; streamKey: string }) => void;
  isFirst?: boolean;
  status?: "idle" | "streaming" | "failed" | "complete";
  statusError?: string | null;
  // Concurrent viewer count from the supervisor's last poll. Null = not
  // yet computed (broadcast still warming up) or RTMP mode (no API).
  viewerCount?: number | null;
  // YouTube-specific — driven by the modal and persisted to the meeting
  // row via PATCH /meetings/{id}.
  meetingId: string;
  mode: "rtmp" | "api";
  onModeChange: (mode: "rtmp" | "api") => void;
  // Initial OAuth state from MeetingOut. The component refreshes it on
  // mount + after popup callback so the user sees connect/disconnect
  // results immediately.
  initialOauthConnected: boolean;
  initialChannelTitle?: string | null;
  initialWatchUrl?: string | null;
}

/**
 * YouTube-specific destination block. Renders side-by-side the stream-key
 * flow (left) and an OAuth + Data API managed flow (right), with a radio
 * selector picking which one is active.
 */
export default function YoutubeDestinationBlock({
  dest,
  enabled,
  url,
  streamKey,
  onChange,
  isFirst,
  status,
  statusError,
  viewerCount,
  meetingId,
  mode,
  onModeChange,
  initialOauthConnected,
  initialChannelTitle,
  initialWatchUrl,
}: Props) {
  const { t } = useTranslation();
  const [showKey, setShowKey] = useState(false);
  const [oauthConnected, setOauthConnected] = useState(initialOauthConnected);
  const [channelTitle, setChannelTitle] = useState<string | null>(initialChannelTitle ?? null);
  const [watchUrl, setWatchUrl] = useState<string | null>(initialWatchUrl ?? null);
  const [busy, setBusy] = useState<"connect" | "disconnect" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // Refresh status from the server on mount + whenever the popup signals
  // the consent flow completed. The popup posts a message via
  // `window.opener.postMessage(...)`; we listen and re-fetch.
  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      try {
        const s = await api.youtubeOauthStatus(meetingId);
        if (cancelled) return;
        setOauthConnected(!!s.connected);
        setChannelTitle(s.channel_title ?? null);
        setWatchUrl(s.watch_url ?? null);
      } catch {
        /* not fatal — keep last-known state */
      }
    }
    refresh();
    function onMsg(ev: MessageEvent) {
      const data = ev.data as { source?: string; payload?: { ok?: boolean; message?: string } };
      if (!data || data.source !== "meet-yt-oauth") return;
      if (data.payload?.ok) {
        refresh();
        // Connecting flips the meeting to API mode server-side, so
        // reflect that locally too.
        onModeChange("api");
      } else {
        setErr(data.payload?.message ?? "Connection failed");
      }
    }
    window.addEventListener("message", onMsg);
    return () => {
      cancelled = true;
      window.removeEventListener("message", onMsg);
    };
    // onModeChange identity changes between renders — intentionally
    // not in deps so we don't re-subscribe on every parent render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetingId]);

  async function connect() {
    setBusy("connect");
    setErr(null);
    try {
      const r = await api.youtubeOauthStart(meetingId);
      const w = window.open(
        r.authorize_url,
        "meet-yt-oauth",
        "popup=yes,width=520,height=720",
      );
      if (!w) {
        setErr(t("livestream.youtubePopupBlocked", { defaultValue: "Popup blocked — allow popups for this site." }));
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function disconnect() {
    setBusy("disconnect");
    setErr(null);
    try {
      await api.youtubeOauthDisconnect(meetingId);
      setOauthConnected(false);
      setChannelTitle(null);
      setWatchUrl(null);
      // Server flips mode back to "rtmp" on disconnect.
      onModeChange("rtmp");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function copyWatchUrl() {
    if (!watchUrl) return;
    try {
      await navigator.clipboard.writeText(watchUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — fall back silently */
    }
  }

  const dot = (() => {
    if (!enabled || !status) return null;
    const map: Record<string, { className: string; label: string }> = {
      streaming: {
        className:
          "bg-green-400 ring-2 ring-green-400/50 shadow-[0_0_8px_rgba(74,222,128,0.7)] animate-pulse",
        label: t("livestream.statusStreaming", { defaultValue: "Streaming live" }),
      },
      failed: { className: "bg-red-500", label: t("livestream.statusFailed", { defaultValue: "Failed" }) },
      complete: {
        className: "bg-slate-500",
        label: t("livestream.statusComplete", { defaultValue: "Last stream completed" }),
      },
      idle: { className: "bg-slate-600", label: t("livestream.statusIdle", { defaultValue: "Idle" }) },
    };
    const v = map[status] ?? map.idle;
    return (
      <span
        data-testid={`ls-${dest.id}-status`}
        title={statusError ? `${v.label}: ${statusError}` : v.label}
        aria-label={v.label}
        className={`inline-block w-3 h-3 rounded-full flex-shrink-0 ${v.className}`}
      />
    );
  })();

  const rtmpActive = mode === "rtmp";
  const apiActive = mode === "api";

  return (
    <div className={isFirst ? "space-y-3" : "space-y-3 border-t border-primary-700 pt-3"}>
      <div className="flex items-center gap-2">
        {dot}
        <div className="flex-1 min-w-0">
          <Toggle
            id={`ls-${dest.id}-enabled`}
            label={t(dest.toggleI18nKey, { defaultValue: dest.toggleDefault })}
            checked={enabled}
            onChange={(v) => onChange({ enabled: v, url, streamKey })}
          />
        </div>
        {enabled && apiActive && typeof viewerCount === "number" && (
          <span
            data-testid="ls-youtube-viewers"
            title={t("livestream.youtubeViewers", { defaultValue: "Concurrent viewers (YouTube)" })}
            className="inline-flex items-center gap-1 text-xs text-slate-300 bg-primary-800/80 border border-primary-700 rounded-md px-2 py-0.5"
          >
            <Eye size={12} />
            {viewerCount.toLocaleString()}
          </span>
        )}
      </div>

      {enabled && status === "failed" && statusError && (
        <div
          data-testid={`ls-${dest.id}-error`}
          className="text-xs text-red-300 bg-red-900/30 border border-red-900 rounded-md px-2 py-1"
        >
          {statusError}
        </div>
      )}

      {enabled && (
        <>
          {/* Mode selector */}
          <div
            role="radiogroup"
            aria-label={t("livestream.youtubeModeLabel", { defaultValue: "YouTube streaming mode" })}
            className="flex flex-col sm:flex-row gap-2 sm:gap-6 bg-primary-900/60 border border-primary-700 rounded-md p-3"
          >
            <label className="flex items-start gap-2 cursor-pointer flex-1">
              <input
                type="radio"
                name={`ls-${dest.id}-mode`}
                value="rtmp"
                checked={rtmpActive}
                onChange={() => onModeChange("rtmp")}
                data-testid="ls-youtube-mode-rtmp"
                className="mt-0.5 accent-accent-500"
              />
              <span className="text-sm text-slate-200">
                <span className="font-medium">
                  {t("livestream.youtubeModeRtmp", { defaultValue: "Stream key (manual)" })}
                </span>
                <span className="block text-xs text-slate-400">
                  {t("livestream.youtubeModeRtmpHint", {
                    defaultValue: "Paste an RTMP URL + stream key from YouTube Studio.",
                  })}
                </span>
              </span>
            </label>
            <label
              className={`flex items-start gap-2 flex-1 ${oauthConnected ? "cursor-pointer" : "cursor-not-allowed opacity-70"}`}
              title={oauthConnected ? undefined : t("livestream.youtubeModeApiNeedsConnect", { defaultValue: "Connect your YouTube channel first to enable API mode." })}
            >
              <input
                type="radio"
                name={`ls-${dest.id}-mode`}
                value="api"
                checked={apiActive}
                onChange={() => oauthConnected && onModeChange("api")}
                disabled={!oauthConnected}
                data-testid="ls-youtube-mode-api"
                className="mt-0.5 accent-accent-500"
              />
              <span className="text-sm text-slate-200">
                <span className="font-medium">
                  {t("livestream.youtubeModeApi", { defaultValue: "OAuth + API (managed)" })}
                </span>
                <span className="block text-xs text-slate-400">
                  {t("livestream.youtubeModeApiHint", {
                    defaultValue: "Sign in once — Meet provisions and supervises the broadcast.",
                  })}
                </span>
              </span>
            </label>
          </div>

          {err && (
            <div className="text-xs text-red-300 bg-red-900/30 border border-red-900 rounded-md px-2 py-1">
              {err}
            </div>
          )}

          {/* Two-column body */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* Left: RTMP key flow */}
            <div
              className={`space-y-3 ${rtmpActive ? "" : "opacity-50 pointer-events-none select-none"}`}
              aria-hidden={rtmpActive ? undefined : true}
            >
              <Field
                id={`ls-${dest.id}-url`}
                label={t(dest.urlLabel.key, { defaultValue: dest.urlLabel.def })}
              >
                <Input
                  id={`ls-${dest.id}-url`}
                  data-testid={`ls-${dest.id}-url`}
                  type="url"
                  placeholder={dest.urlPlaceholder}
                  value={url}
                  onChange={(e) => onChange({ enabled, url: e.target.value, streamKey })}
                  disabled={!rtmpActive}
                />
              </Field>
              <Field
                id={`ls-${dest.id}-key`}
                label={t(dest.keyLabel.key, { defaultValue: dest.keyLabel.def })}
              >
                <div className="flex gap-2">
                  <Input
                    id={`ls-${dest.id}-key`}
                    data-testid={`ls-${dest.id}-key`}
                    type={showKey ? "text" : "password"}
                    placeholder={dest.keyPlaceholder}
                    value={streamKey}
                    onChange={(e) => onChange({ enabled, url, streamKey: e.target.value })}
                    disabled={!rtmpActive}
                  />
                  <button
                    type="button"
                    onClick={() => setShowKey((s) => !s)}
                    disabled={!rtmpActive}
                    className="px-2 text-xs text-slate-300 rounded-md bg-primary-800 hover:bg-primary-700 border border-primary-700 disabled:opacity-50"
                  >
                    {showKey
                      ? t("common.hide", { defaultValue: "Hide" })
                      : t("common.show", { defaultValue: "Show" })}
                  </button>
                </div>
              </Field>
              <div className="text-xs text-slate-400 leading-relaxed bg-primary-900/60 border border-primary-700 rounded-md p-3 space-y-1">
                <p className="font-medium text-slate-300">
                  {t(dest.helpTitle.key, { defaultValue: dest.helpTitle.def })}
                </p>
                <ol className="list-decimal pl-4 space-y-0.5">
                  {dest.steps.map((s) => (
                    <li key={s.key}>{t(s.key, { defaultValue: s.def })}</li>
                  ))}
                </ol>
              </div>
            </div>

            {/* Right: OAuth + API flow */}
            <div
              className={`space-y-3 ${apiActive ? "" : "opacity-50 pointer-events-none select-none"}`}
              aria-hidden={apiActive ? undefined : true}
            >
              <div className="rounded-md border border-primary-700 bg-primary-900/60 p-3 space-y-3">
                <div className="flex items-center gap-2">
                  <Youtube size={16} className="text-red-500" />
                  <span className="text-sm font-medium text-slate-200">
                    {t("livestream.youtubeOauthTitle", { defaultValue: "YouTube account" })}
                  </span>
                </div>

                {oauthConnected ? (
                  <div className="space-y-3">
                    <div className="text-xs text-slate-300">
                      {t("livestream.youtubeOauthConnectedAs", { defaultValue: "Connected as" })}{" "}
                      <span className="font-medium text-slate-100">
                        {channelTitle ?? t("livestream.youtubeOauthUnknownChannel", { defaultValue: "YouTube channel" })}
                      </span>
                    </div>

                    {watchUrl && (
                      <Field
                        id={`ls-${dest.id}-watch-url`}
                        label={t("livestream.youtubeWatchUrlLabel", {
                          defaultValue: "Public watch URL",
                        })}
                      >
                        <div className="flex gap-2">
                          <Input
                            id={`ls-${dest.id}-watch-url`}
                            data-testid="ls-youtube-watch-url"
                            type="text"
                            value={watchUrl}
                            readOnly
                            onFocus={(e) => e.currentTarget.select()}
                          />
                          <button
                            type="button"
                            onClick={copyWatchUrl}
                            aria-label={t("livestream.youtubeWatchUrlCopy", {
                              defaultValue: "Copy watch URL",
                            })}
                            data-testid="ls-youtube-watch-url-copy"
                            className="px-2 text-xs text-slate-300 rounded-md bg-primary-800 hover:bg-primary-700 border border-primary-700 flex items-center gap-1"
                          >
                            {copied ? <Check size={14} /> : <Copy size={14} />}
                            {copied
                              ? t("common.copied", { defaultValue: "Copied" })
                              : t("common.copy", { defaultValue: "Copy" })}
                          </button>
                          <a
                            href={watchUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            aria-label={t("livestream.youtubeWatchUrlOpen", {
                              defaultValue: "Open on YouTube",
                            })}
                            className="px-2 text-xs text-slate-300 rounded-md bg-primary-800 hover:bg-primary-700 border border-primary-700 flex items-center"
                          >
                            <ExternalLink size={14} />
                          </a>
                        </div>
                      </Field>
                    )}

                    <Button
                      type="button"
                      variant="ghost"
                      onClick={disconnect}
                      disabled={busy === "disconnect"}
                      data-testid="ls-youtube-oauth-disconnect"
                    >
                      {busy === "disconnect"
                        ? t("common.saving", { defaultValue: "Saving…" })
                        : t("livestream.youtubeOauthDisconnect", { defaultValue: "Disconnect" })}
                    </Button>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <div className="text-xs text-slate-400">
                      {t("livestream.youtubeOauthNotConnected", {
                        defaultValue: "Not connected. Sign in with Google to let Meet manage broadcasts on your channel.",
                      })}
                    </div>
                    <Button
                      type="button"
                      onClick={connect}
                      disabled={busy === "connect"}
                      data-testid="ls-youtube-oauth-connect"
                    >
                      {busy === "connect"
                        ? t("common.saving", { defaultValue: "Saving…" })
                        : t("livestream.youtubeOauthConnect", { defaultValue: "Connect with Google" })}
                    </Button>
                  </div>
                )}
              </div>

              <div className="text-xs text-slate-400 leading-relaxed bg-primary-900/60 border border-primary-700 rounded-md p-3 space-y-2">
                <p className="font-medium text-slate-300">
                  {t("livestream.youtubeApiWhatTitle", {
                    defaultValue: "What does managed mode do?",
                  })}
                </p>
                <ul className="list-disc pl-4 space-y-1">
                  <li>
                    {t("livestream.youtubeApiBullet1", {
                      defaultValue:
                        "Provisions a persistent YouTube live stream + ingest key automatically — no copy-paste from YouTube Studio.",
                    })}
                  </li>
                  <li>
                    {t("livestream.youtubeApiBullet2", {
                      defaultValue:
                        "Creates and starts the broadcast (title, description, privacy) when you begin streaming from Meet.",
                    })}
                  </li>
                  <li>
                    {t("livestream.youtubeApiBullet3", {
                      defaultValue:
                        "Watchdog polls stream health every ~30 s and restarts the egress if YouTube reports a bad or stalled feed.",
                    })}
                  </li>
                  <li>
                    {t("livestream.youtubeApiBullet4", {
                      defaultValue:
                        "Handles YouTube's 12-hour broadcast limit by rotating to a fresh broadcast on the same ingest key — seamless for 24/7 streaming.",
                    })}
                  </li>
                  <li>
                    {t("livestream.youtubeApiBullet5", {
                      defaultValue:
                        "Reports per-platform concurrent viewer counts back to the meeting dashboard.",
                    })}
                  </li>
                </ul>
                <p className="text-[11px] text-slate-500 pt-1">
                  {t("livestream.youtubeApiQuotaNote", {
                    defaultValue:
                      "Uses the YouTube Data API v3 (live + livestream scopes). Counts against the channel's daily API quota.",
                  })}
                </p>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

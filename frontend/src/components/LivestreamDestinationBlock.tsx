import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Field, Input, Toggle } from "./ui";
import type { LivestreamDestination } from "../lib/livestreamDestinations";

interface Props {
  dest: LivestreamDestination;
  enabled: boolean;
  url: string;
  streamKey: string;
  onChange: (next: { enabled: boolean; url: string; streamKey: string }) => void;
  // First block in a vertical stack has no top border / top padding so it
  // sits flush against the description paragraph above it.
  isFirst?: boolean;
  // Per-destination publish status from LiveKit (via /stream/destinations
  // polling). Drives the coloured dot next to the toggle label. Undefined
  // = no data yet (e.g. in CreateMeeting where the meeting doesn't exist
  // server-side yet) — renders nothing.
  status?: "idle" | "streaming" | "failed" | "complete";
  statusError?: string | null;
}

/**
 * One destination block (toggle + RTMP URL + stream key + help steps). Used
 * by both CreateMeeting and LivestreamSettingsModal — driven by the metadata
 * in lib/livestreamDestinations.ts so a new platform is purely additive.
 */
export default function LivestreamDestinationBlock({
  dest,
  enabled,
  url,
  streamKey,
  onChange,
  isFirst,
  status,
  statusError,
}: Props) {
  const { t } = useTranslation();
  const [showKey, setShowKey] = useState(false);

  // Map status → colour + accessible label. Each row gets the same
  // visual language as the toolbar Recording / Streaming pills.
  const dot = (() => {
    if (!enabled || !status) return null;
    const map: Record<string, { bg: string; label: string }> = {
      streaming: { bg: "bg-green-500", label: t("livestream.statusStreaming", { defaultValue: "Streaming live" }) },
      failed:    { bg: "bg-red-500",   label: t("livestream.statusFailed",    { defaultValue: "Failed" }) },
      complete:  { bg: "bg-slate-500", label: t("livestream.statusComplete",  { defaultValue: "Last stream completed" }) },
      idle:      { bg: "bg-slate-600", label: t("livestream.statusIdle",      { defaultValue: "Idle" }) },
    };
    const v = map[status] ?? map.idle;
    return (
      <span
        data-testid={`ls-${dest.id}-status`}
        title={statusError ? `${v.label}: ${statusError}` : v.label}
        aria-label={v.label}
        className={`inline-block w-2.5 h-2.5 rounded-full flex-shrink-0 ${v.bg} ${
          status === "streaming" ? "animate-pulse" : ""
        }`}
      />
    );
  })();

  return (
    <div
      className={
        isFirst
          ? "space-y-3"
          : "space-y-3 border-t border-primary-700 pt-3"
      }
    >
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
          <Field id={`ls-${dest.id}-url`} label={t(dest.urlLabel.key, { defaultValue: dest.urlLabel.def })}>
            <Input
              id={`ls-${dest.id}-url`}
              data-testid={`ls-${dest.id}-url`}
              type="url"
              placeholder={dest.urlPlaceholder}
              value={url}
              onChange={(e) => onChange({ enabled, url: e.target.value, streamKey })}
            />
          </Field>
          <Field id={`ls-${dest.id}-key`} label={t(dest.keyLabel.key, { defaultValue: dest.keyLabel.def })}>
            <div className="flex gap-2">
              <Input
                id={`ls-${dest.id}-key`}
                data-testid={`ls-${dest.id}-key`}
                type={showKey ? "text" : "password"}
                placeholder={dest.keyPlaceholder}
                value={streamKey}
                onChange={(e) => onChange({ enabled, url, streamKey: e.target.value })}
              />
              <button
                type="button"
                onClick={() => setShowKey((s) => !s)}
                className="px-2 text-xs text-slate-300 rounded-md bg-primary-800 hover:bg-primary-700 border border-primary-700"
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
        </>
      )}
    </div>
  );
}

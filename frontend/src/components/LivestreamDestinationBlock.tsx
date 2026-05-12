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
}: Props) {
  const { t } = useTranslation();
  const [showKey, setShowKey] = useState(false);

  return (
    <div
      className={
        isFirst
          ? "space-y-3"
          : "space-y-3 border-t border-primary-700 pt-3"
      }
    >
      <Toggle
        id={`ls-${dest.id}-enabled`}
        label={t(dest.toggleI18nKey, { defaultValue: dest.toggleDefault })}
        checked={enabled}
        onChange={(v) => onChange({ enabled: v, url, streamKey })}
      />
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

import {
  Mic,
  Monitor,
  Bell,
  Accessibility,
  Wifi,
  Palette,
  X,
} from "lucide-react";
import { Trans, useTranslation } from "react-i18next";
import { usePreferences } from "../lib/preferences";
import type {
  Layout as LayoutT,
  VideoQuality,
  Theme,
} from "../lib/preferences";
import { Toggle } from "./ui";

interface Props {
  open: boolean;
  onClose: () => void;
}

/**
 * In-meeting settings drawer — a curated subset of the full Settings page,
 * focused on the things you might actually want to flip without leaving the
 * meeting. Lives next to ParticipantsPanel and ChatPanel as an inline drawer
 * so the stage shrinks to fit and the bottom control bar stays visible.
 *
 * Writes go straight into the same `meet-preferences-v1` zustand store as
 * the full Settings page; persistence is automatic.
 */
export default function InMeetingSettings({ open, onClose }: Props) {
  const { t } = useTranslation();
  const prefs = usePreferences();

  if (!open) return null;

  return (
    <aside
      data-testid="in-meeting-settings"
      role="complementary"
      aria-label={t("inMeetingSettings.title")}
      className="h-full w-full sm:w-80 flex-shrink-0 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col"
    >
      <header className="flex items-center justify-between px-4 py-3 border-b border-primary-700">
        <h2 className="text-sm font-semibold text-slate-100">{t("inMeetingSettings.title")}</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("inMeetingSettings.close")}
          data-testid="in-meeting-settings-close"
          className="p-1 rounded hover:bg-primary-700 text-slate-300"
        >
          <X size={18} />
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5 text-sm">
        <Group icon={<Mic size={14} />} title={t("inMeetingSettings.groupAv")}>
          <Toggle
            id="im-cam-default"
            label={t("inMeetingSettings.camOnJoin")}
            checked={prefs.av.cameraOnByDefault}
            onChange={(v) => prefs.setAv({ cameraOnByDefault: v })}
          />
          <Toggle
            id="im-mic-default"
            label={t("inMeetingSettings.micOnJoin")}
            checked={prefs.av.micOnByDefault}
            onChange={(v) => prefs.setAv({ micOnByDefault: v })}
          />
          <Toggle
            id="im-noise"
            label={t("inMeetingSettings.noiseSuppression")}
            checked={prefs.av.noiseSuppression}
            onChange={(v) => prefs.setAv({ noiseSuppression: v })}
          />
          <Toggle
            id="im-echo"
            label={t("inMeetingSettings.echoCancellation")}
            checked={prefs.av.echoCancellation}
            onChange={(v) => prefs.setAv({ echoCancellation: v })}
          />
          <Toggle
            id="im-mirror-preview"
            label={t("inMeetingSettings.mirrorPreview")}
            checked={prefs.av.mirrorPreview}
            onChange={(v) => prefs.setAv({ mirrorPreview: v })}
          />
          <p className="text-xs text-slate-500">{t("inMeetingSettings.rejoinNote")}</p>
        </Group>

        <Group icon={<Monitor size={14} />} title={t("inMeetingSettings.groupDisplay")}>
          <Field label={t("inMeetingSettings.layout")} htmlFor="im-layout">
            <SelectInline
              id="im-layout"
              ariaLabel={t("inMeetingSettings.layout")}
              value={prefs.display.layout}
              onChange={(v) => prefs.setDisplay({ layout: v as LayoutT })}
              options={[
                ["auto", t("inMeetingSettings.layoutAuto")],
                ["grid", t("inMeetingSettings.layoutGrid")],
                ["speaker", t("inMeetingSettings.layoutSpeaker")],
                ["spotlight", t("inMeetingSettings.layoutSpotlight")],
              ]}
            />
          </Field>
          <Toggle
            id="im-mirror-own"
            label={t("inMeetingSettings.mirrorOwn")}
            checked={prefs.display.mirrorOwnVideo}
            onChange={(v) => prefs.setDisplay({ mirrorOwnVideo: v })}
          />
          <Toggle
            id="im-hide-self"
            label={t("inMeetingSettings.hideSelf")}
            checked={prefs.display.hideSelfView}
            onChange={(v) => prefs.setDisplay({ hideSelfView: v })}
          />
          <Toggle
            id="im-show-names"
            label={t("inMeetingSettings.showNames")}
            checked={prefs.display.showParticipantNames}
            onChange={(v) => prefs.setDisplay({ showParticipantNames: v })}
          />
          <Toggle
            id="im-conn-quality"
            label={t("inMeetingSettings.connQuality")}
            checked={prefs.display.showConnectionQuality}
            onChange={(v) => prefs.setDisplay({ showConnectionQuality: v })}
          />
          <Toggle
            id="im-meeting-clock"
            label={t("inMeetingSettings.meetingClock")}
            checked={prefs.display.showMeetingClock}
            onChange={(v) => prefs.setDisplay({ showMeetingClock: v })}
          />
          <Toggle
            id="im-highlight-speaker"
            label={t("inMeetingSettings.highlightSpeaker")}
            checked={prefs.display.highlightSpeaker}
            onChange={(v) => prefs.setDisplay({ highlightSpeaker: v })}
          />
        </Group>

        <Group icon={<Bell size={14} />} title={t("inMeetingSettings.groupNotifications")}>
          <Toggle
            id="im-sound-join"
            label={t("inMeetingSettings.soundOnJoin")}
            checked={prefs.notifications.soundOnJoin}
            onChange={(v) => prefs.setNotifications({ soundOnJoin: v })}
          />
          <Toggle
            id="im-chat-sound"
            label={t("inMeetingSettings.chatSound")}
            checked={prefs.notifications.chatMessageSound}
            onChange={(v) => prefs.setNotifications({ chatMessageSound: v })}
          />
          <Toggle
            id="im-ignore-own"
            label={t("inMeetingSettings.ignoreOwnJoins")}
            checked={prefs.notifications.ignoreOwnJoins}
            onChange={(v) => prefs.setNotifications({ ignoreOwnJoins: v })}
          />
        </Group>

        <Group icon={<Accessibility size={14} />} title={t("inMeetingSettings.groupAccessibility")}>
          <Toggle
            id="im-reduced-motion"
            label={t("inMeetingSettings.reducedMotion")}
            checked={prefs.accessibility.reducedMotion}
            onChange={(v) => prefs.setAccessibility({ reducedMotion: v })}
          />
          <Toggle
            id="im-mono"
            label={t("inMeetingSettings.monoAudio")}
            checked={prefs.accessibility.monoAudio}
            onChange={(v) => prefs.setAccessibility({ monoAudio: v })}
          />
        </Group>

        <Group icon={<Wifi size={14} />} title={t("inMeetingSettings.groupNetwork")}>
          <Field label={t("inMeetingSettings.quality")} htmlFor="im-quality">
            <SelectInline
              id="im-quality"
              ariaLabel={t("inMeetingSettings.quality")}
              value={prefs.network.preferredVideoQuality}
              onChange={(v) =>
                prefs.setNetwork({ preferredVideoQuality: v as VideoQuality })
              }
              options={[
                ["auto", t("inMeetingSettings.qualityAuto")],
                ["low", t("inMeetingSettings.qualityLow")],
                ["medium", t("inMeetingSettings.qualityMedium")],
                ["high", t("inMeetingSettings.qualityHigh")],
              ]}
            />
          </Field>
          <Toggle
            id="im-simulcast"
            label={t("inMeetingSettings.simulcast")}
            checked={prefs.network.simulcastEnabled}
            onChange={(v) => prefs.setNetwork({ simulcastEnabled: v })}
          />
          <Toggle
            id="im-force-relay"
            label={t("inMeetingSettings.forceRelay")}
            checked={prefs.network.forceRelay}
            onChange={(v) => prefs.setNetwork({ forceRelay: v })}
          />
          <p className="text-xs text-slate-500">{t("inMeetingSettings.rejoinNote")}</p>
        </Group>

        <Group icon={<Palette size={14} />} title={t("inMeetingSettings.groupAppearance")}>
          <Field label={t("inMeetingSettings.theme")} htmlFor="im-theme">
            <SelectInline
              id="im-theme"
              ariaLabel={t("inMeetingSettings.theme")}
              value={prefs.appearance.theme}
              onChange={(v) => prefs.setAppearance({ theme: v as Theme })}
              options={[
                ["system", t("inMeetingSettings.themeSystem")],
                ["dark", t("inMeetingSettings.themeDark")],
                ["light", t("inMeetingSettings.themeLight")],
              ]}
            />
          </Field>
          <Toggle
            id="im-compact"
            label={t("inMeetingSettings.compact")}
            checked={prefs.appearance.compactMode}
            onChange={(v) => prefs.setAppearance({ compactMode: v })}
          />
        </Group>

        <p className="text-xs text-slate-500 pt-1">
          <Trans
            i18nKey="inMeetingSettings.moreSettings"
            components={{ 1: <a href="/settings" className="underline" title={t("nav.settings")} /> }}
          />
        </p>
      </div>
    </aside>
  );
}

function Group({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
        <span className="text-slate-500">{icon}</span>
        {title}
      </h3>
      <div className="flex flex-col gap-3">{children}</div>
    </section>
  );
}

function Field({ label, htmlFor, children }: { label: string; htmlFor?: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="block text-xs text-slate-300 mb-1">
        {label}
      </label>
      {children}
    </div>
  );
}

function SelectInline({
  id,
  value,
  onChange,
  options,
  ariaLabel,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
  ariaLabel?: string;
}) {
  return (
    <select
      id={id}
      data-testid={id}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={ariaLabel ?? id}
      className="w-full px-2 py-1.5 rounded-lg bg-primary-800 text-slate-100 border border-primary-700 focus:outline-none focus:ring-2 focus:ring-primary-500 text-sm"
    >
      {options.map(([v, label]) => (
        <option key={v} value={v}>
          {label}
        </option>
      ))}
    </select>
  );
}

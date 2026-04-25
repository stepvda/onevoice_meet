import {
  Mic,
  Monitor,
  Bell,
  Accessibility,
  Wifi,
  Palette,
  X,
} from "lucide-react";
import { usePreferences } from "../lib/preferences";
import type {
  Layout as LayoutT,
  VideoQuality,
  Theme,
  FontSize,
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
  const prefs = usePreferences();

  if (!open) return null;

  return (
    <aside
      data-testid="in-meeting-settings"
      role="complementary"
      aria-label="In-meeting settings"
      className="h-full w-full sm:w-80 flex-shrink-0 bg-primary-900/95 backdrop-blur border-l border-primary-700 flex flex-col"
    >
      <header className="flex items-center justify-between px-4 py-3 border-b border-primary-700">
        <h2 className="text-sm font-semibold text-slate-100">Settings</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close settings"
          data-testid="in-meeting-settings-close"
          className="p-1 rounded hover:bg-primary-700 text-slate-300"
        >
          <X size={18} />
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5 text-sm">
        <Group icon={<Mic size={14} />} title="Audio & Video">
          <Toggle
            id="im-cam-default"
            label="Camera on when I join"
            checked={prefs.av.cameraOnByDefault}
            onChange={(v) => prefs.setAv({ cameraOnByDefault: v })}
          />
          <Toggle
            id="im-mic-default"
            label="Microphone on when I join"
            checked={prefs.av.micOnByDefault}
            onChange={(v) => prefs.setAv({ micOnByDefault: v })}
          />
          <Toggle
            id="im-noise"
            label="Noise suppression"
            checked={prefs.av.noiseSuppression}
            onChange={(v) => prefs.setAv({ noiseSuppression: v })}
          />
          <Toggle
            id="im-echo"
            label="Echo cancellation"
            checked={prefs.av.echoCancellation}
            onChange={(v) => prefs.setAv({ echoCancellation: v })}
          />
          <Toggle
            id="im-mirror-preview"
            label="Mirror my preview"
            checked={prefs.av.mirrorPreview}
            onChange={(v) => prefs.setAv({ mirrorPreview: v })}
          />
        </Group>

        <Group icon={<Monitor size={14} />} title="Display">
          <Field label="Layout">
            <SelectInline
              id="im-layout"
              value={prefs.display.layout}
              onChange={(v) => prefs.setDisplay({ layout: v as LayoutT })}
              options={[
                ["auto", "Auto"],
                ["grid", "Grid"],
                ["speaker", "Active speaker"],
                ["spotlight", "Spotlight"],
              ]}
            />
          </Field>
          <Toggle
            id="im-mirror-own"
            label="Mirror my own video"
            checked={prefs.display.mirrorOwnVideo}
            onChange={(v) => prefs.setDisplay({ mirrorOwnVideo: v })}
          />
          <Toggle
            id="im-hide-self"
            label="Hide self view"
            checked={prefs.display.hideSelfView}
            onChange={(v) => prefs.setDisplay({ hideSelfView: v })}
          />
          <Toggle
            id="im-show-names"
            label="Show participant names"
            checked={prefs.display.showParticipantNames}
            onChange={(v) => prefs.setDisplay({ showParticipantNames: v })}
          />
          <Toggle
            id="im-conn-quality"
            label="Show connection quality"
            checked={prefs.display.showConnectionQuality}
            onChange={(v) => prefs.setDisplay({ showConnectionQuality: v })}
          />
          <Toggle
            id="im-meeting-clock"
            label="Show meeting clock"
            checked={prefs.display.showMeetingClock}
            onChange={(v) => prefs.setDisplay({ showMeetingClock: v })}
          />
          <Toggle
            id="im-highlight-speaker"
            label="Highlight active speaker"
            checked={prefs.display.highlightSpeaker}
            onChange={(v) => prefs.setDisplay({ highlightSpeaker: v })}
          />
        </Group>

        <Group icon={<Bell size={14} />} title="Notifications">
          <Toggle
            id="im-sound-join"
            label="Sound when someone joins"
            checked={prefs.notifications.soundOnJoin}
            onChange={(v) => prefs.setNotifications({ soundOnJoin: v })}
          />
          <Toggle
            id="im-chat-sound"
            label="Sound on chat message"
            checked={prefs.notifications.chatMessageSound}
            onChange={(v) => prefs.setNotifications({ chatMessageSound: v })}
          />
          <Toggle
            id="im-ignore-own"
            label="Ignore my own joins"
            checked={prefs.notifications.ignoreOwnJoins}
            onChange={(v) => prefs.setNotifications({ ignoreOwnJoins: v })}
          />
        </Group>

        <Group icon={<Accessibility size={14} />} title="Accessibility">
          <Toggle
            id="im-captions"
            label="Live captions"
            checked={prefs.accessibility.liveCaptions}
            onChange={(v) => prefs.setAccessibility({ liveCaptions: v })}
          />
          <Field label="Captions font size">
            <SelectInline
              id="im-captions-size"
              value={prefs.accessibility.captionsFontSize}
              onChange={(v) =>
                prefs.setAccessibility({ captionsFontSize: v as FontSize })
              }
              options={[
                ["small", "Small"],
                ["medium", "Medium"],
                ["large", "Large"],
                ["xl", "Extra large"],
              ]}
            />
          </Field>
          <Toggle
            id="im-reduced-motion"
            label="Reduced motion"
            checked={prefs.accessibility.reducedMotion}
            onChange={(v) => prefs.setAccessibility({ reducedMotion: v })}
          />
          <Toggle
            id="im-mono"
            label="Mono audio mix"
            checked={prefs.accessibility.monoAudio}
            onChange={(v) => prefs.setAccessibility({ monoAudio: v })}
          />
        </Group>

        <Group icon={<Wifi size={14} />} title="Network">
          <Field label="Preferred video quality">
            <SelectInline
              id="im-quality"
              value={prefs.network.preferredVideoQuality}
              onChange={(v) =>
                prefs.setNetwork({ preferredVideoQuality: v as VideoQuality })
              }
              options={[
                ["auto", "Auto"],
                ["low", "Low"],
                ["medium", "Medium"],
                ["high", "High"],
              ]}
            />
          </Field>
          <Toggle
            id="im-simulcast"
            label="Simulcast"
            checked={prefs.network.simulcastEnabled}
            onChange={(v) => prefs.setNetwork({ simulcastEnabled: v })}
          />
          <Toggle
            id="im-force-relay"
            label="Force TURN relay"
            checked={prefs.network.forceRelay}
            onChange={(v) => prefs.setNetwork({ forceRelay: v })}
          />
        </Group>

        <Group icon={<Palette size={14} />} title="Appearance">
          <Field label="Theme">
            <SelectInline
              id="im-theme"
              value={prefs.appearance.theme}
              onChange={(v) => prefs.setAppearance({ theme: v as Theme })}
              options={[
                ["system", "System"],
                ["dark", "Dark"],
                ["light", "Light"],
              ]}
            />
          </Field>
          <Toggle
            id="im-compact"
            label="Compact UI"
            checked={prefs.appearance.compactMode}
            onChange={(v) => prefs.setAppearance({ compactMode: v })}
          />
        </Group>

        <p className="text-xs text-slate-500 pt-1">
          More settings live on the full <a href="/settings" className="underline">Settings</a> page.
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-slate-300 mb-1">{label}</div>
      {children}
    </div>
  );
}

function SelectInline({
  id,
  value,
  onChange,
  options,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
}) {
  return (
    <select
      id={id}
      data-testid={id}
      value={value}
      onChange={(e) => onChange(e.target.value)}
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

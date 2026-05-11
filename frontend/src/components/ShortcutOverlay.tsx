import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { X } from "lucide-react";
import { usePreferences } from "../lib/preferences";

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function ShortcutOverlay({ open, onClose }: Props) {
  const { t } = useTranslation();
  const kb = usePreferences((s) => s.keyboard);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const rows: [string, string][] = [
    [t("shortcuts.mute"), kb.muteToggleKey],
    [t("shortcuts.camera"), kb.cameraToggleKey],
    [t("shortcuts.hand"), kb.handRaiseKey],
    [t("shortcuts.screenshare"), kb.screenshareKey],
    [t("shortcuts.leave"), kb.leaveMeetingKey],
    [t("shortcuts.help"), "?"],
  ];

  return (
    <div
      role="dialog"
      aria-modal="true"
      data-testid="shortcut-overlay"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="bg-primary-900 border border-primary-700 rounded-2xl shadow-xl max-w-sm w-full p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between">
          <h2 className="text-lg font-semibold text-slate-50">
            {t("shortcuts.title", { defaultValue: "Keyboard shortcuts" })}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("shortcuts.close", { defaultValue: "Close" })}
            className="p-1 rounded hover:bg-primary-800 text-slate-400"
          >
            <X size={18} />
          </button>
        </div>
        {!kb.enableShortcuts && (
          <p className="mt-2 text-xs text-amber-400">
            {t("shortcuts.disabled", { defaultValue: "Shortcuts are currently disabled in Settings → Keyboard." })}
          </p>
        )}
        <ul className="mt-3 space-y-2 text-sm">
          {rows.map(([label, key]) => (
            <li key={label} className="flex items-center justify-between gap-3">
              <span className="text-slate-300">{label}</span>
              <kbd className="px-2 py-0.5 rounded border border-primary-600 bg-primary-800 text-slate-200 text-xs font-mono whitespace-nowrap">
                {key}
              </kbd>
            </li>
          ))}
        </ul>
        <p className="mt-4 text-xs text-slate-500">
          {t("shortcuts.editHint", { defaultValue: "Change shortcuts on the Settings page (Keyboard section)." })}
        </p>
      </div>
    </div>
  );
}

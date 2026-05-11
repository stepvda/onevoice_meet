import { useEffect } from "react";
import { usePreferences } from "./preferences";

/**
 * Translates a handful of UI-only preferences into `data-*` attributes and
 * CSS custom properties on `<html>`. The matching CSS rules live in
 * styles/global.css. Keeping the mapping centralised here means everything
 * driven by a preference reacts to the same toggle from one place.
 */
export function useUiPrefs() {
  const highContrast = usePreferences((s) => s.accessibility.highContrast);
  const focusOutlines = usePreferences((s) => s.accessibility.keyboardFocusOutlines);
  const roundedAvatars = usePreferences((s) => s.appearance.roundedAvatars);
  const fontSize = usePreferences((s) => s.appearance.fontSize);
  const accentColor = usePreferences((s) => s.appearance.accentColor);
  const backgroundOpacity = usePreferences((s) => s.appearance.backgroundOpacity);

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.highContrast = highContrast ? "true" : "false";
    root.dataset.focusOutlines = focusOutlines ? "on" : "off";
    root.dataset.roundedAvatars = roundedAvatars ? "true" : "false";
    root.dataset.fontSize = fontSize;
    root.style.setProperty("--accent-color", accentColor);
    root.style.setProperty(
      "--bg-opacity",
      String(Math.max(0, Math.min(100, backgroundOpacity)) / 100),
    );
  }, [highContrast, focusOutlines, roundedAvatars, fontSize, accentColor, backgroundOpacity]);
}

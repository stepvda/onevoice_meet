import { useEffect } from "react";
import { usePreferences } from "./preferences";
import { useEmbed } from "./embed";
import { applyEmbedTheme, applyScheme, type SchemeMode } from "./scheme";

function systemMode(): SchemeMode {
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/**
 * Applies the colour scheme and `appearance.theme` to the document root:
 * `data-scheme="meet"|"mail"`, `data-theme="light"|"dark"` and the matching
 * `light` / `dark` class (lib/scheme.ts, styles/schemes.css).
 *
 * - Meet scheme (default): the stylesheet is dark; the `light` theme is still
 *   a no-op there, but the attribute stays useful for downstream styling.
 * - Mail scheme (Settings → Colour scheme): One Voice Mail's light or dark
 *   colours — the first real light mode.
 * - Embedded in Mail: scheme `mail` is forced and the mode and tokens come
 *   from the parent (`ovm:init`, `ovm:theme`), live.
 */
export function useThemePref() {
  const theme = usePreferences((s) => s.appearance.theme);
  const scheme = usePreferences((s) => s.appearance.scheme) ?? "meet";
  const { status, theme: parentTheme } = useEmbed();
  useEffect(() => {
    const root = document.documentElement;
    if (status !== "off") {
      root.dataset.embed = "ovm";
      const current = root.dataset.theme;
      applyEmbedTheme(root, parentTheme, current === "dark" || current === "light" ? current : systemMode());
      return;
    }
    delete root.dataset.embed;
    const apply = () => applyScheme(root, scheme, theme === "system" ? systemMode() : theme);
    apply();
    if (theme !== "system") return;
    const mql = window.matchMedia("(prefers-color-scheme: light)");
    mql.addEventListener("change", apply);
    return () => mql.removeEventListener("change", apply);
  }, [theme, scheme, status, parentTheme]);
}

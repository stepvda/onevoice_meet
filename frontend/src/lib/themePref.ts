import { useEffect } from "react";
import { usePreferences } from "./preferences";

/**
 * Applies `appearance.theme` by toggling a `light` or `dark` class on the
 * document root. The app's stylesheet defaults to dark; the `light` class is
 * a no-op until/unless light-theme CSS is added, but the data attribute is
 * still useful for downstream styling.
 */
export function useThemePref() {
  const theme = usePreferences((s) => s.appearance.theme);
  useEffect(() => {
    const root = document.documentElement;
    const apply = () => {
      const effective =
        theme === "system"
          ? window.matchMedia("(prefers-color-scheme: light)").matches
            ? "light"
            : "dark"
          : theme;
      root.classList.toggle("light", effective === "light");
      root.classList.toggle("dark", effective === "dark");
      root.dataset.theme = effective;
    };
    apply();
    if (theme !== "system") return;
    const mql = window.matchMedia("(prefers-color-scheme: light)");
    mql.addEventListener("change", apply);
    return () => mql.removeEventListener("change", apply);
  }, [theme]);
}

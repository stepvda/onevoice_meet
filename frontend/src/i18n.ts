/**
 * i18next setup for meet.witysk.org.
 *
 * Loads translation JSON lazily from /locales/<lang>.json so the bundle
 * doesn't ship 50 languages worth of strings to every visitor.
 *
 * Language is decided in this order:
 *   1. Server-side per-user preference (only if `language_set_manually=true`).
 *      Fetched after init via `syncServerLanguage()` once a JWT is available.
 *   2. The user's saved preference at `meet-preferences-v1.state.locale.language`
 *      (writes here happen alongside server PUTs in `setLocale`).
 *   3. The browser's navigator.language (via i18next-browser-languagedetector).
 *   4. Fallback: English.
 *
 * For authenticated users, `syncServerLanguage()` honours the
 * `language_set_manually` flag: when false, the browser language wins on
 * every page load; when true, the server-stored language overrides it.
 */
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import resourcesToBackend from "i18next-resources-to-backend";

export const SUPPORTED_LANGS = [
  "am", "ar", "bg", "bn", "cs", "da", "de", "el", "en", "es",
  "fa", "fi", "fil", "fr", "gu", "ha", "he", "hi", "hr", "hu",
  "id", "it", "ja", "kk", "ko", "mr", "ms", "my", "ne", "nl",
  "no", "pa", "pl", "pt", "ro", "ru", "sk", "sl", "sr", "sv",
  "sw", "ta", "te", "th", "tr", "uk", "ur", "uz", "vi", "zh",
] as const;
export type SupportedLang = (typeof SUPPORTED_LANGS)[number];

function readPreferredLang(): string | null {
  try {
    const raw = localStorage.getItem("meet-preferences-v1");
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed?.state?.locale?.language ?? null;
  } catch {
    return null;
  }
}

void i18n
  .use(
    resourcesToBackend(
      (lang: string, _ns: string) =>
        fetch(`/locales/${lang}.json`).then((r) => {
          if (!r.ok) throw new Error(`failed to load locale ${lang}: ${r.status}`);
          return r.json();
        })
    )
  )
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    fallbackLng: "en",
    supportedLngs: [...SUPPORTED_LANGS],
    nonExplicitSupportedLngs: true, // accept "en-US" → "en"
    lng: readPreferredLang() || undefined,
    interpolation: { escapeValue: false }, // React handles escaping
    detection: {
      order: ["querystring", "localStorage", "navigator"],
      lookupLocalStorage: "i18nextLng",
      caches: ["localStorage"],
    },
    react: { useSuspense: false },
  });

/**
 * After login, ask the API for the user's saved language preference.
 *
 * - If `language_set_manually` is true and the server has a language, switch
 *   to it (overriding whatever was loaded from localStorage / browser).
 * - If `language_set_manually` is false, do nothing — the browser-detected
 *   language stays in effect, and we leave the server flag alone until the
 *   user explicitly picks a language in Settings.
 *
 * Errors are swallowed: we don't want a flaky API to block i18n.
 */
export async function syncServerLanguage(): Promise<void> {
  try {
    const { api } = await import("./lib/api");
    const prefs = await api.getMyPreferences();
    if (prefs.language_set_manually && prefs.language && prefs.language !== i18n.language) {
      await i18n.changeLanguage(prefs.language);
      // Mirror into the local zustand store so Settings reflects the value.
      const { usePreferences } = await import("./lib/preferences");
      usePreferences.setState((s) => ({
        locale: { ...s.locale, language: prefs.language as never },
      }));
    }
  } catch {
    /* offline / unauthenticated / API down — keep current language */
  }
}

export default i18n;

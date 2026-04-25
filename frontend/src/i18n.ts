/**
 * i18next setup for meet.witysk.org.
 *
 * Loads translation JSON lazily from /locales/<lang>.json so the bundle
 * doesn't ship 50 languages worth of strings to every visitor.
 *
 * Language is decided in this order:
 *   1. The user's saved preference at `meet-preferences-v1.state.locale.language`
 *      (matches the existing Settings → Language pref).
 *   2. The browser's navigator.language (via i18next-browser-languagedetector).
 *   3. Fallback: English.
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

export default i18n;

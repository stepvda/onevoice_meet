import { useEffect, useRef } from "react";
import { usePreferences } from "./preferences";
import { isAuthenticated } from "./auth";
import { api } from "./api";

/**
 * Mirror the user's privacy preferences to the backend so server-side
 * enforcement (anonymise email in join log, etc.) can read them at request
 * time. Debounced so successive toggles don't flood the API.
 */
export function usePrivacyServerSync() {
  const anonymise = usePreferences((s) => s.privacy.anonymiseEmailInJoinLog);
  const dontLogIp = usePreferences((s) => s.privacy.dontLogMyIp);
  const last = useRef<{ a: boolean; d: boolean } | null>(null);

  useEffect(() => {
    if (!isAuthenticated()) return;
    const next = { a: anonymise, d: dontLogIp };
    if (last.current && last.current.a === next.a && last.current.d === next.d) return;
    const id = window.setTimeout(() => {
      api
        .updateMyPreferences({
          anonymise_email_in_join_log: anonymise,
          dont_log_my_ip: dontLogIp,
        })
        .catch(() => {
          /* offline / API down — local pref is the source of truth */
        });
      last.current = next;
    }, 400);
    return () => window.clearTimeout(id);
  }, [anonymise, dontLogIp]);
}

/**
 * Applies `privacy.blurEmailInScreenshots` as a `data-blur-emails="true"`
 * attribute on the document root. The .privacy-blur CSS class (in
 * global.css) reads this attribute to blur targeted nodes.
 */
export function usePrivacyClassNames() {
  const blurEmails = usePreferences((s) => s.privacy.blurEmailInScreenshots);
  const disableAnalytics = usePreferences((s) => s.privacy.disableAnalytics);
  useEffect(() => {
    const root = document.documentElement;
    root.dataset.blurEmails = blurEmails ? "true" : "false";
    root.dataset.analyticsDisabled = disableAnalytics ? "true" : "false";
  }, [blurEmails, disableAnalytics]);
}

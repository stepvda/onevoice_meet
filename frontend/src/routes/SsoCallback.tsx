/**
 * SSO callback — the landing page after one.witysk.org/sso-redirect.html
 * bounces the user back here.
 *
 * The access token arrives in the URL fragment (`#access_token=…`), which is
 * never sent to the server. We read it, store it, scrub it from the address
 * bar via history.replaceState, kick off a server-side language sync, and
 * navigate to whatever the original `?next=` was (or `/`).
 */
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { setAccessToken } from "../lib/auth";
import { syncServerLanguage } from "../i18n";

export default function SsoCallback() {
  const navigate = useNavigate();

  useEffect(() => {
    const search = new URLSearchParams(window.location.search);
    const next = sanitizeNext(search.get("next"));
    const fragment = window.location.hash.startsWith("#")
      ? window.location.hash.slice(1)
      : "";
    const params = new URLSearchParams(fragment);
    const token = params.get("access_token");
    // sso_error=not_authenticated is the other possible fragment value when
    // the user isn't currently logged into one.witysk.org. We don't need to
    // read it explicitly — falling through to the default navigation below
    // takes the user back to whatever page rendered the sign-in prompt,
    // where they'll see the prompt again.

    if (token) {
      setAccessToken(token);
      // Strip the fragment from the URL bar BEFORE navigating, so the token
      // doesn't sit in browser history.
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
      // Pull the user's language preference from meet-api now that we have
      // a fresh token.
      void syncServerLanguage();
    }

    // If sso_error=not_authenticated, we just navigate back; the destination
    // page renders its existing "sign in" prompt.

    // Use a tiny delay so React has a tick to absorb the state change.
    window.setTimeout(() => navigate(next, { replace: true }), 0);
    // We intentionally suppress the eslint deps warning: we want this to run
    // exactly once, on mount, regardless of route re-renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="p-8 text-center text-slate-300" data-testid="sso-callback">
      <p>Signing you in…</p>
    </div>
  );
}

/** Only allow same-origin path-with-search; reject absolute URLs to prevent
 *  open-redirect via the `next` parameter. */
function sanitizeNext(raw: string | null): string {
  if (!raw) return "/";
  // Must start with a single `/` and not be `//host` (which would be a
  // protocol-relative URL the browser would treat as cross-origin).
  if (!raw.startsWith("/") || raw.startsWith("//")) return "/";
  return raw;
}

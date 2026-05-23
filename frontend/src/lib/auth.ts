/**
 * Auth integration with one.witysk.org.
 *
 * localStorage is per-origin, so we cannot directly read one.witysk.org's
 * `access_token` from meet.witysk.org. We bootstrap via a hidden iframe that
 * loads https://one.witysk.org/sso-bootstrap.html; that page reads its own
 * localStorage and posts the token back via postMessage with an explicit
 * targetOrigin.
 *
 * Once received, we mirror the token into meet.witysk.org's own localStorage
 * so subsequent visits are fast (no iframe round-trip on every page load).
 */

const ONE_WITYSK = "https://one.witysk.org";
const STORAGE_KEY = "access_token";
// Iframe SSO is the silent fast-path for desktop browsers that allow third-
// party storage access. On Safari / mobile / private modes it can't read
// one.witysk.org's localStorage at all, so we fail fast and rely on the
// explicit redirect-based flow (see startSsoRedirect below) instead.
const BOOTSTRAP_TIMEOUT_MS = 1500;

export function getAccessToken(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setAccessToken(token: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, token);
  } catch {
    /* ignore */
  }
}

export function clearAccessToken(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

export function isAuthenticated(): boolean {
  return !!getAccessToken();
}

/**
 * Top-level redirect SSO. Use this whenever the silent iframe bootstrap is
 * known to have failed or is too unreliable (mobile Safari, ITP-strict
 * browsers, private browsing).
 *
 * Flow:
 *   1. We navigate the user to one.witysk.org/sso-redirect.html?return_url=…
 *   2. That page reads its own first-party access_token from localStorage
 *      (works on every browser because top-level navigation is never
 *      treated as third-party).
 *   3. It bounces back to /sso-callback#access_token=<token> here.
 *   4. SsoCallback reads the fragment, stores the token, and navigates the
 *      user back to where they came from.
 *
 * Pass `returnTo` as a path-with-search (default: current URL) and we'll
 * land the user there after sign-in completes.
 */
export function startSsoRedirect(returnTo?: string): void {
  const ret = returnTo ?? window.location.pathname + window.location.search;
  const callback = new URL("/sso-callback", window.location.origin);
  callback.searchParams.set("next", ret);
  const url = new URL(`${ONE_WITYSK}/sso-redirect.html`);
  url.searchParams.set("return_url", callback.toString());
  // Preserve the original navigation target via replace() so the user can
  // hit Back from the destination page and land where they were before.
  window.location.assign(url.toString());
}

/**
 * Log out everywhere — invalidate one.witysk.org's server-side sessions,
 * clear its localStorage, then clear meet's localStorage. Resolves once both
 * sides are clean (or the timeout fires).
 *
 * Implementation: load a hidden iframe to one.witysk.org/sso-bootstrap.html,
 * send `witysk-sso-logout`. The iframe POSTs /api/auth/logout (same-origin)
 * and clears its localStorage, then posts back `{ logout: "ok" | "failed" }`.
 * Either way we clear meet's local cache so the user is signed out here.
 */
export function logoutFromOneWitysk(): Promise<{ ok: boolean }> {
  return new Promise((resolve) => {
    let done = false;
    let iframe: HTMLIFrameElement | null = null;

    function finish(ok: boolean) {
      if (done) return;
      done = true;
      window.removeEventListener("message", onMessage);
      try {
        if (iframe) iframe.remove();
      } catch {
        /* ignore */
      }
      clearAccessToken();
      try {
        // Clear any ancillary keys we use, just in case.
        localStorage.removeItem("refresh_token");
      } catch {
        /* ignore */
      }
      // Tear down the global Café audio session if it's running. The
      // TICafeProvider listens for this event and disconnects gracefully.
      try {
        window.dispatchEvent(new Event("ti-cafe-logout"));
      } catch {
        /* ignore */
      }
      resolve({ ok });
    }

    const onMessage = (ev: MessageEvent) => {
      if (ev.origin !== ONE_WITYSK) return;
      if (!ev.data || typeof ev.data !== "object") return;
      if ((ev.data as { type?: string }).type !== "witysk-sso") return;
      const logout = (ev.data as { logout?: string }).logout;
      if (logout) {
        finish(logout === "ok");
      }
    };

    iframe = document.createElement("iframe");
    iframe.src = `${ONE_WITYSK}/sso-bootstrap.html`;
    iframe.style.display = "none";
    iframe.setAttribute("aria-hidden", "true");
    iframe.setAttribute("title", "SSO logout");
    iframe.addEventListener("load", () => {
      try {
        iframe?.contentWindow?.postMessage(
          { type: "witysk-sso-logout" },
          ONE_WITYSK
        );
      } catch {
        finish(false);
      }
    });
    window.addEventListener("message", onMessage);
    document.body.appendChild(iframe);

    // Hard timeout — if one.witysk.org doesn't respond, still log out locally.
    window.setTimeout(() => finish(false), 6000);
  });
}

/** One.witysk.org user shape returned by `/api/admin/users/{user_id}`.
 * Mirrors the AdminUserDetail Pydantic model on the onevoice backend; only
 * the fields the meet admin panel actually surfaces are typed here. */
export interface OneWityskUserDetail {
  id: number;
  username: string;
  email: string;
  name: string | null;
  is_admin: boolean;
  is_disabled: boolean;
  email_verified: boolean;
  twofa_enabled: boolean;
  created_at: string | null;
  facepic_path: string | null;
  city: string | null;
  country: string | null;
  last_activity: string | null;
}

/**
 * Admin lookup of an arbitrary one.witysk.org user by their numeric user_id.
 *
 * The meet admin panel uses this to enrich SSO rows in the user list (the
 * `external_id` we store *is* the one.witysk.org user_id). Auth is the
 * same bearer token that meet itself uses — bound to the browser's IP, so
 * this call MUST happen from the browser, not from meet's backend.
 *
 * Returns `null` when:
 *  - no token is available
 *  - the caller isn't an admin on one.witysk.org (403)
 *  - the user doesn't exist on one.witysk.org (404)
 *  - the network/JWT failed for any other reason
 *
 * Does NOT throw — callers can safely render a "—" placeholder on null.
 */
export async function fetchOneWityskUser(userId: number | string): Promise<OneWityskUserDetail | null> {
  const tok = getAccessToken();
  if (!tok) return null;
  try {
    const res = await fetch(`${ONE_WITYSK}/api/admin/users/${encodeURIComponent(String(userId))}`, {
      headers: { Authorization: `Bearer ${tok}` },
      credentials: "omit",
    });
    if (!res.ok) return null;
    return (await res.json()) as OneWityskUserDetail;
  } catch {
    return null;
  }
}

/**
 * Fetch the signed-in user's preferred display name from one.witysk.org's
 * `/api/auth/me`. Returns `name || username || email || null`.
 *
 * Browser-to-server: the JWT is bound to the browser's IP, so the call MUST
 * originate from the user's browser (a server-to-server call from meet's
 * backend would either fail validation or trip session-revocation rules).
 *
 * Not cached: callers should hit this every time they need a fresh name
 * (e.g. on meeting creation, every owner-token mint, and invite-send),
 * because the user may have updated their preferred name since last fetch.
 *
 * Requires `https://meet.witysk.org` in one.witysk.org's CORS allow_origins.
 */
export async function fetchOneWityskName(): Promise<string | null> {
  const me = await fetchOneWityskMe();
  if (!me) return null;
  return me.name || me.username || me.email || null;
}

/** What `https://one.witysk.org/api/auth/me` returns. Subset of fields
 *  the meet SPA actually needs for pre-filling forms. */
export interface OneWityskMe {
  name: string | null;
  username: string | null;
  email: string | null;
}

/** Fetch both the display name AND email from one.witysk.org. Used when
 *  meet's own `/v1/me` returns null fields for an SSO user (the meet
 *  account row is auto-provisioned with only the external_id; the
 *  human-readable fields live on one.witysk.org).
 *
 *  Returns `null` on any failure — callers can render placeholders. */
export async function fetchOneWityskMe(): Promise<OneWityskMe | null> {
  const tok = getAccessToken();
  if (!tok) return null;
  try {
    const res = await fetch(`${ONE_WITYSK}/api/auth/me`, {
      headers: { Authorization: `Bearer ${tok}` },
      credentials: "omit",
    });
    if (!res.ok) return null;
    const j = (await res.json()) as {
      name?: string | null;
      username?: string | null;
      email?: string | null;
    };
    return {
      name: j.name ?? null,
      username: j.username ?? null,
      email: j.email ?? null,
    };
  } catch {
    return null;
  }
}

let bootstrapInFlight: Promise<string | null> | null = null;

/**
 * Attempt to pull an access token from one.witysk.org via a hidden iframe.
 * Idempotent: subsequent calls while the first is in-flight share the promise.
 * Always resolves — returns null on failure/timeout/not-logged-in.
 */
export function bootstrapFromOneWitysk(): Promise<string | null> {
  const existing = getAccessToken();
  if (existing) return Promise.resolve(existing);

  if (bootstrapInFlight) return bootstrapInFlight;

  bootstrapInFlight = new Promise((resolve) => {
    const iframe = document.createElement("iframe");
    iframe.src = `${ONE_WITYSK}/sso-bootstrap.html`;
    iframe.style.display = "none";
    iframe.setAttribute("aria-hidden", "true");
    iframe.setAttribute("title", "SSO bootstrap");

    let done = false;
    const finish = (token: string | null) => {
      if (done) return;
      done = true;
      window.removeEventListener("message", onMessage);
      try {
        iframe.remove();
      } catch {
        /* ignore */
      }
      bootstrapInFlight = null;
      if (token) setAccessToken(token);
      resolve(token);
    };

    const onMessage = (ev: MessageEvent) => {
      if (ev.origin !== ONE_WITYSK) return;
      if (!ev.data || typeof ev.data !== "object") return;
      if ((ev.data as { type?: string }).type !== "witysk-sso") return;
      const token = (ev.data as { access_token?: string | null }).access_token;
      finish(typeof token === "string" && token.length > 0 ? token : null);
    };

    window.addEventListener("message", onMessage);
    document.body.appendChild(iframe);

    window.setTimeout(() => finish(null), BOOTSTRAP_TIMEOUT_MS);
  });

  return bootstrapInFlight;
}

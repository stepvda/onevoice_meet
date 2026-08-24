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
// 4s (was 1500ms): the bootstrap page now REFRESHES an expired token before
// handing it over (it holds the httpOnly refresh cookie), which can add a
// same-origin round-trip, so the parent must wait a little longer.
const BOOTSTRAP_TIMEOUT_MS = 4000;

/** Decoded-payload view of a JWT — `exp`/`iss` only, no signature check (the
 *  backend does that). Returns null when the token is unparseable. */
function tokenClaims(token: string | null): { exp?: number; iss?: string } | null {
  if (!token) return null;
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(part)) as { exp?: number; iss?: string };
  } catch {
    return null;
  }
}

/** True if the JWT is missing, unparseable, or within 10s of expiry.
 *  This just stops us from reusing a token the server will reject. */
function isTokenExpired(token: string | null): boolean {
  const claims = tokenClaims(token);
  if (!claims || typeof claims.exp !== "number") return true;
  return claims.exp * 1000 <= Date.now() + 10_000;
}

/** The cached access token, or null if absent OR EXPIRED. An expired cached
 *  token is cleared so callers (isAuthenticated / bootstrapFromOneWitysk) treat
 *  it as "not signed in" and re-bootstrap a fresh one from one.witysk.org,
 *  instead of blindly sending a dead token that 401s with "Signature has
 *  expired". This is the fix for the stale-token loop. */
export function getAccessToken(): string | null {
  try {
    const tok = localStorage.getItem(STORAGE_KEY);
    if (tok && isTokenExpired(tok)) {
      try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
      return null;
    }
    return tok;
  } catch {
    return null;
  }
}

// Sticky "the user has signed in here before" marker. Survives the token
// itself being cleared on expiry, so the keep-alive knows a silent SSO
// re-bootstrap is worth attempting (vs. spawning iframes for a visitor who
// never signed in). Cleared only on explicit logout.
const HAD_SESSION_KEY = "meet_had_session";

function hadSession(): boolean {
  try {
    return localStorage.getItem(HAD_SESSION_KEY) === "1";
  } catch {
    return false;
  }
}

export function setAccessToken(token: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, token);
    localStorage.setItem(HAD_SESSION_KEY, "1");
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
        // Clear any ancillary keys we use, just in case. HAD_SESSION_KEY
        // goes too — an explicit logout means the keep-alive must NOT
        // silently re-bootstrap a new session behind the user's back.
        localStorage.removeItem("refresh_token");
        localStorage.removeItem(HAD_SESSION_KEY);
        localStorage.removeItem(PROFILE_KEY);
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
  // **Never** fall back to the email address — this value gets passed
  // into LiveKit `participant.name` (via `api.ownerToken` and friends)
  // and shows up on every viewer's screen + the participants panel.
  // If we have no display name, return null and let the backend pick
  // a safe placeholder (`User <sub>`) instead of leaking the email
  // address of the meeting host.
  return me.name || me.username || null;
}

/** What `https://one.witysk.org/api/auth/me` returns. Subset of fields
 *  the meet SPA actually needs for pre-filling forms. */
export interface OneWityskMe {
  name: string | null;
  username: string | null;
  email: string | null;
}

// Profile snapshot delivered alongside the token by one.witysk.org's
// sso-bootstrap page. Needed because one.witysk.org sessions can be
// DPoP-bound: resource calls require a proof signed with a key that lives
// only in one.witysk.org's IndexedDB, so meet's own cross-origin call to
// /api/auth/me is rejected for bound sessions. The bootstrap iframe fetches
// the profile same-origin (where it CAN sign the proof) and posts it here.
const PROFILE_KEY = "witysk_profile";

function cacheWityskProfile(p: OneWityskMe): void {
  try {
    localStorage.setItem(PROFILE_KEY, JSON.stringify(p));
  } catch {
    /* ignore */
  }
}

export function getCachedWityskProfile(): OneWityskMe | null {
  try {
    const raw = localStorage.getItem(PROFILE_KEY);
    if (!raw) return null;
    const j = JSON.parse(raw) as Partial<OneWityskMe>;
    return { name: j.name ?? null, username: j.username ?? null, email: j.email ?? null };
  } catch {
    return null;
  }
}

/** Fetch both the display name AND email from one.witysk.org. Used when
 *  meet's own `/v1/me` returns null fields for an SSO user (the meet
 *  account row is auto-provisioned with only the external_id; the
 *  human-readable fields live on one.witysk.org).
 *
 *  Returns `null` on any failure — callers can render placeholders. */
/** Fallback when the direct /api/auth/me call fails: the cached profile
 *  from a previous bootstrap handoff, or — when the cache is still empty —
 *  a forced iframe bootstrap, whose message handler caches the profile the
 *  bootstrap page fetched same-origin (where DPoP-bound sessions work). */
async function cachedProfileOrBootstrap(): Promise<OneWityskMe | null> {
  const cached = getCachedWityskProfile();
  if (cached && (cached.name || cached.username || cached.email)) return cached;
  await forceBootstrapFromOneWitysk();
  return getCachedWityskProfile();
}

export async function fetchOneWityskMe(): Promise<OneWityskMe | null> {
  const tok = getAccessToken();
  // DPoP-bound one.witysk.org sessions reject this cross-origin call (meet
  // cannot sign the proof — the key is non-extractable in one.witysk.org's
  // IndexedDB), so on ANY failure fall back to the profile snapshot the
  // sso-bootstrap iframe delivered with the token handoff.
  if (!tok) return getCachedWityskProfile();
  try {
    const res = await fetch(`${ONE_WITYSK}/api/auth/me`, {
      headers: { Authorization: `Bearer ${tok}` },
      credentials: "omit",
    });
    if (!res.ok) return cachedProfileOrBootstrap();
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
    return cachedProfileOrBootstrap();
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
  return forceBootstrapFromOneWitysk();
}

/**
 * Like bootstrapFromOneWitysk, but SKIPS the cached-token short-circuit —
 * always asks the one.witysk.org iframe for its current token. Two callers
 * need this: the 401 retry in api.ts (our cached token was just rejected,
 * so returning it again is useless) and the session keep-alive (which
 * wants a fresher token than the one we hold). As a side effect every
 * call touches one.witysk.org's own auth endpoint, which keeps the SSO
 * refresh-cookie session warm too.
 */
export function forceBootstrapFromOneWitysk(): Promise<string | null> {
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
      const d = ev.data as {
        access_token?: string | null;
        name?: string | null;
        username?: string | null;
        email?: string | null;
      };
      // Newer sso-bootstrap builds piggyback the user's profile on the token
      // handoff (fetched same-origin, where DPoP-bound sessions still work).
      // Cache it; fetchOneWityskMe falls back to this when its own
      // cross-origin call is rejected. Only overwrite on a non-empty payload.
      if (d.name || d.username || d.email) {
        cacheWityskProfile({
          name: d.name ?? null,
          username: d.username ?? null,
          email: d.email ?? null,
        });
      }
      const token = d.access_token;
      finish(typeof token === "string" && token.length > 0 ? token : null);
    };

    window.addEventListener("message", onMessage);
    document.body.appendChild(iframe);

    window.setTimeout(() => finish(null), BOOTSTRAP_TIMEOUT_MS);
  });

  return bootstrapInFlight;
}

// ---------------------------------------------------------------------------
// Session keep-alive
//
// Why: an SSO access token can expire while the user sits on one screen for
// hours (a long meeting is the canonical case). Media keeps flowing — the
// LiveKit token is separate — but the next REST call (e.g. "Stop recording")
// 401s. Worse, getAccessToken() clears the expired token, so that call went
// out with NO Authorization header, whose 401 detail didn't match the retry
// guard in api.ts. The keep-alive makes this a non-event by renewing the
// token BEFORE it expires, on every screen, in or out of a meeting.
// ---------------------------------------------------------------------------

/** Raw stored token, even if already expired — the keep-alive needs to see
 *  expired tokens (to know renewal is due) without the clearing side effect
 *  of getAccessToken(). */
function getStoredTokenAny(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Exchange the current (still-valid) token for a fresh meet-minted one via
 *  POST /api/v1/auth/refresh. Works for native accounts and as an SSO
 *  fallback when the one.witysk.org iframe is unavailable. Returns true on
 *  success. Plain fetch — api.ts imports this module, not vice versa. */
export async function refreshMeetSession(): Promise<boolean> {
  const tok = getStoredTokenAny();
  if (!tok) return false;
  try {
    const res = await fetch("/api/v1/auth/refresh", {
      method: "POST",
      headers: { Authorization: `Bearer ${tok}` },
    });
    if (!res.ok) return false;
    const j = (await res.json()) as { access_token?: string };
    if (j.access_token) {
      setAccessToken(j.access_token);
      return true;
    }
    return false;
  } catch {
    return false;
  }
}

const KEEPALIVE_TICK_MS = 4 * 60_000; // check every 4 minutes
const RENEW_MARGIN_MS = 15 * 60_000; // renew when < 15 minutes remain

let keepAliveStarted = false;

/**
 * Start the app-wide session keep-alive. Call once at app boot; subsequent
 * calls are no-ops. Every 4 minutes (plus on tab-visible and network-online
 * transitions) it checks the stored token and renews it before expiry:
 *
 *   - one.witysk.org SSO token → force a fresh iframe bootstrap (also keeps
 *     the SSO server session alive); backend /auth/refresh as fallback
 *     while the old token is still valid.
 *   - meet-native / meet-refreshed token → backend /auth/refresh.
 *   - token already gone but the user had a session (expired + cleared) →
 *     silent SSO re-bootstrap, so even a laptop waking from sleep recovers.
 */
export function startSessionKeepAlive(): void {
  if (keepAliveStarted) return;
  keepAliveStarted = true;

  const tick = async (): Promise<void> => {
    const tok = getStoredTokenAny();
    if (!tok) {
      // Nothing stored. If this browser had a session before, an SSO
      // re-bootstrap may restore it silently (native logins can't be
      // restored without credentials — those users see the login page).
      if (hadSession()) await forceBootstrapFromOneWitysk();
      return;
    }
    const claims = tokenClaims(tok);
    if (!claims || typeof claims.exp !== "number") return;
    const msLeft = claims.exp * 1000 - Date.now();
    if (msLeft > RENEW_MARGIN_MS) return; // healthy — nothing to do

    const meetMinted = claims.iss === "meet" || claims.iss === "meet-sso";
    if (meetMinted) {
      // Backend refresh needs the token to still be valid — do it first.
      if (msLeft > 60_000 && (await refreshMeetSession())) return;
      // meet-sso users are SSO users at heart: the iframe can restore them.
      if (claims.iss === "meet-sso") await forceBootstrapFromOneWitysk();
      return;
    }
    // Genuine one.witysk.org token: prefer a fresh SSO token (keeps the SSO
    // session alive server-side); fall back to a meet-minted continuation
    // while the current token is still accepted.
    const fresh = await forceBootstrapFromOneWitysk();
    if (!fresh && msLeft > 60_000) await refreshMeetSession();
  };

  window.setInterval(() => void tick(), KEEPALIVE_TICK_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void tick();
  });
  window.addEventListener("online", () => void tick());
  void tick();
}

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
const BOOTSTRAP_TIMEOUT_MS = 4000;

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

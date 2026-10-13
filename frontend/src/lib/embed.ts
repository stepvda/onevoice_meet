/**
 * Meet embedded in One Voice Mail (contract meet-integration.md §2.2, §2.3).
 *
 * Mail shows Meet in an iframe whose URL carries `?embed=ovm&parent=<Mail
 * origin>`. Embed mode is on when
 *   1. the URL has `embed=ovm` and `parent` (or this tab already validated a
 *      parent: sessionStorage `meet_embed_parent`, so navigation and reloads
 *      inside the iframe stay embedded),
 *   2. the page is framed, and
 *   3. the parent origin is in `GET /api/v1/embed/config` → `parent_origins`.
 *
 * While the allowlist check runs the status is "pending": One Voice SSO must
 * not start yet, and nothing is posted to or accepted from the parent.
 *
 * Protocol (version 1, every message `{ ovm: 1, type, ... }`):
 *   Meet → Mail  meet:ready, meet:token-request {requestId},
 *                meet:location {path, title?}, meet:call {inCall, room?, title?},
 *                meet:open-mail {path}
 *   Mail → Meet  ovm:init {theme, token, expiresAt, user}, ovm:token
 *                {requestId, token, expiresAt | error}, ovm:theme {theme},
 *                ovm:navigate {path}
 * Messages are accepted only from `window.parent` with `event.origin` equal to
 * the validated parent, and posted only with that exact targetOrigin.
 *
 * Auth: the access token comes from the parent (never One Voice SSO). It is
 * kept in memory and in this tab's sessionStorage — not in localStorage,
 * which meet.witysk.org shares with stand-alone Meet tabs of the same
 * browser (Mail and Meet are same-site, so the iframe's storage is not
 * partitioned) — and renewed through `meet:token-request` two minutes before
 * it expires and after a 401.
 */
import { useSyncExternalStore } from "react";

export const EMBED_PARENT_KEY = "meet_embed_parent";
export const EMBED_TOKEN_KEY = "meet_embed_token";

/** Ask for a new token this long before the current one expires. */
export const RENEW_BEFORE_MS = 2 * 60_000;
/** A token request the parent does not answer resolves null after this. */
export const TOKEN_REQUEST_TIMEOUT_MS = 15_000;
/** After a failed renewal, try again after this long. */
export const RENEW_RETRY_MS = 30_000;
/** Never renew sooner than this after receiving a token (no tight loops). */
export const MIN_RENEW_DELAY_MS = 5_000;
/** `meet:ready` is re-posted at these delays until `ovm:init` arrives. */
export const READY_RETRY_MS = [1_000, 2_000, 4_000, 8_000] as const;
/** How long the allowlist lookup may take before embed mode is refused. */
export const CONFIG_TIMEOUT_MS = 8_000;
/** A token is treated as expired this long before `expiresAt`. */
const EXPIRY_SKEW_MS = 10_000;

export type EmbedStatus = "off" | "pending" | "on";

export interface EmbedUser {
  name: string;
  email: string;
}

export interface EmbedTheme {
  scheme: "mail";
  mode: "light" | "dark";
  tokens: Record<string, string>;
}

export interface EmbedSnapshot {
  status: EmbedStatus;
  /** The validated (or, while pending, the candidate) parent origin. */
  parent: string | null;
  user: EmbedUser | null;
  theme: EmbedTheme | null;
  /** `ovm:init` was received. */
  initialised: boolean;
}

export interface MessageLike {
  source: unknown;
  origin: string;
  data: unknown;
}

export interface PostTarget {
  postMessage(message: unknown, targetOrigin: string): void;
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** What the controller needs from the browser (faked in unit tests). */
export interface EmbedEnv {
  href: string;
  framed: boolean;
  parent: PostTarget | null;
  storage: StorageLike | null;
  fetchConfig(): Promise<unknown>;
  listen(handler: (ev: MessageLike) => void): () => void;
  replaceUrl?(url: string): void;
  now?(): number;
}

const OFF: EmbedSnapshot = { status: "off", parent: null, user: null, theme: null, initialised: false };

/** `value` as an http(s) origin, or null. */
export function normaliseOrigin(value: unknown): string | null {
  if (typeof value !== "string" || !value) return null;
  try {
    const u = new URL(value);
    if (u.protocol !== "https:" && u.protocol !== "http:") return null;
    return u.origin;
  } catch {
    return null;
  }
}

/** A Meet path the parent may ask us to show: starts with one "/", same origin. */
export function isSafeMeetPath(path: unknown, origin: string): path is string {
  if (typeof path !== "string" || path.length === 0 || path.length > 2048) return false;
  if (!path.startsWith("/") || path.startsWith("//") || path.startsWith("/\\")) return false;
  if (/[\u0000-\u001f\u007f]/.test(path)) return false;
  try {
    return new URL(path, origin).origin === origin;
  } catch {
    return false;
  }
}

/** Mail paths Meet may ask Mail to open (contract §2.2). */
export function isMailPath(path: unknown): path is string {
  return typeof path === "string" && /^\/(meetings\/|calendar(?:[/?#]|$))/.test(path) && !/[\u0000-\u001f\u007f]/.test(path);
}

/** The Mail path of an `open_url` (`https://mail…/meetings/m/<id>` → `/meetings/m/<id>`). */
export function mailPathOf(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    const path = `${u.pathname}${u.search}${u.hash}`;
    return isMailPath(path) ? path : null;
  } catch {
    return null;
  }
}

function isObj(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

function parseTheme(v: unknown): EmbedTheme | null {
  if (!isObj(v)) return null;
  const mode = v.mode === "dark" ? "dark" : v.mode === "light" ? "light" : null;
  if (!mode) return null;
  const tokens: Record<string, string> = {};
  if (isObj(v.tokens)) {
    for (const [k, val] of Object.entries(v.tokens)) {
      if (typeof val === "string" && /^#[0-9a-f]{3}([0-9a-f]{3})?$/i.test(val.trim())) tokens[k] = val.trim();
    }
  }
  return { scheme: "mail", mode, tokens };
}

function parseUser(v: unknown): EmbedUser | null {
  if (!isObj(v)) return null;
  const name = typeof v.name === "string" ? v.name : "";
  const email = typeof v.email === "string" ? v.email : "";
  if (!name && !email) return null;
  return { name, email };
}

function validToken(token: unknown, expiresAt: unknown): token is string {
  return typeof token === "string" && token.length > 0 && typeof expiresAt === "number" && Number.isFinite(expiresAt);
}

interface InFlight {
  id: string;
  promise: Promise<string | null>;
  finish: (token: string | null) => void;
}

export class EmbedController {
  private snap: EmbedSnapshot = OFF;
  private readonly listeners = new Set<() => void>();
  private readonly navListeners = new Set<(path: string) => void>();
  private pendingNav: string | null = null;
  private token: { token: string; expiresAt: number } | null = null;
  private tokenWaiters: Array<(token: string | null) => void> = [];
  private inflight: InFlight | null = null;
  private renewTimer: ReturnType<typeof setTimeout> | null = null;
  private readyTimers: Array<ReturnType<typeof setTimeout>> = [];
  private lastLocation: string | null = null;
  private reuseStoredToken = false;
  private seq = 0;
  private resolveStatus: (s: EmbedStatus) => void = () => undefined;
  private readonly resolved: Promise<EmbedStatus>;

  constructor(private readonly env: EmbedEnv) {
    this.resolved = new Promise((r) => {
      this.resolveStatus = r;
    });
  }

  private now(): number {
    return this.env.now ? this.env.now() : Date.now();
  }

  private set(patch: Partial<EmbedSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    for (const l of [...this.listeners]) l();
  }

  // ── lifecycle ─────────────────────────────────────────────────────────

  /**
   * Synchronous first look (before React renders): "pending" when this page
   * may be embedded, else "off". Removes `embed`/`parent` from the URL so
   * Meet's routes and `meet:location` see clean paths.
   */
  detect(): EmbedStatus {
    const { env } = this;
    let url: URL;
    try {
      url = new URL(env.href);
    } catch {
      return this.finishDetect(null);
    }
    const flagged = url.searchParams.get("embed") === "ovm";
    const fromUrl = flagged ? normaliseOrigin(url.searchParams.get("parent")) : null;
    if (flagged) {
      url.searchParams.delete("embed");
      url.searchParams.delete("parent");
      env.replaceUrl?.(`${url.pathname}${url.search}${url.hash}`);
    }
    // A stored token is reused only on reloads inside the iframe; when Mail
    // (re)creates the iframe, its ovm:init brings the token of whoever is
    // signed in to Mail now.
    this.reuseStoredToken = !flagged;
    let stored: string | null = null;
    try {
      stored = normaliseOrigin(env.storage?.getItem(EMBED_PARENT_KEY));
    } catch {
      stored = null;
    }
    const candidate = fromUrl ?? stored;
    if (!env.framed || !env.parent || !candidate) {
      // A copy of an embedded tab's sessionStorage (window.open) is not embedded.
      if (!env.framed) this.forget();
      return this.finishDetect(null);
    }
    return this.finishDetect(candidate);
  }

  private finishDetect(candidate: string | null): EmbedStatus {
    if (!candidate) {
      this.snap = OFF;
      this.resolveStatus("off");
      return "off";
    }
    this.snap = { ...OFF, status: "pending", parent: candidate };
    return "pending";
  }

  /** Check the candidate parent against the allowlist; resolves the final status. */
  async start(): Promise<EmbedStatus> {
    if (this.snap.status !== "pending") return this.snap.status;
    const parent = this.snap.parent!;
    let allowed: string[] = [];
    try {
      const cfg = await Promise.race([
        this.env.fetchConfig(),
        new Promise<null>((r) => setTimeout(() => r(null), CONFIG_TIMEOUT_MS)),
      ]);
      const list = isObj(cfg) && Array.isArray(cfg.parent_origins) ? cfg.parent_origins : [];
      allowed = list.map(normaliseOrigin).filter((o): o is string => !!o);
    } catch {
      allowed = [];
    }
    if (this.snap.status !== "pending") return this.snap.status;
    if (!allowed.includes(parent)) {
      this.forget();
      this.set(OFF);
      this.resolveStatus("off");
      return "off";
    }
    try {
      this.env.storage?.setItem(EMBED_PARENT_KEY, parent);
    } catch {
      /* storage blocked — embed mode still works for this page load */
    }
    if (this.reuseStoredToken) this.restoreToken();
    else this.clearToken();
    this.env.listen((ev) => this.onMessage(ev));
    this.set({ status: "on" });
    this.resolveStatus("on");
    this.post({ type: "meet:ready" });
    let total = 0;
    for (const d of READY_RETRY_MS) {
      total += d;
      this.readyTimers.push(
        setTimeout(() => {
          if (!this.snap.initialised) this.post({ type: "meet:ready" });
        }, total),
      );
    }
    return "on";
  }

  private forget(): void {
    try {
      this.env.storage?.removeItem(EMBED_PARENT_KEY);
      this.env.storage?.removeItem(EMBED_TOKEN_KEY);
    } catch {
      /* ignore */
    }
  }

  // ── state ─────────────────────────────────────────────────────────────

  get status(): EmbedStatus {
    return this.snap.status;
  }

  getSnapshot = (): EmbedSnapshot => this.snap;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Resolves once the status is no longer "pending". */
  whenResolved(): Promise<EmbedStatus> {
    return this.snap.status === "pending" ? this.resolved : Promise.resolve(this.snap.status);
  }

  // ── messages from the parent ──────────────────────────────────────────

  private onMessage(ev: MessageLike): void {
    if (this.snap.status !== "on") return;
    if (ev.source !== this.env.parent || ev.origin !== this.snap.parent) return;
    const d = ev.data;
    if (!isObj(d) || d.ovm !== 1 || typeof d.type !== "string") return;
    switch (d.type) {
      case "ovm:init": {
        if (validToken(d.token, d.expiresAt)) this.setToken(d.token, d.expiresAt as number);
        const theme = parseTheme(d.theme);
        this.set({ initialised: true, user: parseUser(d.user) ?? this.snap.user, theme: theme ?? this.snap.theme });
        for (const t of this.readyTimers) clearTimeout(t);
        this.readyTimers = [];
        break;
      }
      case "ovm:token": {
        const f = this.inflight;
        if (!f || d.requestId !== f.id) return;
        if (validToken(d.token, d.expiresAt)) {
          this.setToken(d.token, d.expiresAt as number);
          f.finish(d.token);
        } else {
          f.finish(null);
        }
        break;
      }
      case "ovm:theme": {
        const theme = parseTheme(d.theme);
        if (theme) this.set({ theme });
        break;
      }
      case "ovm:navigate": {
        if (!isSafeMeetPath(d.path, new URL(this.env.href).origin)) return;
        if (this.navListeners.size === 0) this.pendingNav = d.path;
        for (const l of [...this.navListeners]) l(d.path);
        break;
      }
      default:
        break;
    }
  }

  /** Register the router's navigate; a navigation that arrived earlier is delivered now. */
  onNavigate(listener: (path: string) => void): () => void {
    this.navListeners.add(listener);
    if (this.pendingNav) {
      const p = this.pendingNav;
      this.pendingNav = null;
      listener(p);
    }
    return () => this.navListeners.delete(listener);
  }

  // ── tokens ────────────────────────────────────────────────────────────

  /** The current token when it is still valid, else null. */
  getToken(): string | null {
    if (this.snap.status !== "on" || !this.token) return null;
    return this.token.expiresAt - EXPIRY_SKEW_MS > this.now() ? this.token.token : null;
  }

  setToken(token: string, expiresAt: number): void {
    this.token = { token, expiresAt };
    try {
      this.env.storage?.setItem(EMBED_TOKEN_KEY, JSON.stringify(this.token));
    } catch {
      /* ignore */
    }
    this.scheduleRenewal();
    const waiters = this.tokenWaiters;
    this.tokenWaiters = [];
    for (const w of waiters) w(token);
  }

  clearToken(): void {
    this.token = null;
    try {
      this.env.storage?.removeItem(EMBED_TOKEN_KEY);
    } catch {
      /* ignore */
    }
  }

  private restoreToken(): void {
    try {
      const raw = this.env.storage?.getItem(EMBED_TOKEN_KEY);
      if (!raw) return;
      const j = JSON.parse(raw) as { token?: unknown; expiresAt?: unknown };
      if (validToken(j.token, j.expiresAt) && (j.expiresAt as number) - EXPIRY_SKEW_MS > this.now()) {
        this.token = { token: j.token, expiresAt: j.expiresAt as number };
        this.scheduleRenewal();
      }
    } catch {
      /* ignore */
    }
  }

  private scheduleRenewal(delay?: number): void {
    if (this.renewTimer) clearTimeout(this.renewTimer);
    this.renewTimer = null;
    if (this.snap.status === "off") return;
    const wait =
      delay ?? Math.max(MIN_RENEW_DELAY_MS, (this.token?.expiresAt ?? this.now()) - RENEW_BEFORE_MS - this.now());
    this.renewTimer = setTimeout(() => {
      this.renewTimer = null;
      void this.requestToken().then((t) => {
        if (!t) this.scheduleRenewal(RENEW_RETRY_MS);
      });
    }, wait);
  }

  /**
   * Ask the parent for a fresh token (`meet:token-request`). Concurrent
   * callers share one request; resolves null on an error answer, on timeout
   * or when not embedded.
   */
  requestToken(): Promise<string | null> {
    if (this.snap.status !== "on") return Promise.resolve(null);
    if (this.inflight) return this.inflight.promise;
    const id = `t${this.now().toString(36)}-${++this.seq}`;
    let resolve: (t: string | null) => void = () => undefined;
    const promise = new Promise<string | null>((r) => {
      resolve = r;
    });
    const timer = setTimeout(() => finish(null), TOKEN_REQUEST_TIMEOUT_MS);
    const finish = (t: string | null) => {
      clearTimeout(timer);
      if (this.inflight?.id === id) this.inflight = null;
      resolve(t);
    };
    this.inflight = { id, promise, finish };
    this.post({ type: "meet:token-request", requestId: id });
    return promise;
  }

  /**
   * A valid token: the current one, else the first one that arrives (from
   * `ovm:init` or a request) within `timeoutMs`; null when not embedded.
   */
  async waitForToken(timeoutMs = TOKEN_REQUEST_TIMEOUT_MS): Promise<string | null> {
    const status = await this.whenResolved();
    if (status !== "on") return null;
    const now = this.getToken();
    if (now) return now;
    return new Promise((resolve) => {
      let done = false;
      const timer = setTimeout(() => {
        if (done) return;
        done = true;
        this.tokenWaiters = this.tokenWaiters.filter((w) => w !== waiter);
        resolve(null);
      }, timeoutMs);
      const waiter = (t: string | null) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(t);
      };
      this.tokenWaiters.push(waiter);
    });
  }

  // ── messages to the parent ────────────────────────────────────────────

  private post(msg: Record<string, unknown>): boolean {
    if (this.snap.status !== "on" || !this.snap.parent || !this.env.parent) return false;
    try {
      this.env.parent.postMessage({ ovm: 1, ...msg }, this.snap.parent);
      return true;
    } catch {
      return false;
    }
  }

  /** `meet:location` — Meet navigated (consecutive duplicates are skipped). */
  postLocation(path: string, title?: string): void {
    const key = `${path}\u0000${title ?? ""}`;
    if (key === this.lastLocation) return;
    if (this.post({ type: "meet:location", path, ...(title ? { title } : {}) })) this.lastLocation = key;
  }

  /** `meet:call` — joined or left a room. */
  postCall(inCall: boolean, room?: string | null, title?: string | null): void {
    this.post({ type: "meet:call", inCall, ...(room ? { room } : {}), ...(title ? { title } : {}) });
  }

  /** `meet:open-mail` — only `/meetings/…` and `/calendar…` paths. */
  postOpenMail(path: string): boolean {
    if (!isMailPath(path)) return false;
    return this.post({ type: "meet:open-mail", path });
  }
}

// ── the app's controller ──────────────────────────────────────────────────

function browserEnv(): EmbedEnv {
  let framed = false;
  try {
    framed = window.parent !== window;
  } catch {
    framed = true;
  }
  let storage: StorageLike | null = null;
  try {
    storage = window.sessionStorage;
  } catch {
    storage = null;
  }
  return {
    href: window.location.href,
    framed,
    parent: framed ? window.parent : null,
    storage,
    fetchConfig: () =>
      fetch("/api/v1/embed/config", { credentials: "omit" }).then((r) => (r.ok ? r.json() : null)),
    listen: (handler) => {
      const fn = (ev: MessageEvent) => handler({ source: ev.source, origin: ev.origin, data: ev.data });
      window.addEventListener("message", fn);
      return () => window.removeEventListener("message", fn);
    },
    replaceUrl: (url) => window.history.replaceState(window.history.state, "", url),
  };
}

let controller: EmbedController | null = null;

/** The app-wide controller (inert outside a browser). */
export function embed(): EmbedController {
  if (!controller) {
    if (typeof window === "undefined") {
      controller = new EmbedController({
        href: "http://localhost/",
        framed: false,
        parent: null,
        storage: null,
        fetchConfig: async () => null,
        listen: () => () => undefined,
      });
      controller.detect();
    } else {
      controller = new EmbedController(browserEnv());
    }
  }
  return controller;
}

/** True while embedded or still checking — One Voice SSO must stay off. */
export function embedActive(): boolean {
  return embed().status !== "off";
}

/** React: the embed snapshot (status, parent, user, theme). */
export function useEmbed(): EmbedSnapshot {
  const c = embed();
  return useSyncExternalStore(c.subscribe, c.getSnapshot, c.getSnapshot);
}

/** The user Mail signed in (from `ovm:init`). */
export function embedUser(): EmbedUser | null {
  return embed().getSnapshot().user;
}


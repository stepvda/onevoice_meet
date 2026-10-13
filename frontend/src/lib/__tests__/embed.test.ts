import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  EMBED_PARENT_KEY,
  EMBED_TOKEN_KEY,
  EmbedController,
  READY_RETRY_MS,
  RENEW_BEFORE_MS,
  RENEW_RETRY_MS,
  TOKEN_REQUEST_TIMEOUT_MS,
  isMailPath,
  isSafeMeetPath,
  mailPathOf,
  normaliseOrigin,
  type EmbedEnv,
  type MessageLike,
} from "../embed";
import { MAIL_DARK_TOKENS, MAIL_LIGHT_TOKENS, applyEmbedTheme, mailSchemeVars, type SchemeRoot } from "../scheme";

const MAIL = "https://mail.witysk.org";
const MEET = "https://meet.witysk.org";

interface Posted {
  message: Record<string, unknown>;
  targetOrigin: string;
}

function memoryStorage(init: Record<string, string> = {}) {
  const m = new Map(Object.entries(init));
  return {
    map: m,
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
  };
}

function setup(opts: { href?: string; framed?: boolean; allow?: string[] | null; storage?: Record<string, string>; configFails?: boolean } = {}) {
  const posted: Posted[] = [];
  const parent = { postMessage: (message: unknown, targetOrigin: string) => posted.push({ message: message as Record<string, unknown>, targetOrigin }) };
  const storage = memoryStorage(opts.storage);
  let handler: ((ev: MessageLike) => void) | null = null;
  const replaced: string[] = [];
  const env: EmbedEnv = {
    href: opts.href ?? `${MEET}/?embed=ovm&parent=${encodeURIComponent(MAIL)}`,
    framed: opts.framed ?? true,
    parent,
    storage,
    fetchConfig: opts.configFails
      ? () => Promise.reject(new Error("down"))
      : async () => (opts.allow === null ? null : { parent_origins: opts.allow ?? [MAIL] }),
    listen: (h) => {
      handler = h;
      return () => {
        handler = null;
      };
    },
    replaceUrl: (u) => void replaced.push(u),
    now: () => Date.now(),
  };
  const c = new EmbedController(env);
  /** Deliver a message as if it came from `source` at `origin`. */
  const deliver = (data: unknown, origin = MAIL, source: unknown = parent) => handler?.({ source, origin, data });
  const sent = (type: string) => posted.filter((p) => p.message.type === type);
  return { c, env, parent, posted, storage, replaced, deliver, sent, hasListener: () => !!handler };
}

async function started(opts: Parameters<typeof setup>[0] = {}) {
  const s = setup(opts);
  expect(s.c.detect()).toBe("pending");
  await s.c.start();
  return s;
}

const init = (extra: Record<string, unknown> = {}) => ({
  ovm: 1,
  type: "ovm:init",
  token: "tok-1",
  expiresAt: Date.now() + 15 * 60_000,
  user: { name: "Ann Example", email: "ann@example.org" },
  theme: { scheme: "mail", mode: "light", tokens: { accent: "#1c5cab" } },
  ...extra,
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-13T10:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("embed mode detection and the parent allowlist", () => {
  it("is on for a framed page whose parent is allowlisted; strips the flags and remembers the parent", async () => {
    const s = setup({ href: `${MEET}/brave-blue-fox?pw=1&embed=ovm&parent=${encodeURIComponent(MAIL)}` });
    expect(s.c.detect()).toBe("pending");
    expect(s.replaced).toEqual(["/brave-blue-fox?pw=1"]);
    expect(s.sent("meet:ready")).toHaveLength(0); // nothing is posted before the allowlist check
    expect(await s.c.start()).toBe("on");
    expect(s.c.getSnapshot()).toMatchObject({ status: "on", parent: MAIL });
    expect(s.storage.getItem(EMBED_PARENT_KEY)).toBe(MAIL);
    expect(s.sent("meet:ready")).toEqual([{ message: { ovm: 1, type: "meet:ready" }, targetOrigin: MAIL }]);
  });

  it("is off when the parent is not in the allowlist, and forgets a remembered parent", async () => {
    const s = setup({ allow: ["https://other.example"], storage: { [EMBED_PARENT_KEY]: MAIL } });
    expect(s.c.detect()).toBe("pending");
    expect(await s.c.start()).toBe("off");
    expect(s.storage.getItem(EMBED_PARENT_KEY)).toBeNull();
    expect(s.posted).toHaveLength(0);
    expect(s.hasListener()).toBe(false);
    expect(await s.c.whenResolved()).toBe("off");
  });

  it("is off when the allowlist cannot be read", async () => {
    const s = setup({ configFails: true });
    s.c.detect();
    expect(await s.c.start()).toBe("off");
    const t = setup({ allow: null });
    t.c.detect();
    expect(await t.c.start()).toBe("off");
  });

  it("is off when not framed (and drops a copied parent flag)", () => {
    const s = setup({ framed: false, storage: { [EMBED_PARENT_KEY]: MAIL } });
    expect(s.c.detect()).toBe("off");
    expect(s.storage.getItem(EMBED_PARENT_KEY)).toBeNull();
  });

  it("is off without both flags", () => {
    expect(setup({ href: `${MEET}/?embed=ovm` }).c.detect()).toBe("off");
    expect(setup({ href: `${MEET}/?parent=${encodeURIComponent(MAIL)}` }).c.detect()).toBe("off");
    expect(setup({ href: `${MEET}/?embed=other&parent=${encodeURIComponent(MAIL)}` }).c.detect()).toBe("off");
  });

  it("stays embedded after navigation inside the iframe (remembered parent, no URL flags)", async () => {
    const s = await started({ href: `${MEET}/r/brave-blue-fox`, storage: { [EMBED_PARENT_KEY]: MAIL } });
    expect(s.c.status).toBe("on");
    expect(s.replaced).toEqual([]);
  });

  it("re-posts meet:ready until ovm:init arrives", async () => {
    const s = await started();
    vi.advanceTimersByTime(READY_RETRY_MS[0]);
    expect(s.sent("meet:ready")).toHaveLength(2);
    s.deliver(init());
    vi.advanceTimersByTime(60_000);
    expect(s.sent("meet:ready")).toHaveLength(2);
  });

  it("normalises origins", () => {
    expect(normaliseOrigin("https://mail.witysk.org/")).toBe(MAIL);
    expect(normaliseOrigin("javascript:alert(1)")).toBeNull();
    expect(normaliseOrigin("not a url")).toBeNull();
    expect(normaliseOrigin(42)).toBeNull();
  });
});

describe("message validation", () => {
  it("accepts ovm:init only from window.parent at the validated origin with ovm: 1", async () => {
    const s = await started();
    s.deliver(init({ token: "evil-origin" }), "https://evil.example");
    s.deliver(init({ token: "evil-source" }), MAIL, { postMessage() {} });
    s.deliver({ ...init({ token: "no-version" }), ovm: undefined });
    s.deliver({ ...init({ token: "wrong-version" }), ovm: 2 });
    s.deliver("ovm:init");
    expect(s.c.getToken()).toBeNull();
    expect(s.c.getSnapshot().initialised).toBe(false);
    s.deliver(init());
    expect(s.c.getToken()).toBe("tok-1");
    expect(s.c.getSnapshot()).toMatchObject({ initialised: true, user: { name: "Ann Example", email: "ann@example.org" } });
  });

  it("ignores everything before the allowlist check succeeded", async () => {
    const s = setup();
    s.c.detect();
    s.deliver(init()); // no listener yet
    expect(s.c.getToken()).toBeNull();
  });

  it("posts only with the exact parent origin, never *", async () => {
    const s = await started();
    s.deliver(init());
    s.c.postLocation("/settings");
    s.c.postCall(true, "brave-blue-fox", "Board meeting");
    s.c.postOpenMail("/meetings/m/123");
    void s.c.requestToken();
    expect(s.posted.length).toBeGreaterThan(3);
    expect(s.posted.every((p) => p.targetOrigin === MAIL && p.message.ovm === 1)).toBe(true);
  });

  it("navigates only to same-origin paths starting with /", async () => {
    const s = await started();
    const seen: string[] = [];
    s.c.onNavigate((p) => seen.push(p));
    for (const path of ["//evil.example/x", "https://evil.example/", "/\\evil.example", "javascript:alert(1)", "relative", "/ok\nx", 5]) {
      s.deliver({ ovm: 1, type: "ovm:navigate", path });
    }
    s.deliver({ ovm: 1, type: "ovm:navigate", path: "/brave-blue-fox?x=1" });
    expect(seen).toEqual(["/brave-blue-fox?x=1"]);
  });

  it("delivers a navigation that arrived before the router listened", async () => {
    const s = await started();
    s.deliver({ ovm: 1, type: "ovm:navigate", path: "/recordings" });
    const seen: string[] = [];
    s.c.onNavigate((p) => seen.push(p));
    expect(seen).toEqual(["/recordings"]);
  });

  it("checks paths", () => {
    expect(isSafeMeetPath("/a", MEET)).toBe(true);
    expect(isSafeMeetPath("//a", MEET)).toBe(false);
    expect(isMailPath("/meetings/m/1")).toBe(true);
    expect(isMailPath("/calendar")).toBe(true);
    expect(isMailPath("/calendar/week")).toBe(true);
    expect(isMailPath("/calendarx")).toBe(false);
    expect(isMailPath("/mail/inbox")).toBe(false);
    expect(mailPathOf(`${MAIL}/meetings/m/abc?x=1#y`)).toBe("/meetings/m/abc?x=1#y");
    expect(mailPathOf(`${MAIL}/settings`)).toBeNull();
    expect(mailPathOf("nonsense")).toBeNull();
  });

  it("meet:open-mail refuses other Mail paths", async () => {
    const s = await started();
    expect(s.c.postOpenMail("/mail/inbox")).toBe(false);
    expect(s.c.postOpenMail("/meetings/m/1")).toBe(true);
    expect(s.sent("meet:open-mail")).toEqual([{ message: { ovm: 1, type: "meet:open-mail", path: "/meetings/m/1" }, targetOrigin: MAIL }]);
  });

  it("meet:location skips consecutive duplicates", async () => {
    const s = await started();
    s.c.postLocation("/");
    s.c.postLocation("/");
    s.c.postLocation("/recordings");
    expect(s.sent("meet:location").map((p) => p.message.path)).toEqual(["/", "/recordings"]);
  });
});

describe("tokens", () => {
  it("asks for a new token 2 minutes before expiry and keeps renewing", async () => {
    const s = await started();
    const exp = Date.now() + 15 * 60_000;
    s.deliver(init({ expiresAt: exp }));
    vi.advanceTimersByTime(15 * 60_000 - RENEW_BEFORE_MS - 1);
    expect(s.sent("meet:token-request")).toHaveLength(0);
    vi.advanceTimersByTime(1);
    const [req] = s.sent("meet:token-request");
    expect(req.message.requestId).toEqual(expect.any(String));
    s.deliver({ ovm: 1, type: "ovm:token", requestId: req.message.requestId, token: "tok-2", expiresAt: Date.now() + 15 * 60_000 });
    await vi.advanceTimersByTimeAsync(0);
    expect(s.c.getToken()).toBe("tok-2");
    expect(JSON.parse(s.storage.getItem(EMBED_TOKEN_KEY)!).token).toBe("tok-2");
    await vi.advanceTimersByTimeAsync(15 * 60_000 - RENEW_BEFORE_MS);
    expect(s.sent("meet:token-request")).toHaveLength(2);
  });

  it("shares one in-flight request between callers (401s from several calls)", async () => {
    const s = await started();
    s.deliver(init());
    const a = s.c.requestToken();
    const b = s.c.requestToken();
    expect(a).toBe(b);
    expect(s.sent("meet:token-request")).toHaveLength(1);
    const id = s.sent("meet:token-request")[0].message.requestId;
    s.deliver({ ovm: 1, type: "ovm:token", requestId: "someone-else", token: "nope", expiresAt: Date.now() + 60_000 });
    s.deliver({ ovm: 1, type: "ovm:token", requestId: id, token: "tok-3", expiresAt: Date.now() + 15 * 60_000 });
    expect(await a).toBe("tok-3");
    // The next request is a new one.
    void s.c.requestToken();
    expect(s.sent("meet:token-request")).toHaveLength(2);
  });

  it("resolves null on an error answer and on timeout", async () => {
    const s = await started();
    const a = s.c.requestToken();
    const id = s.sent("meet:token-request")[0].message.requestId;
    s.deliver({ ovm: 1, type: "ovm:token", requestId: id, error: "not_signed_in" });
    expect(await a).toBeNull();
    const b = s.c.requestToken();
    vi.advanceTimersByTime(TOKEN_REQUEST_TIMEOUT_MS);
    expect(await b).toBeNull();
  });

  it("retries a failed renewal", async () => {
    const s = await started();
    s.deliver(init({ expiresAt: Date.now() + 15 * 60_000 }));
    await vi.advanceTimersByTimeAsync(15 * 60_000 - RENEW_BEFORE_MS);
    expect(s.sent("meet:token-request")).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(TOKEN_REQUEST_TIMEOUT_MS);
    await vi.advanceTimersByTimeAsync(RENEW_RETRY_MS);
    expect(s.sent("meet:token-request")).toHaveLength(2);
  });

  it("treats a token as gone shortly before it expires", async () => {
    const s = await started();
    s.deliver(init({ expiresAt: Date.now() + 60_000 }));
    expect(s.c.getToken()).toBe("tok-1");
    vi.setSystemTime(Date.now() + 55_000);
    expect(s.c.getToken()).toBeNull();
  });

  it("waitForToken resolves with the init token, or null when not embedded", async () => {
    const s = await started();
    const w = s.c.waitForToken();
    s.deliver(init());
    expect(await w).toBe("tok-1");
    const off = setup({ framed: false });
    off.c.detect();
    expect(await off.c.waitForToken()).toBeNull();
    expect(await off.c.requestToken()).toBeNull();
  });

  it("restores a stored token after a reload inside the iframe, not when Mail creates the iframe", async () => {
    const stored = JSON.stringify({ token: "tok-stored", expiresAt: Date.now() + 10 * 60_000 });
    const s = await started({ href: `${MEET}/recordings`, storage: { [EMBED_PARENT_KEY]: MAIL, [EMBED_TOKEN_KEY]: stored } });
    expect(s.c.getToken()).toBe("tok-stored");
    const fresh = await started({ storage: { [EMBED_PARENT_KEY]: MAIL, [EMBED_TOKEN_KEY]: stored } });
    expect(fresh.c.getToken()).toBeNull();
    expect(fresh.storage.getItem(EMBED_TOKEN_KEY)).toBeNull();
  });
});

describe("theme", () => {
  function fakeRoot(): SchemeRoot & { vars: Map<string, string>; classes: Set<string> } {
    const vars = new Map<string, string>();
    const classes = new Set<string>();
    return {
      vars,
      classes,
      dataset: {},
      classList: { toggle: (n: string, f?: boolean) => (f ? classes.add(n) : classes.delete(n)) },
      style: { setProperty: (k: string, v: string) => void vars.set(k, v), removeProperty: (k: string) => vars.delete(k) },
    };
  }

  it("ovm:init and ovm:theme update the theme live", async () => {
    const s = await started();
    s.deliver(init());
    expect(s.c.getSnapshot().theme).toEqual({ scheme: "mail", mode: "light", tokens: { accent: "#1c5cab" } });
    s.deliver({ ovm: 1, type: "ovm:theme", theme: { scheme: "mail", mode: "dark", tokens: { accent: "#3987e5", page: "#0d0d0d", bogus: "red" } } });
    expect(s.c.getSnapshot().theme).toEqual({ scheme: "mail", mode: "dark", tokens: { accent: "#3987e5", page: "#0d0d0d" } });
    s.deliver({ ovm: 1, type: "ovm:theme", theme: { mode: "sepia" } });
    expect(s.c.getSnapshot().theme?.mode).toBe("dark");
  });

  it("applies the parent's mode and tokens as inline variables on <html>", () => {
    const root = fakeRoot();
    applyEmbedTheme(root, { mode: "dark", tokens: { accent: "#ff0000", page: "#000000" } }, "light");
    expect(root.dataset).toMatchObject({ scheme: "mail", theme: "dark" });
    expect(root.classes.has("dark")).toBe(true);
    expect(root.vars.get("--m-accent-500")).toBe("255 0 0");
    expect(root.vars.get("--m-primary-500")).toBe("255 0 0");
    expect(root.vars.get("--m-primary-900")).toBe("0 0 0");
    // Missing tokens fall back to Mail's built-in dark values.
    expect(root.vars.get("--m-primary-800")).toBe("26 26 25");
    // Without a theme: Mail's built-in tokens for the fallback mode.
    const r2 = fakeRoot();
    applyEmbedTheme(r2, null, "light");
    expect(Object.fromEntries(r2.vars)).toEqual(mailSchemeVars(MAIL_LIGHT_TOKENS, "light"));
    expect(r2.dataset.theme).toBe("light");
  });

  it("ignores malformed token values", () => {
    const root = fakeRoot();
    applyEmbedTheme(root, { mode: "dark", tokens: { accent: "url(evil)", ink: "#12" } }, "light");
    expect(root.vars.get("--m-accent-500")).toBe(mailSchemeVars(MAIL_DARK_TOKENS, "dark")["--m-accent-500"]);
    expect(root.vars.get("--m-fg")).toBe("255 255 255");
  });
});

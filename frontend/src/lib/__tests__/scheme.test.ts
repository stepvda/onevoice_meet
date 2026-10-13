import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  MAIL_DARK_TOKENS,
  MAIL_LIGHT_TOKENS,
  MEET_VARS,
  PALETTE_VARS,
  applyScheme,
  completeTokens,
  mailSchemeVars,
  parseHex,
  type SchemeRoot,
} from "../scheme";
// @ts-expect-error -- the Tailwind config is plain JS without type declarations.
import tailwindConfig from "../../../tailwind.config.js";

const css = readFileSync(new URL("../../styles/schemes.css", import.meta.url), "utf8");
const globalCss = readFileSync(new URL("../../styles/global.css", import.meta.url), "utf8");
const mailCss = (() => {
  try {
    // Mail's own stylesheet when this checkout sits next to it (dev); optional.
    return readFileSync(new URL("../../../../../mail/apps/web/src/styles.css", import.meta.url), "utf8");
  } catch {
    return null;
  }
})();

/** The custom properties declared in the CSS block that starts with `selector {`. */
function block(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  expect(start, `block ${selector}`).toBeGreaterThanOrEqual(0);
  const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
  const out: Record<string, string> = {};
  for (const m of body.matchAll(/(--[\w-]+):\s*([^;]+);/g)) out[m[1]] = m[2].trim();
  return out;
}

const rgb = (hex: string) => parseHex(hex)!.join(" ");

describe("Meet scheme (stand-alone default)", () => {
  it("keeps today's colours: the hex values tailwind.config.js and global.css hard-coded", () => {
    const before: Record<string, string> = {
      "primary-50": "#f1f5fb", "primary-100": "#dde6f2", "primary-200": "#b6c7dd", "primary-300": "#7f9dbf",
      "primary-400": "#4f73a0", "primary-500": "#1E3A5F", "primary-600": "#1A3354", "primary-700": "#162C49",
      "primary-800": "#12253E", "primary-900": "#0E1E33", "primary-950": "#0A1626",
      "secondary-500": "#2E5A8F", "secondary-600": "#294F7E", "secondary-700": "#24446D",
      "accent-500": "#4CAF50", "accent-600": "#43A047", "accent-700": "#388E3C",
      // Tailwind's default slate.
      "slate-50": "#f8fafc", "slate-100": "#f1f5f9", "slate-200": "#e2e8f0", "slate-300": "#cbd5e1",
      "slate-400": "#94a3b8", "slate-500": "#64748b", "slate-600": "#475569", "slate-700": "#334155",
      "slate-800": "#1e293b", "slate-900": "#0f172a", "slate-950": "#020617",
      // White as body text, the primary button hover (= primary-600).
      fg: "#ffffff", "primary-strong": "#1A3354",
      // Page gradient stops.
      "page-1": "#b6c7dd", "page-2": "#1E3A5F", "page-3": "#0E1E33",
    };
    for (const [name, hex] of Object.entries(before)) expect(MEET_VARS[`--m-${name}`], name).toBe(rgb(hex));
    expect(MEET_VARS["--m-sidebar-from"]).toBe("rgba(5, 15, 70, 0.97)");
    expect(MEET_VARS["--m-sidebar-to"]).toBe("rgba(15, 40, 140, 0.95)");
    expect(MEET_VARS["--m-scroll-track"]).toBe("#0E1E33");
    expect(MEET_VARS["--m-scroll-thumb"]).toBe("#294F7E");
    expect(MEET_VARS["--m-scroll-thumb-hover"]).toBe("#2E5A8F");
    // Sidebar items: white/15 active, white/70 idle, white/10 hover.
    expect(MEET_VARS["--m-nav-active"]).toBe("255 255 255 / 0.15");
    expect(MEET_VARS["--m-nav-dim"]).toBe("255 255 255 / 0.7");
    expect(MEET_VARS["--m-nav-hover"]).toBe("255 255 255 / 0.1");
  });

  it("schemes.css declares exactly these defaults on :root", () => {
    expect(block(":root")).toEqual(MEET_VARS);
  });

  it(".m-own-palette resets every Tailwind palette variable to Meet's value", () => {
    const reset = block(".m-own-palette");
    expect(Object.keys(reset).sort()).toEqual([...PALETTE_VARS].sort());
    for (const k of PALETTE_VARS) expect(reset[k]).toBe(MEET_VARS[k]);
  });

  it("tailwind colours read the variables with <alpha-value>", () => {
    const colors = (tailwindConfig as { theme: { extend: { colors: Record<string, Record<string, string> | string> } } }).theme.extend.colors;
    const scales: Record<string, number[]> = {
      primary: [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950],
      secondary: [500, 600, 700],
      accent: [500, 600, 700],
      slate: [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950],
    };
    for (const [name, shades] of Object.entries(scales)) {
      for (const s of shades) {
        expect((colors[name] as Record<string, string>)[s]).toBe(`rgb(var(--m-${name}-${s}) / <alpha-value>)`);
        expect(MEET_VARS[`--m-${name}-${s}`]).toBeDefined();
      }
    }
    expect(colors.fg).toBe("rgb(var(--m-fg) / <alpha-value>)");
  });

  it("global.css no longer hard-codes the gradients and the scrollbar", () => {
    expect(globalCss).toContain("rgb(var(--m-page-1))");
    expect(globalCss).toContain("var(--m-sidebar-from)");
    expect(globalCss).toContain("var(--m-scroll-thumb)");
    const rules = globalCss.replace(/\/\*[\s\S]*?\*\//g, "");
    expect(rules).not.toMatch(/#0E1E33|#294F7E|#2E5A8F|#b6c7dd|#1E3A5F|rgba\(5, 15, 70/i);
  });
});

describe("Mail scheme", () => {
  it("built-in tokens are Mail's light and dark values", () => {
    expect(MAIL_LIGHT_TOKENS).toMatchObject({
      accent: "#1c5cab", accentStrong: "#144a89", page: "#f5f7fb", surface: "#ffffff", surface2: "#f1f3f6",
      selected: "#dbe8fb", line: "#e6e6e3", ink: "#1f1f1f", ink2: "#52514e", muted: "#6b6a66",
      good: "#0ca30c", warn: "#fab219", crit: "#d03b3b",
    });
    expect(MAIL_DARK_TOKENS).toMatchObject({
      accent: "#3987e5", accentStrong: "#144a89", page: "#0d0d0d", surface: "#1a1a19", surface2: "#252524",
      selected: "#1c3a5e", line: "#2c2c2a", ink: "#ffffff", ink2: "#c3c2b7", muted: "#9a9990",
    });
  });

  it.skipIf(!mailCss)("matches apps/web/src/styles.css of the Mail checkout next to this one", () => {
    const theme = mailCss!.slice(mailCss!.indexOf("@theme {"));
    const dark = mailCss!.slice(mailCss!.indexOf('[data-theme="dark"] {'));
    const value = (src: string, name: string) => new RegExp(`--color-${name}:\\s*(#[0-9a-f]+)`, "i").exec(src.slice(0, src.indexOf("}")))?.[1];
    const names: Record<string, string> = { accent: "accent", accentStrong: "accent-strong", page: "page", surface: "surface", surface2: "surface-2", selected: "selected", line: "line", ink: "ink", ink2: "ink-2", muted: "muted", good: "good", warn: "warn", crit: "crit" };
    for (const [k, css] of Object.entries(names)) {
      expect(MAIL_LIGHT_TOKENS[k as keyof typeof MAIL_LIGHT_TOKENS].toLowerCase(), k).toBe(value(theme, css)!.toLowerCase());
      const d = value(dark, css) ?? value(theme, css)!;
      expect(MAIL_DARK_TOKENS[k as keyof typeof MAIL_DARK_TOKENS].toLowerCase(), `dark ${k}`).toBe(d.toLowerCase());
    }
  });

  it("schemes.css ships the variables computed from Mail's light and dark tokens", () => {
    expect(block(':root[data-scheme="mail"][data-theme="light"]')).toEqual(mailSchemeVars(MAIL_LIGHT_TOKENS, "light"));
    expect(block(':root[data-scheme="mail"][data-theme="dark"]')).toEqual(mailSchemeVars(MAIL_DARK_TOKENS, "dark"));
  });

  it("light mode turns the navy surfaces into Mail's light surfaces and the text into Mail's ink", () => {
    const v = mailSchemeVars(MAIL_LIGHT_TOKENS, "light");
    expect(v["--m-primary-900"]).toBe(rgb("#f5f7fb")); // page level
    expect(v["--m-primary-800"]).toBe(rgb("#ffffff")); // cards, modals
    expect(v["--m-primary-700"]).toBe(rgb("#e6e6e3")); // borders, chips
    expect(v["--m-primary-500"]).toBe(rgb("#1c5cab")); // filled active controls
    expect(v["--m-accent-500"]).toBe(rgb("#1c5cab"));
    expect(v["--m-slate-100"]).toBe(rgb("#1f1f1f")); // body text
    expect(v["--m-slate-300"]).toBe(rgb("#52514e"));
    expect(v["--m-slate-400"]).toBe(rgb("#6b6a66"));
    expect(v["--m-fg"]).toBe(rgb("#1f1f1f"));
    expect(v["--m-page-1"]).toBe(rgb("#f5f7fb"));
  });

  it("covers every variable the Meet scheme defines", () => {
    expect(Object.keys(mailSchemeVars(MAIL_LIGHT_TOKENS, "light")).sort()).toEqual(Object.keys(MEET_VARS).sort());
  });

  it("completes partial parent tokens from the built-in set of the mode", () => {
    expect(completeTokens({ accent: "#abcdef" }, "dark")).toEqual({ ...MAIL_DARK_TOKENS, accent: "#abcdef" });
    expect(completeTokens({ accent: "nope", ink: 3 }, "light")).toEqual(MAIL_LIGHT_TOKENS);
    expect(completeTokens(null, "light")).toEqual(MAIL_LIGHT_TOKENS);
  });

  it("applyScheme sets data-scheme / data-theme and clears inline embed values", () => {
    const removed: string[] = [];
    const classes = new Set<string>();
    const root: SchemeRoot = {
      dataset: {},
      classList: { toggle: (n: string, f?: boolean) => (f ? classes.add(n) : classes.delete(n)) },
      style: { setProperty: () => undefined, removeProperty: (k: string) => removed.push(k) },
    };
    applyScheme(root, "mail", "light");
    expect(root.dataset).toEqual({ scheme: "mail", theme: "light" });
    expect(classes.has("light")).toBe(true);
    expect(removed).toContain("--m-primary-500");
    applyScheme(root, "meet", "dark");
    expect(root.dataset).toEqual({ scheme: "meet", theme: "dark" });
    expect(classes.has("dark") && !classes.has("light")).toBe(true);
  });
});

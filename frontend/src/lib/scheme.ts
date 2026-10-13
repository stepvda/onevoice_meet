/**
 * Colour schemes (contract meet-integration.md §2.4).
 *
 * Tailwind's `primary`, `secondary`, `accent` and `slate` scales, the page and
 * sidebar gradients and the scrollbar read CSS variables (`--m-*`, RGB
 * triplets so Tailwind's `<alpha-value>` works). The defaults are Meet's
 * historical colours, so stand-alone Meet looks exactly as before.
 *
 * `<html data-scheme="mail" data-theme="light|dark">` switches to One Voice
 * Mail's look. The app is written "light text on dark navy", so the Mail
 * light scheme maps the scales by ROLE rather than by lightness:
 *
 *   primary-900  page level (the background panels sit on)  → Mail page
 *   primary-800  raised panels, cards, modals               → Mail surface
 *   primary-700  borders, chips, toolbar buttons            → Mail line
 *   primary-600  hover of a 700 chip                        → line, a shade darker
 *   primary-500  filled "active" controls (white text)      → Mail accent
 *   slate-50…500 body text on panels                        → Mail ink … muted
 *   fg           `text-fg`: white-as-body-text              → Mail ink
 *
 * `schemes.css` ships these values for Mail's built-in light and dark tokens
 * (a unit test keeps the file and this module identical); when Meet runs
 * inside Mail the parent's tokens are applied live as inline variables on
 * <html> (applyEmbedTheme).
 *
 * Areas with a palette of their own (the Meet++ board and dialogs, light;
 * the video stage, dark) carry the class `m-own-palette`, which re-declares
 * the Meet defaults for their subtree.
 */

/** Mail's colour tokens, as sent in `ovm:init` / `ovm:theme` (hex). */
export interface MailTokens {
  accent: string;
  accentStrong: string;
  page: string;
  surface: string;
  surface2: string;
  selected: string;
  line: string;
  ink: string;
  ink2: string;
  muted: string;
  good: string;
  warn: string;
  crit: string;
}

export type SchemeMode = "light" | "dark";
export type SchemeName = "meet" | "mail";

/** Mail's built-in tokens (apps/web/src/styles.css `@theme` and `[data-theme="dark"]`). */
export const MAIL_LIGHT_TOKENS: MailTokens = {
  accent: "#1c5cab",
  accentStrong: "#144a89",
  page: "#f5f7fb",
  surface: "#ffffff",
  surface2: "#f1f3f6",
  selected: "#dbe8fb",
  line: "#e6e6e3",
  ink: "#1f1f1f",
  ink2: "#52514e",
  muted: "#6b6a66",
  good: "#0ca30c",
  warn: "#fab219",
  crit: "#d03b3b",
};

export const MAIL_DARK_TOKENS: MailTokens = {
  ...MAIL_LIGHT_TOKENS,
  accent: "#3987e5",
  page: "#0d0d0d",
  surface: "#1a1a19",
  surface2: "#252524",
  selected: "#1c3a5e",
  line: "#2c2c2a",
  ink: "#ffffff",
  ink2: "#c3c2b7",
  muted: "#9a9990",
};

export const MAIL_TOKEN_NAMES = Object.keys(MAIL_LIGHT_TOKENS) as Array<keyof MailTokens>;

/** Variables that `.m-own-palette` resets (the Tailwind colour scales). */
export const PALETTE_VARS = [
  ...[50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map((s) => `--m-primary-${s}`),
  "--m-primary-strong",
  ...[500, 600, 700].map((s) => `--m-secondary-${s}`),
  ...[500, 600, 700].map((s) => `--m-accent-${s}`),
  ...[50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map((s) => `--m-slate-${s}`),
  "--m-fg",
] as const;

/**
 * Meet's own colours (the stand-alone default). Values are what
 * tailwind.config.js and global.css hard-coded before the variables existed.
 */
export const MEET_VARS: Record<string, string> = {
  "--m-primary-50": "241 245 251", // #f1f5fb
  "--m-primary-100": "221 230 242", // #dde6f2
  "--m-primary-200": "182 199 221", // #b6c7dd
  "--m-primary-300": "127 157 191", // #7f9dbf
  "--m-primary-400": "79 115 160", // #4f73a0
  "--m-primary-500": "30 58 95", // #1E3A5F
  "--m-primary-600": "26 51 84", // #1A3354
  "--m-primary-700": "22 44 73", // #162C49
  "--m-primary-800": "18 37 62", // #12253E
  "--m-primary-900": "14 30 51", // #0E1E33
  "--m-primary-950": "10 22 38", // #0A1626
  "--m-primary-strong": "26 51 84", // = primary-600: hover of a filled primary button
  "--m-secondary-500": "46 90 143", // #2E5A8F
  "--m-secondary-600": "41 79 126", // #294F7E
  "--m-secondary-700": "36 68 109", // #24446D
  "--m-accent-500": "76 175 80", // #4CAF50
  "--m-accent-600": "67 160 71", // #43A047
  "--m-accent-700": "56 142 60", // #388E3C
  // Tailwind's default slate.
  "--m-slate-50": "248 250 252",
  "--m-slate-100": "241 245 249",
  "--m-slate-200": "226 232 240",
  "--m-slate-300": "203 213 225",
  "--m-slate-400": "148 163 184",
  "--m-slate-500": "100 116 139",
  "--m-slate-600": "71 85 105",
  "--m-slate-700": "51 65 85",
  "--m-slate-800": "30 41 59",
  "--m-slate-900": "15 23 42",
  "--m-slate-950": "2 6 23",
  // `text-fg` / `bg-fg/10`: white used as foreground on the navy surfaces.
  "--m-fg": "255 255 255",
  // Page-level variables (not reset by .m-own-palette).
  "--m-page-1": "182 199 221", // radial gradient centre (#b6c7dd)
  "--m-page-2": "30 58 95", // mid (#1E3A5F)
  "--m-page-3": "14 30 51", // edge (#0E1E33)
  "--m-sidebar-from": "rgba(5, 15, 70, 0.97)",
  "--m-sidebar-to": "rgba(15, 40, 140, 0.95)",
  "--m-nav-fg": "255 255 255",
  "--m-nav-dim": "255 255 255 / 0.7",
  "--m-nav-active": "255 255 255 / 0.15",
  "--m-nav-hover": "255 255 255 / 0.1",
  "--m-scroll-track": "#0E1E33",
  "--m-scroll-thumb": "#294F7E",
  "--m-scroll-thumb-hover": "#2E5A8F",
  "--m-line": "22 44 73", // = primary-700 (sidebar edge, control bar border)
  "--m-stage-bg": "transparent",
  "--m-review-page": "241 245 249", // Meet++ review page background (slate-100)
  "--m-review-header": "15 23 42", // Meet++ review header (slate-900)
  "--m-review-header-fg": "255 255 255",
};

// ── colour helpers ──────────────────────────────────────────────────────

/** "#1c5cab" / "#fff" → [28, 92, 171]; null when not a hex colour. */
export function parseHex(hex: unknown): [number, number, number] | null {
  if (typeof hex !== "string") return null;
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  let h = m[1];
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

const triplet = (c: [number, number, number]) => c.map((n) => Math.round(n)).join(" ");

function rgbOf(hex: string): [number, number, number] {
  return parseHex(hex) ?? [0, 0, 0];
}

/** `a` blended towards `b` by `t` (0 = a, 1 = b), as an RGB triplet. */
function mix(a: string, b: string, t: number): [number, number, number] {
  const x = rgbOf(a);
  const y = rgbOf(b);
  return [0, 1, 2].map((i) => x[i] + (y[i] - x[i]) * t) as [number, number, number];
}

/** Fill missing or malformed tokens from Mail's built-in set for `mode`. */
export function completeTokens(tokens: Partial<Record<keyof MailTokens, unknown>> | null | undefined, mode: SchemeMode): MailTokens {
  const base = mode === "dark" ? MAIL_DARK_TOKENS : MAIL_LIGHT_TOKENS;
  const out = { ...base };
  for (const k of MAIL_TOKEN_NAMES) {
    const v = tokens?.[k];
    if (parseHex(v)) out[k] = (v as string).trim();
  }
  return out;
}

/**
 * The `--m-*` variables for the Mail scheme with the given tokens. The same
 * function produces the built-in blocks in schemes.css (see scheme.test.ts)
 * and the inline values applied from the parent's `ovm:theme`.
 */
export function mailSchemeVars(tokens: MailTokens, mode: SchemeMode): Record<string, string> {
  const T = tokens;
  const t = (hex: string) => triplet(rgbOf(hex));
  const m = (a: string, b: string, k: number) => triplet(mix(a, b, k));
  const dark = mode === "dark";
  return {
    "--m-primary-50": t(dark ? T.ink : T.page),
    "--m-primary-100": t(T.ink),
    "--m-primary-200": t(T.accent),
    "--m-primary-300": t(T.accent),
    "--m-primary-400": t(T.accent),
    "--m-primary-500": t(T.accent),
    "--m-primary-600": m(T.line, T.ink, 0.08),
    "--m-primary-700": t(T.line),
    "--m-primary-800": t(T.surface),
    "--m-primary-900": t(T.page),
    "--m-primary-950": t(dark ? T.page : T.surface),
    "--m-primary-strong": t(T.accentStrong),
    "--m-secondary-500": t(T.accent),
    "--m-secondary-600": t(T.accentStrong),
    "--m-secondary-700": m(T.accentStrong, dark ? T.page : T.ink, 0.2),
    "--m-accent-500": t(T.accent),
    "--m-accent-600": t(T.accentStrong),
    "--m-accent-700": m(T.accentStrong, dark ? T.page : T.ink, 0.2),
    "--m-slate-50": t(T.ink),
    "--m-slate-100": t(T.ink),
    "--m-slate-200": m(T.ink, T.ink2, 0.5),
    "--m-slate-300": t(T.ink2),
    "--m-slate-400": t(T.muted),
    "--m-slate-500": m(T.muted, T.surface, dark ? 0.25 : 0.2),
    "--m-slate-600": m(T.muted, T.surface, dark ? 0.5 : 0.45),
    "--m-slate-700": t(T.line),
    "--m-slate-800": t(T.surface2),
    "--m-slate-900": t(dark ? T.surface : T.page),
    "--m-slate-950": t(dark ? T.page : T.surface),
    "--m-fg": t(T.ink),
    "--m-page-1": t(T.page),
    "--m-page-2": t(T.page),
    "--m-page-3": t(T.page),
    "--m-sidebar-from": T.surface,
    "--m-sidebar-to": T.surface,
    "--m-nav-fg": t(T.ink),
    "--m-nav-dim": t(T.ink2),
    "--m-nav-active": `${t(T.accent)} / 0.15`,
    "--m-nav-hover": t(T.line),
    "--m-scroll-track": T.page,
    "--m-scroll-thumb": `rgb(${m(T.line, T.ink, 0.15)})`,
    "--m-scroll-thumb-hover": `rgb(${m(T.line, T.ink, 0.3)})`,
    "--m-line": t(T.line),
    "--m-stage-bg": dark ? T.page : "#1f1f1f",
    "--m-review-page": t(T.page),
    "--m-review-header": t(T.surface),
    "--m-review-header-fg": t(T.ink),
  };
}

/** Every variable name the Mail scheme sets (for clearing inline values). */
export const MAIL_VAR_NAMES = Object.keys(mailSchemeVars(MAIL_LIGHT_TOKENS, "light"));

// ── applying to the document ────────────────────────────────────────────

/** The bits of <html> the scheme code touches (a fake in unit tests). */
export interface SchemeRoot {
  dataset: Record<string, string | undefined>;
  classList: { toggle(name: string, force?: boolean): unknown };
  style: { setProperty(name: string, value: string): void; removeProperty(name: string): unknown };
}

/** Stand-alone: Meet or Mail scheme with the built-in values (from CSS). */
export function applyScheme(root: SchemeRoot, scheme: SchemeName, mode: SchemeMode): void {
  for (const name of MAIL_VAR_NAMES) root.style.removeProperty(name);
  root.dataset.scheme = scheme;
  root.dataset.theme = mode;
  root.classList.toggle("light", mode === "light");
  root.classList.toggle("dark", mode === "dark");
}

export interface EmbedThemeInput {
  mode?: unknown;
  tokens?: Partial<Record<keyof MailTokens, unknown>> | null;
}

/**
 * Embedded in Mail: scheme `mail`, the parent's mode, and the parent's tokens
 * as inline variables (they override the built-in values live). Unknown or
 * malformed tokens fall back to Mail's built-in value for that mode.
 */
export function applyEmbedTheme(root: SchemeRoot, theme: EmbedThemeInput | null | undefined, fallbackMode: SchemeMode): SchemeMode {
  const mode: SchemeMode = theme?.mode === "dark" ? "dark" : theme?.mode === "light" ? "light" : fallbackMode;
  root.dataset.scheme = "mail";
  root.dataset.theme = mode;
  root.classList.toggle("light", mode === "light");
  root.classList.toggle("dark", mode === "dark");
  const vars = mailSchemeVars(completeTokens(theme?.tokens ?? null, mode), mode);
  for (const [name, value] of Object.entries(vars)) root.style.setProperty(name, value);
  return mode;
}

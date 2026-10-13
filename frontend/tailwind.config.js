/** Tailwind config — color tokens borrowed from one.witysk.org to match look-and-feel. */

// Colours are CSS variables (src/styles/schemes.css) so the Meet / Mail
// colour schemes can swap them at runtime; `<alpha-value>` keeps opacity
// modifiers such as `bg-primary-900/60` working. The defaults in
// schemes.css are the hex values this file used to hard-code.
const v = (name) => `rgb(var(--m-${name}) / <alpha-value>)`;
const scale = (name, shades) => Object.fromEntries(shades.map((s) => [s, v(`${name}-${s}`)]));
const ALL = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950];

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  theme: {
    extend: {
      colors: {
        primary: {
          // 50–900: #f1f5fb #dde6f2 #b6c7dd #7f9dbf #4f73a0 #1E3A5F #1A3354
          // #162C49 #12253E #0E1E33.
          // 950 (#0A1626): darkest surface used by panels/modals/inputs
          // (Meet++). Was missing, so `bg-primary-950` silently did nothing
          // and native controls fell back to a white background with light
          // text.
          ...scale("primary", ALL),
          // Hover of a filled primary button (= primary-600 in Meet's scheme;
          // Mail's accent-strong in the Mail scheme, where 600 is a grey).
          strong: v("primary-strong"),
        },
        // #2E5A8F #294F7E #24446D
        secondary: scale("secondary", [500, 600, 700]),
        // #4CAF50 #43A047 #388E3C
        accent: scale("accent", [500, 600, 700]),
        // Tailwind's slate by default; the Mail light scheme inverts it.
        slate: scale("slate", ALL),
        // White used as body text on the navy surfaces (`text-fg`); follows
        // the scheme, unlike `text-white` on coloured buttons.
        fg: v("fg"),
        // Sidebar navigation (values carry their own alpha).
        nav: {
          fg: "rgb(var(--m-nav-fg))",
          dim: "rgb(var(--m-nav-dim))",
          active: "rgb(var(--m-nav-active))",
          hover: "rgb(var(--m-nav-hover))",
        },
        // Meet++ review page chrome (the cards keep Meet++'s light palette).
        review: {
          page: "rgb(var(--m-review-page))",
          header: "rgb(var(--m-review-header))",
          "header-fg": "rgb(var(--m-review-header-fg))",
        },
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "Oxygen",
          "Ubuntu",
          "sans-serif",
        ],
      },
    },
  },
  plugins: [],
};

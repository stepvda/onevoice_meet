/** Tailwind config — color tokens borrowed from one.witysk.org to match look-and-feel. */
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  theme: {
    extend: {
      colors: {
        primary: {
          50:  "#f1f5fb",
          100: "#dde6f2",
          200: "#b6c7dd",
          300: "#7f9dbf",
          400: "#4f73a0",
          500: "#1E3A5F",
          600: "#1A3354",
          700: "#162C49",
          800: "#12253E",
          900: "#0E1E33",
        },
        secondary: {
          500: "#2E5A8F",
          600: "#294F7E",
          700: "#24446D",
        },
        accent: {
          500: "#4CAF50",
          600: "#43A047",
          700: "#388E3C",
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

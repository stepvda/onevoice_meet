import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8080",
      "/rtc": { target: "ws://localhost:7880", ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});

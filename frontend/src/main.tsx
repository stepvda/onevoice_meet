import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "@livekit/components-styles";
import "./styles/schemes.css";
import "./styles/global.css";
import "./i18n";
import App from "./App";
import { embed } from "./lib/embed";
import { applyEmbedTheme } from "./lib/scheme";

// Embedded in One Voice Mail? Decide before the first render (and before any
// One Voice SSO can start): the URL flags are read and stripped here, the
// parent allowlist is checked in the background (lib/embed.ts).
if (embed().detect() === "pending") {
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
  applyEmbedTheme(document.documentElement, null, prefersDark ? "dark" : "light");
  document.documentElement.dataset.embed = "ovm";
  void embed().start();
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);

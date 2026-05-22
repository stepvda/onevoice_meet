// PiP compositor service.
//
// Receives `POST /sessions/<room>` (start) / `DELETE /sessions/<room>`
// (stop) from meeting-api whenever the meeting's `pip_enabled` flag
// changes. For each active session it launches a single Puppeteer page
// pointed at the SPA's `/egress-layout/composite` route. That page
// joins the LiveKit room as `composite-<room>`, draws a PiP onto a
// canvas, and publishes the canvas via `captureStream` + LiveKit JS
// SDK — so every other client in the room ends up subscribed to a
// composite ScreenShare track without any client-side composition.
//
// Single browser, one page per room. The browser auto-restarts if it
// disconnects (chromium crash, OOM kill). Sessions are best-effort: if
// the page crashes mid-stream we relog it but the meeting-api will
// retry on the next `pip_enabled` toggle.

const express = require("express");
const puppeteer = require("puppeteer-core");

const PORT = Number(process.env.PORT || 8090);
const PUBLIC_URL = process.env.PUBLIC_URL || "https://meet.witysk.org";
const CHROMIUM_PATH = process.env.PUPPETEER_EXECUTABLE_PATH || "/usr/bin/chromium";

/** @type {Map<string, { page: import("puppeteer-core").Page, startedAt: number }>} */
const sessions = new Map();

/** @type {import("puppeteer-core").Browser | null} */
let browser = null;
let browserLaunchInFlight = null;

async function getBrowser() {
  if (browser && browser.connected) return browser;
  if (browserLaunchInFlight) return browserLaunchInFlight;
  browserLaunchInFlight = puppeteer
    .launch({
      executablePath: CHROMIUM_PATH,
      // **NOT headless**. Both `headless: true` (new) and `'shell'`
      // (old) fail to surface `canvas.captureStream` frames in this
      // container — verified by drawing a moving red bar on the
      // canvas every interval and getting a solid-black published
      // track on the subscriber side. Real Chrome works (proven by
      // manually opening the compositor URL in a browser tab). The
      // image installs Xvfb and the container's CMD wraps node in
      // `xvfb-run`, giving Chrome an in-memory display so it behaves
      // exactly like a real browser. ~30 MB extra RAM, well worth it.
      headless: false,
      args: [
        // Sandboxing requires UID > 0 or specific kernel features that
        // standard Docker bridge networking doesn't provide cleanly.
        // Trade off: this Chromium only ever loads our own SPA URL.
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        // Let the page autoplay video without a user gesture.
        "--autoplay-policy=no-user-gesture-required",
        // No fake camera/mic; we never publish from real devices.
        "--mute-audio",
        // Smaller window — only the canvas matters for captureStream,
        // but a sensible viewport keeps the SPA layout intact.
        "--window-size=1280,800",
        // Headless Chrome treats the page as "occluded" (no compositor
        // window is visible), which throttles requestAnimationFrame to
        // ~1Hz and pauses timers. That collapses the canvas draw loop
        // so `captureStream` produces a frozen / mostly-black track.
        // Disabling all three throttles makes the page behave as if
        // it were a foreground tab.
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        // Same family: forbid Chrome from "intensive wake-up" throttling
        // of timers / rAF when the page hasn't received user input.
        "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion",
        // Auto-accept any media-stream permission prompts (mic, cam,
        // screen). For `getDisplayMedia` the picker is bypassed and
        // the first available source is used.
        "--use-fake-ui-for-media-stream",
        // Chrome 113+ flag that auto-accepts current-tab capture
        // specifically — without it, `preferCurrentTab: true` still
        // shows the "share this tab?" prompt in some builds even with
        // `--use-fake-ui-for-media-stream`.
        "--auto-accept-this-tab-capture",
        // Pre-grant the page permission to enumerate / capture
        // displays, so the auto-pick logic doesn't bail because the
        // permission state defaults to "prompt".
        "--enable-features=AutoApproveScreenCaptureAccept",
        // Belt-and-braces: explicitly enable user-media screen
        // capturing, in case Chrome falls back to a stricter default.
        "--enable-usermedia-screen-capturing",
        // Start Chrome maximised so the page is rendered at the full
        // framebuffer size; current-tab capture then captures the
        // whole layout (main video full-bleed + overlay in the
        // bottom-right corner).
        "--start-maximized",
      ],
      defaultViewport: { width: 1280, height: 800 },
    })
    .then((b) => {
      browser = b;
      browserLaunchInFlight = null;
      b.on("disconnected", () => {
        console.error("compositor: chromium disconnected, clearing sessions");
        browser = null;
        sessions.clear();
      });
      return b;
    })
    .catch((err) => {
      browserLaunchInFlight = null;
      throw err;
    });
  return browserLaunchInFlight;
}

async function stopSession(room) {
  const s = sessions.get(room);
  if (!s) return false;
  sessions.delete(room);
  try {
    await s.page.close({ runBeforeUnload: false });
  } catch (err) {
    console.error(`compositor[${room}]: page.close error`, err.message);
  }
  return true;
}

async function startSession(room, { token, livekit_url, overlay_identity }) {
  // Idempotent: stop any prior session for the same room first so the
  // identity `composite-<room>` isn't claimed twice.
  await stopSession(room);

  const b = await getBrowser();
  const page = await b.newPage();

  // Forward page console + errors to the service's stdout so a single
  // `docker compose logs compositor` shows what the embedded SPA is
  // doing. Each line is prefixed with the room name for grep.
  page.on("console", (msg) => {
    const t = msg.type();
    const text = msg.text();
    if (t === "error" || t === "warning") {
      console.error(`compositor[${room}] page.${t}: ${text}`);
    } else {
      console.log(`compositor[${room}] page.${t}: ${text}`);
    }
  });
  page.on("pageerror", (err) => {
    console.error(`compositor[${room}] pageerror: ${err.message}`);
  });
  page.on("requestfailed", (req) => {
    console.error(
      `compositor[${room}] requestfailed: ${req.url()} (${req.failure()?.errorText})`,
    );
  });

  const url = new URL(`${PUBLIC_URL}/egress-layout/composite`);
  url.searchParams.set("room", room);
  url.searchParams.set("token", token);
  url.searchParams.set("url", livekit_url);
  if (overlay_identity) url.searchParams.set("overlay", overlay_identity);

  await page.goto(url.toString(), { waitUntil: "load", timeout: 30_000 });

  sessions.set(room, { page, startedAt: Date.now() });
}

const app = express();
app.use(express.json({ limit: "256kb" }));

app.get("/health", (_req, res) => {
  res.json({
    ok: true,
    sessions: Array.from(sessions.keys()),
    browser_connected: !!browser?.connected,
  });
});

app.post("/sessions/:room", async (req, res) => {
  const { room } = req.params;
  const { token, livekit_url, overlay_identity } = req.body || {};
  if (typeof token !== "string" || !token) {
    return res.status(400).json({ error: "missing token" });
  }
  if (typeof livekit_url !== "string" || !livekit_url) {
    return res.status(400).json({ error: "missing livekit_url" });
  }
  try {
    await startSession(room, {
      token,
      livekit_url,
      overlay_identity: typeof overlay_identity === "string" ? overlay_identity : "",
    });
    res.json({ ok: true, room });
  } catch (err) {
    console.error(`compositor[${room}] start failed:`, err);
    res.status(500).json({ error: err.message || String(err) });
  }
});

app.delete("/sessions/:room", async (req, res) => {
  const { room } = req.params;
  const stopped = await stopSession(room);
  res.json({ ok: true, stopped });
});

app.get("/sessions", (_req, res) => {
  res.json({
    sessions: Array.from(sessions.entries()).map(([room, s]) => ({
      room,
      uptime_ms: Date.now() - s.startedAt,
    })),
  });
});

// Graceful shutdown so SIGTERM (compose stop / restart) closes
// chromium cleanly rather than leaving zombie processes that take a
// few seconds to GC on the next launch.
async function shutdown() {
  console.log("compositor: shutting down");
  for (const room of Array.from(sessions.keys())) {
    await stopSession(room);
  }
  if (browser) {
    try {
      await browser.close();
    } catch (e) {
      console.error("compositor: browser.close error", e.message);
    }
  }
  process.exit(0);
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

app.listen(PORT, () => {
  console.log(
    `compositor listening on :${PORT} (public_url=${PUBLIC_URL}, chromium=${CHROMIUM_PATH})`,
  );
});

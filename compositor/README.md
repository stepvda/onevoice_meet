# compositor

Picture-in-picture composition for LiveKit recordings and livestreams. A tiny Node + Express service that runs Puppeteer with Chromium under `xvfb-run`.

## What it does

When the meeting host enables PiP (picture-in-picture egress layout), `meeting-api` POSTs to `compositor:8090/sessions/<room>`. This service:

1. Launches a Chromium page (one per active room) pointed at `https://<public-url>/egress-layout/composite?room=<...>&token=<...>`.
2. That page joins the LiveKit room as `composite-<room>` and draws a canvas:
   - **Main source** (full-bleed): active screenshare → video playback participant → active speaker (fallback chain).
   - **Corner overlay**: the participant whose `pip_overlay_identity` is configured on the meeting.
3. The page publishes `canvas.captureStream()` as a ScreenShare track via the LiveKit JS SDK.
4. LiveKit Egress (the recording / livestream worker) records that screenshare with the standard "single-speaker" preset.

`DELETE /sessions/<room>` tears the page down.

This indirection — "make a participant publish the composite, then record/livestream the composite as if it were a screenshare" — lets us achieve a custom layout without writing a new egress template engine. The trade-off is one extra Chromium instance per active PiP meeting (~150 MB RAM at idle).

## Why not headless?

Both `headless: true` (new) and `headless: 'shell'` (old) Chrome fail to surface `canvas.captureStream` frames in this container — subscribers see a solid black track. Real Chrome works. The Docker image installs Xvfb and wraps Node in `xvfb-run`, giving Chrome an in-memory virtual display. Costs ~30 MB extra RAM and is the only path that works reliably.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/sessions/<room>` | Start a PiP page for this room. |
| DELETE | `/sessions/<room>` | Stop a PiP page. |
| GET | `/health` | Liveness probe. |

The service binds to port 8090 inside the bridge network. It is **never** exposed externally — `meeting-api` reaches it via Docker DNS as `compositor:8090`.

## Resilience

- Single browser, restarted automatically if it disconnects (chromium crash, OOM kill).
- Sessions are best-effort: if a page crashes mid-meeting, the service logs it but does not auto-relaunch. Meeting-api retries the next time the host toggles PiP.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8090` | Listen port. |
| `PUBLIC_URL` | `https://meet.witysk.org` | Base URL the Chromium pages load from. |
| `PUPPETEER_EXECUTABLE_PATH` | `/usr/bin/chromium` | Path to the Chromium binary inside the image. |

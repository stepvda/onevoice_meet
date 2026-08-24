# `one.witysk.org` integration — SSO bootstrap

meet.witysk.org needs a way to read the access_token that the onevoice SPA
stores in localStorage. localStorage is per-origin, so a subdomain cannot
share data with another subdomain directly.

This directory contains a **single static HTML file** that needs to be served
from `https://one.witysk.org/sso-bootstrap.html`. No backend changes are
required.

## How it works

1. User logs in on `one.witysk.org` → access_token stored in localStorage.
2. User visits `https://meet.witysk.org/`.
3. meet's SPA mounts a hidden iframe pointing at `https://one.witysk.org/sso-bootstrap.html`.
4. That page reads `localStorage.getItem("access_token")` (same-origin, so it can).
5. It posts the token to `window.parent` via `postMessage`, with an explicit
   `targetOrigin` of `https://meet.witysk.org` (never `"*"`).
6. meet's SPA receives the message, validates the origin, stores the token in
   its own localStorage, and proceeds as a logged-in user.

## Deployment

Copy `sso-bootstrap.html` to wherever one.witysk.org's frontend static assets
are served from. Typical paths, based on the onevoice deploy layout:

```
/Users/nstephane/Dev/onevoice/react/frontend/public/sso-bootstrap.html   # dev
/opt/onevoice/react/frontend/dist/sso-bootstrap.html                     # prod?
```

If onevoice serves the SPA with Vite's `public/` directory mechanism, drop the
file into `react/frontend/public/sso-bootstrap.html` and it will be copied
into `dist/` on the next `npm run build`.

If onevoice serves the built SPA through Caddy + `file_server`, just put the
file next to `index.html`.

Verify after deploy:

```
curl -sSI https://one.witysk.org/sso-bootstrap.html | head -5
# should return 200 OK with text/html
```

No backend changes. No DB changes. No Caddy changes.

## Security

- `postMessage` uses an explicit `targetOrigin`; the browser drops the message
  if the actual parent origin does not match.
- meet.witysk.org validates `event.origin === "https://one.witysk.org"` on the
  receiving end.
- The iframe is invisible and the user never interacts with it — no
  clickjacking surface.
- An attacker running JS on `one.witysk.org` could already read localStorage
  directly; this page adds no new attack surface for that origin.
- To add another trusted subdomain later, add it to the `ALLOWED_ORIGINS`
  array in the HTML file.

## Testing

Once deployed:

1. Log into one.witysk.org.
2. Open `https://meet.witysk.org/` in the same browser.
3. The CreateMeeting form should appear (not the "sign in on one.witysk.org"
   message). Under the hood, meet has just bootstrapped your token.

## Profile handoff (2026-08-24)

`sso-bootstrap.html` now also fetches `/api/auth/me` **same-origin** and
piggybacks `name` / `username` / `email` on the `witysk-sso` postMessage
payload. Rationale: DPoP-bound one.witysk.org sessions reject meet's own
cross-origin `/api/auth/me` call (meet cannot sign a proof — the key is
non-extractable in one.witysk.org's IndexedDB), which left SSO users named
"User <sub>" in meetings. The bootstrap page CAN sign the proof (same
`ov-dpop` IndexedDB key the onevoice SPA uses; the /me proof carries `ath`
per RFC 9449 §4.3), with bearer-only and cookie-only fallbacks for unbound
sessions. Best-effort and capped at 1200 ms so the token handoff itself is
never delayed past the parent's timeout; on any failure the payload is
token-only, exactly like older builds.

meet's SPA caches the received profile in localStorage (`witysk_profile`)
and uses it whenever its direct fetch fails (lobby name prefill, owner
token mint, invites).

Redeploy = copy this file over the served one on one.witysk.org, same as
the original deployment. The page is maintained in the onevoice repo as
well — keep the two in sync (this version was built on top of the deployed
build fetched 2026-08-24, which added token refresh + DPoP; the previous
copy in this directory predated both).

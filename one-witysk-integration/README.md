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

# caddy

TLS termination + reverse proxy + static SPA file server. [Caddy 2](https://caddyserver.com) handles cert provisioning, HTTPS redirects, HTTP/2 and HTTP/3, and routes traffic to the right service based on URL path.

## Files

| File | Purpose |
| --- | --- |
| `Caddyfile` | Site config — domain, path routes, per-path CSP. |

## Path routing

| Path | Routed to | Notes |
| --- | --- | --- |
| `/rtc/*` | `host.docker.internal:7880` (LiveKit) | WebSocket signaling. Prefix stripped so LiveKit gets the canonical `/rtc/v1` it expects. |
| `/api/*` | `meeting-api:8080` | All REST endpoints (and the LiveKit webhook receiver). |
| `/rec/*` | `meeting-api:8080` | Recording downloads (range-supported). |
| `/public/*` | static SPA, no framing restriction | View-only embeddable viewer (CSP `frame-ancestors *`). |
| `/egress-layout/*` | static SPA, no framing restriction | Custom Web template loaded by LiveKit Egress headless Chrome. CSP `connect-src *` because the egress runner passes the LiveKit WS URL as a query param. |
| `/*` (everything else) | static SPA, strict framing | Strictest CSP. `frame-ancestors 'none'`. |

## Why three different CSPs

Each path has different framing requirements:

- **`/*` (the main SPA)** is locked down. `frame-ancestors 'none'` protects against clickjacking. We allow `cdn.jsdelivr.net` because `@livekit/track-processors` fetches MediaPipe wasm + tflite models from there at runtime — without that allowance, virtual backgrounds silently fail. We allow `paypal.com` / `www.paypalobjects.com` because the embedded PayPal Buttons SDK loads from those origins. We allow `https://one.witysk.org` in `frame-src` so the SSO bootstrap iframe can mount.
- **`/public/*`** is meant to be embedded in arbitrary third-party sites (blogs, marketing pages). So we use `frame-ancestors *` and omit `X-Frame-Options`.
- **`/egress-layout/*`** is fetched by LiveKit Egress's headless Chrome with no cookies. The page receives the LiveKit WS URL as a query param and needs to connect to it. We can't predict the URL ahead of time (in a multi-host setup it could be anything), so `connect-src *` is the pragmatic answer. The page is only ever loaded by the trusted egress runner — no end user opens it directly.

Caddy doesn't merge `header` directives across `handle` blocks cleanly when path-matched and unmatched headers coexist at the site level. The Caddyfile keeps path-specific framing rules inside each `handle` block to avoid silent overrides.

## Path-independent headers

Set at the site level, apply to every response:

```
Strict-Transport-Security: max-age=31536000; includeSubDomains; preload
X-Content-Type-Options: nosniff
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: camera=(self), microphone=(self), display-capture=(self)
```

## TLS

Caddy auto-provisions Let's Encrypt certificates on first boot. Requirements:

- DNS for your domain must point at the host.
- Port 80 must be reachable from the public internet (for the HTTP-01 challenge).
- Port 443 should be reachable (for normal HTTPS traffic).

Certs and OCSP staples are stored in the `caddy_data` Docker volume; renewals are automatic.

For local dev (no real DNS / cert), Caddy falls back to its self-signed local CA at `https://localhost`. Browsers will warn — accept the warning for development.

## Swapping the domain

Replace `meet.witysk.org` everywhere in `Caddyfile`. There's also a hard-coded email at the top for Let's Encrypt notifications — change that too.

## Caddy collision

If the host already has something on 80/443 (nginx, another Caddy for the same operator's admin UI), see [`DEPLOYMENT.md` §5](../DEPLOYMENT.md#5-caddy-collision-if-another-proxy-exists-on-80443) for two options: bind this Caddy to a loopback port and reverse-proxy from the host's main proxy, or merge this Caddy's blocks into the host's existing Caddyfile.

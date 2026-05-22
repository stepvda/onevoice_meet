# Security

This document covers the security posture of `onevoice-meet`: auth, transport, data, rate limits, intrusion detection. It does *not* relieve you of the obligation to threat-model your own deployment.

## Reporting vulnerabilities

If you find a security issue, please **do not** open a public GitHub issue. Email **stephane@stepvda.com** with a short description and reproduction. I'll acknowledge within a week.

## Authentication

### JWT — HS256 shared secret

The reference deployment uses HS256 JWTs with a secret shared between this service and the upstream SSO issuer (`one.witysk.org`). This is **not** the design recommended by modern guidance (RS256 with a JWKS endpoint would be safer); it matches the existing reality of the integration partner.

If you're deploying on your own, the practical implications:

- **Same secret on both sides.** Set `JWT_SECRET_KEY` on `meet` to match the upstream issuer's secret. If the secret leaks anywhere, both services are compromised.
- **No revocation list.** Tokens are valid until they expire. There is no `iat` window enforcement; if you need short-lived tokens, set them short on the issuer side.
- **One token format.** Both native-meet logins and upstream-SSO tokens validate against the same code path; they differ only by the `iss` claim (`"meet"` vs. absent). See [`meeting-api/app/auth.py`](../meeting-api/app/auth.py).

Migrating to RS256 + JWKS is a self-contained change in `auth.py` — the rest of the codebase only cares about `AuthUser`. The change requires the upstream issuer to publish a JWKS endpoint first.

### Native account passwords

Native passwords are hashed with [Argon2id](https://argon2.online/) via `passlib[argon2]` defaults. Passwords are never logged, even at debug. Reset tokens are 32 random bytes; the secret is mailed to the user, the SHA-256 digest is stored in `password_reset_tokens` — so a DB leak doesn't expose live reset tokens.

### Two-factor authentication

Native accounts can enable:

- **TOTP (RFC 6238)** via `pyotp`. Secrets stored as base32 in the user row. Recovery codes are Argon2-hashed in a JSON array; each is single-use (deleted on consumption).
- **Email-OTP**. 6-digit codes stored in Redis with a 5-minute TTL.

The login flow is split into two stages: `POST /api/v1/auth/login` returns either a full token (no 2FA) or a `challenge_token` (2FA enabled); the SPA then POSTs `/auth/login/2fa` or `/auth/login/email-otp` with the challenge + code. Brute-force on the second stage is a separate, tighter IDS bucket (`IDS_TWOFA_BRUTE_FORCE_THRESHOLD`, default 5 attempts / 5 min).

## Transport

- **TLS 1.2+** via Caddy. Let's Encrypt cert auto-provisioned on first boot when port 80 is reachable.
- **HSTS** with `max-age=31536000; includeSubDomains; preload` (don't enable preload until you're committed — preload removal takes months).
- **HTTP/2 and HTTP/3** offered by Caddy.

## Content Security Policy

Path-specific CSP headers are set in [`caddy/Caddyfile`](../caddy/Caddyfile). Three policies:

| Path | Why it differs |
| --- | --- |
| `/*` (the SPA fallback) | Strictest: `frame-ancestors 'none'`, locked-down sources. Allows `cdn.jsdelivr.net` for the MediaPipe wasm bundle the virtual-background pipeline loads at runtime, and `one.witysk.org` for the SSO iframe. Allows PayPal hosts for embedded checkout buttons. |
| `/public/*` (view-only stream) | Identical sources, but `frame-ancestors *` because the public view is meant to be embedded in arbitrary third-party sites (blogs, marketing pages). |
| `/egress-layout/*` (LiveKit Egress headless-Chrome template) | `connect-src *` because the egress container hits the page with the LiveKit WS URL as a query param; the page must connect to whatever URL the egress runner chose. Only the trusted egress chrome ever loads these paths. |

Other path-independent headers:

- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(self), microphone=(self), display-capture=(self)`

## CORS

`meeting-api` allows three origins (see `main.py`):

- `PUBLIC_URL` (the configured deployment URL)
- `https://one.witysk.org` (because the Café widget on the SSO origin calls our API directly)
- `http://localhost:5173` (Vite dev server)

If you swap in a different SSO issuer, update the CORS list.

## Rate limiting

Redis-backed sliding-window buckets ([`services/rate_limit.py`](../meeting-api/app/services/rate_limit.py)) on:

- Anonymous LiveKit token requests (default 30/hour/IP).
- Native signup, login, password-reset request (sane defaults inside each route).
- Voucher redemption.

Fails **open** if Redis is unreachable — the bias is toward accepting requests rather than blocking all of them during a Redis outage. If Redis loss is a likely scenario in your deployment and you'd rather fail closed, change the Try/Except in `rate_limit.py`.

## Intrusion detection (IDS)

[`services/intrusion_detector.py`](../meeting-api/app/services/intrusion_detector.py) is a small in-memory detector that watches three signals per IP:

1. **Brute-force auth failures.** N failures within W seconds → temp-block.
2. **Brute-force 2FA failures.** Tighter (5 within 5 min) — implies a stolen password.
3. **Path scanning.** Many 404s in a short window (vulnerability scanners).

Events are persisted to `security_events` for the admin panel's history view (in-memory windows show only the recent past). Temp blocks live in RAM (cleared on restart). Manual permanent blocks live in `blocked_ips` and survive restarts.

## IP blocking

[`services/ip_block.py`](../meeting-api/app/services/ip_block.py) is the outermost middleware. Three entry formats:

- Exact IP — `203.0.113.5` or `2001:db8::1`
- CIDR — `203.0.113.0/24` or `2001:db8::/32`
- Dash range (IPv4 last octet only) — `203.0.113.5-50`

Blocks are an in-memory cache reloaded from the DB on admin mutations. Lookup is O(1) for exact IPs and O(N) for CIDR / range entries; N is small in practice (the cache is admin-curated).

## Audit logs

Every moderation action and significant ownership change writes a row to `moderation_audit`:

```
meeting_id, actor_user_id, action, target_identity, details, created_at
```

`action` values: `mute`, `kick`, `lower_hand`, `presenter`, `recording_start`, `recording_stop`, `stream_start`, `stream_stop`, `playback_start`, `playback_stop`, `cohost_promote`, `cohost_demote`. Read via SQL — there's no admin UI for the audit log today.

## Data retention

| Data | Retention | Mechanism |
| --- | --- | --- |
| Recordings (MP4 + transcript) | `RECORDING_RETENTION_DAYS` (default 30) or disk-cap (`RECORDING_DISK_CAP_RATIO`, default 90%) | APScheduler nightly + on-write |
| Chat, polls, whiteboard, notes | Until meeting hard-delete | None automatic |
| Audit log | Forever (rows accumulate) | Manual SQL cleanup |
| `security_events` | Forever | Manual SQL cleanup |
| Logs (`app.log`, `requests.log`, `db.log`) | 180 days | `GzipTimedRotatingFileHandler` daily rotation |
| Redis sessions / OTP codes | TTL'd | Set per-key |
| Account data | Until user clicks "delete account" | Self-service |

## Privacy controls

Per-user toggles, stored in `UserPreferences`:

- **Anonymise email in join log** — when set, joins by this user record `<redacted>` instead of the email.
- **Don't log my IP** — reserved hook; the access log mw still records IP today (the field is wired but not yet enforced).
- **Privacy mode (client-side)** — blurs real names + emails in the participant panel and chat. Useful for screen-shared meetings.

## Dependencies

- **Python** — `requirements.txt` pins minor versions for reproducibility. Run `pip-audit` periodically.
- **JavaScript** — `package-lock.json` committed. Run `npm audit` periodically.
- **LiveKit / Caddy / Redis** — pinned by tag in `docker-compose.yml`. Re-pin and test before deploying.

## Threat model — what this is *not* designed to defend against

- **Compromised host operator.** Anyone with shell access to the host can read the SQLite DB, the recordings, the `.env` secrets, and intercept TLS termination. Encrypt at rest if your trust model requires it.
- **Compromised SSO issuer.** A stolen `JWT_SECRET_KEY` lets the holder mint admin tokens for any user. Treat the secret as production-critical.
- **Coordinated DDoS.** The IDS and rate limits are designed to slow individual attackers, not absorb traffic floods. Run behind Cloudflare or similar if you're a target.
- **Browser-level XSS through user-generated content.** Chat messages, meeting titles, notes, and whiteboard text are stored as plain text and rendered as text (not HTML). React's JSX escaping is the only defense — adding `dangerouslySetInnerHTML` *anywhere* could undo this.

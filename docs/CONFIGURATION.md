# Configuration

Every runtime knob lives in `.env`. `meeting-api` reads it via [`pydantic-settings`](https://docs.pydantic.dev/latest/concepts/pydantic_settings/); `docker-compose.yml` interpolates the `LIVEKIT_*` values into the egress / ingress configs at boot.

Copy `.env.example` to `.env` and fill in the secrets. Never commit `.env`.

## Required

| Variable | Example | Notes |
| --- | --- | --- |
| `LIVEKIT_API_KEY` | `APInP1Z2…` | Generate with `docker run --rm livekit/livekit-server generate-keys`. |
| `LIVEKIT_API_SECRET` | (64-char base64) | Pair with the key above. |
| `LIVEKIT_WEBHOOK_KEY` | same as `LIVEKIT_API_KEY` | Used to sign webhook calls from LiveKit → meeting-api. |
| `LIVEKIT_EXTERNAL_IP` | `203.0.113.10` | Public IP of the LiveKit host. Set explicitly for reliability behind NAT. |
| `JWT_SECRET_KEY` | random 64+ chars | HS256 shared secret. **Must match** the SSO issuer's secret if integrating with one. |
| `TURN_STATIC_AUTH_SECRET` | random 64+ chars | Shared with `coturn`'s `static-auth-secret`. Empty disables TURN. |

## LiveKit

| Variable | Default | Notes |
| --- | --- | --- |
| `LIVEKIT_WS_URL` | `wss://meet.witysk.org/rtc` | What the SPA connects to. Override for non-reference deployments. |

## meeting-api

| Variable | Default | Notes |
| --- | --- | --- |
| `MEETING_API_PORT` | `8080` | Internal port; not exposed publicly. |
| `DATABASE_URL` | `sqlite:////var/lib/meet/meet.db` | SQLAlchemy URL. Switch to Postgres for multi-host setups. |
| `RECORDINGS_DIR` | `/var/lib/meet/recordings` | Must be writable by uid 1001 (egress container). |
| `RECORDING_RETENTION_DAYS` | `30` | APScheduler nightly sweep deletes expired recordings. |
| `RECORDING_DISK_CAP_RATIO` | `0.90` | When the filesystem reaches this fraction full, oldest recordings are GC'd. |
| `RECORDING_PRESET_1080P` | `false` | Toggle to 1080p30 (≈1 extra CPU core). |
| `PUBLIC_URL` | `https://meet.witysk.org` | Used for generated join links and CORS. |
| `REDIS_URL` | `redis://redis:6379/0` | Shared with rate-limit, IDS, email-OTP. |
| `ANON_TOKEN_RATE_PER_HOUR` | `30` | Per-IP cap on anonymous-token requests. |

## TURN

| Variable | Default | Notes |
| --- | --- | --- |
| `TURN_HOST` | `turn.witysk.org` | Hostname of the TURN server. |
| `TURN_STATIC_AUTH_SECRET` | _(empty)_ | If empty, TURN credentials are not minted — calls work but may fail behind strict NAT. |
| `TURN_TTL_SECONDS` | `3600` | How long each minted credential is valid. |

## JWT / SSO

| Variable | Default | Notes |
| --- | --- | --- |
| `JWT_ALGORITHM` | `HS256` | Don't change unless the upstream SSO does too. |
| `JWT_AUDIENCE` | `meet.witysk.org` | `aud` claim on minted tokens. |

## SSO bridge (optional)

If you're integrating with `one.witysk.org` or a similar SSO issuer, deploy `one-witysk-integration/sso-bootstrap.html` to that origin so the SPA can read the access token via postMessage. See [`one-witysk-integration/README.md`](../one-witysk-integration/README.md).

To swap in a different SSO issuer, set `JWT_SECRET_KEY` to match its secret. The auth layer accepts any HS256 token signed with that key, so any issuer that emits compatible tokens (subject + signature) works.

## Platform admin

| Variable | Default | Notes |
| --- | --- | --- |
| `PLATFORM_ADMIN_EMAILS` | `stephane@stepvda.com,david.iorlano@pm.me` | Comma-separated. Re-applied on every startup — adding an email here promotes the matching user next boot. |
| `VOUCHER_ADMIN_USER_IDS` | `1,404` | one.witysk.org user IDs that can issue/list vouchers from `/vouchers`. |
| `VOUCHER_SIGNING_KEY` | _(empty)_ | HMAC secret for voucher codes. Rotating it invalidates all outstanding vouchers. |

## Email (Resend)

| Variable | Default | Notes |
| --- | --- | --- |
| `RESEND_API_KEY` | _(empty)_ | Empty = email no-ops (useful for dev). |
| `FROM_EMAIL` | `meet@witysk.org` | Supports `"Display Name <addr@domain>"`. |
| `INVITE_REPLY_TO` | _(empty)_ | Set if invite replies should go somewhere other than the From address. |

## PayPal

| Variable | Default | Notes |
| --- | --- | --- |
| `PAYPAL_CLIENT_ID` | _(empty)_ | From the PayPal Business developer dashboard. |
| `PAYPAL_CLIENT_SECRET` | _(empty)_ | Pair with client_id. |
| `PAYPAL_API_BASE` | `https://api-m.paypal.com` | Sandbox: `https://api-m.sandbox.paypal.com`. |
| `PAYPAL_PLAN_ID_MONTHLY` | _(empty)_ | Subscription plan ID. |
| `PAYPAL_PLAN_ID_ANNUAL` | _(empty)_ | (Reserved — annual is currently one-shot.) |
| `PAYPAL_WEBHOOK_ID` | _(empty)_ | Required for production. Empty skips signature checks (insecure). |
| `PAYPAL_MONTHLY_PRICE` | `2.00` | Bill-once monthly amount. |
| `PAYPAL_ANNUAL_PRICE` | `20.00` | Bill-once annual amount. |
| `PAYPAL_MONTHLY_CURRENCY` | `EUR` | Currency for monthly. |
| `PAYPAL_ANNUAL_CURRENCY` | `EUR` | Currency for annual. |

## YouTube

| Variable | Default | Notes |
| --- | --- | --- |
| `YOUTUBE_CLIENT_ID` | _(empty)_ | OAuth2 desktop client. |
| `YOUTUBE_CLIENT_SECRET` | _(empty)_ | Pair with client_id. |
| `YOUTUBE_REFRESH_TOKEN` | _(empty)_ | One-time token from `scripts/youtube_oauth.py`. |
| `YOUTUBE_DEFAULT_PRIVACY` | `unlisted` | `public` / `unlisted` / `private`. Overridable per upload. |

## Transcription

| Variable | Default | Notes |
| --- | --- | --- |
| `WHISPER_URL` | `http://whisper:8080/inference` | OpenAI-compatible endpoint. Empty disables transcription entirely. |
| `WHISPER_THREADS` | `2` | Sized for the 2-vCPU Hetzner box; bump on bigger hosts. |

## Compositor

| Variable | Default | Notes |
| --- | --- | --- |
| `COMPOSITOR_URL` | `http://compositor:8090` | Internal — meeting-api reaches via Docker DNS. |

## Intrusion detection

All defaults in [`config.py`](../meeting-api/app/config.py). To tune:

| Variable | Default | Notes |
| --- | --- | --- |
| `IDS_ENABLED` | `true` | Master switch. |
| `IDS_BRUTE_FORCE_THRESHOLD` | `10` | Auth failures within the window → temp-block. |
| `IDS_BRUTE_FORCE_WINDOW_SECONDS` | `300` | Sliding window for auth-fail counting. |
| `IDS_TWOFA_BRUTE_FORCE_THRESHOLD` | `5` | Tighter — 2FA failures imply a stolen password. |
| `IDS_TWOFA_BRUTE_FORCE_WINDOW_SECONDS` | `300` |  |
| `IDS_PATH_SCAN_THRESHOLD` | `30` | 404s within the window — generic scanner detection. |
| `IDS_PATH_SCAN_WINDOW_SECONDS` | `60` |  |
| `IDS_TEMP_BLOCK_MINUTES` | `30` | Duration of auto-temp blocks. |
| `IDS_MAX_EVENTS_PER_IP` | `200` | In-memory ring cap per IP (memory-bounded under flooding). |

## File system layout

These paths are bind-mounted into containers; create the host directories before `docker compose up` if you want non-default ownership:

```
/var/lib/meet/                      # parent of all persistent data
├── meet.db                         # SQLite database
├── recordings/                     # MP4s from LiveKit Egress (owned by uid 1001)
├── playback/<meeting_id>/          # uploaded playlist MP4s
├── whats_next_cache/               # cached rundown slides (gc'd by scheduler)
├── branding/<meeting_id>.<ext>     # per-meeting branding image
├── chat-attachments/<sha>.<ext>    # chat image attachments
└── facepics/<user_id>.<ext>        # native-user avatars

/var/log/meet/                      # rotated logs (daily, gzipped, 180-day)
├── app.log
├── requests.log
└── db.log
```

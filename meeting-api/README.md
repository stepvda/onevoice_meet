# meeting-api

The control plane. A FastAPI (Python 3.12) service that:

- Verifies JWTs (HS256, dual-issuer: native meet + upstream SSO)
- Mints LiveKit join tokens
- Owns the SQLite database (meetings, users, recordings, chat, polls, whiteboard, IDS state)
- Receives LiveKit webhooks and reacts to participant / egress / ingress events
- Orchestrates recording, livestreaming, in-meeting video playback
- Talks to PayPal (billing), YouTube (manual publish), Resend (transactional email), and whisper.cpp (transcription)
- Runs intrusion detection and serves the platform admin panel

## Quick links

- [REST API reference](../docs/API.md)
- [Configuration / `.env`](../docs/CONFIGURATION.md)
- [Architecture overview](../docs/ARCHITECTURE.md)

## Layout

```
app/
├── main.py                # FastAPI app factory, middleware stack, router wire-up
├── auth.py                # JWT decode, AuthUser, RequireUser / RequireAdmin / RequirePlatformAdmin deps
├── config.py              # Pydantic Settings (every .env knob)
├── db.py                  # SQLAlchemy engine + sessionmaker, lightweight_migrate()
├── models.py              # ORM models for every table
├── livekit_client.py      # LiveKit Server API wrapper + token minting + TURN creds
├── logging_config.py      # Three rotating log files (app / requests / db)
├── scheduler.py           # APScheduler background jobs (retention, disk cap, watchdog)
├── webhooks.py            # LiveKit webhook receiver (signed-JWT verified)
├── routes/                # FastAPI routers, one per area
│   ├── meetings.py        # Meeting CRUD + discover + branding
│   ├── tokens.py          # Anonymous-join LiveKit token endpoint
│   ├── moderation.py      # mute / kick / lower-hand / presenter
│   ├── waiting_room.py    # Pending-joiner admit/deny + poll
│   ├── recordings.py      # Start/stop/list/download + YouTube publish
│   ├── streams.py         # Livestream start/stop + per-destination status
│   ├── playback.py        # In-meeting video playlist + start/stop/pause/resume
│   ├── polls.py           # Polls + Q&A
│   ├── chat.py            # Chat + whiteboard + notes
│   ├── auth_native.py     # Native signup/login/password reset, facepic, /me
│   ├── totp.py            # TOTP + email-OTP 2FA
│   ├── users.py           # User preferences
│   ├── ti_cafe.py         # Always-on TI Café audio room
│   ├── vouchers.py        # Issue / redeem / revoke vouchers
│   ├── billing.py         # PayPal orders, subscriptions, webhooks
│   ├── admin.py           # Platform admin: users, blocked IPs, IDS
│   └── health.py          # Liveness probe
└── services/              # Cross-route domain logic
    ├── egress_mgr.py      # Unified record + stream orchestration (one egress slot)
    ├── playback_mgr.py    # Playlist advance + freeze-frame pause + What's Next
    ├── whats_next_slide.py # 35s rundown slide MP4 generation + content-hash cache
    ├── transcription.py   # MP4 → WAV → whisper.cpp → .txt → email
    ├── youtube.py         # OAuth2 + resumable upload
    ├── paypal.py          # PayPal REST v2 Orders + v1 Subscriptions
    ├── vouchers.py        # 8-char code generation + HMAC verification
    ├── email.py           # Resend HTTP API wrapper
    ├── email_templates.py # HTML + text email templates
    ├── ics.py             # iCalendar (RFC 5545) generation
    ├── intrusion_detector.py # Sliding-window IDS (auth / 2FA / path scan)
    ├── ip_block.py        # IP / CIDR / dash-range blocklist + middleware
    ├── rate_limit.py      # Redis-backed sliding-window limiter
    └── slug_words.py      # 3-word URL-safe room-name generator
```

## Running locally

See [docs/DEVELOPMENT.md → meeting-api standalone](../docs/DEVELOPMENT.md#meeting-api-standalone).

## Tests

```bash
pytest
```

`pytest` uses FastAPI's `TestClient` with a fresh SQLite file per session — no Docker dependency.

## Database

SQLAlchemy 2.0 with SQLite. Schema is created on first boot via `Base.metadata.create_all()`. Subsequent additive changes are handled by `lightweight_migrate()` in [`db.py`](app/db.py) — it runs `ALTER TABLE ADD COLUMN` for each new field defined in `models.py` but missing on disk.

Why no Alembic? This is a single-box self-hostable service; there's only ever one writer; rollbacks are SQL'ed by hand on the rare occasions they're needed. Adding Alembic is a worthwhile change if you're forking for a larger deployment.

## Logs

Three rotating files under `/var/log/meet/`:

| File | What | Bytes/day at the reference deployment |
| --- | --- | --- |
| `app.log` | Application events via `log_event("kind", k=v)` | ~1–5 MB |
| `requests.log` | One line per HTTP request | ~10–50 MB |
| `db.log` | SQLAlchemy noise | ~10 MB |

Daily rotation, gzip on rotation, 180-day retention. Bind-mounted from the host so they survive container restarts.

## Webhook receiver

LiveKit sends signed webhook events to `POST /api/v1/webhooks/livekit`. The handler:

- Verifies the `Authorization: <jwt>` header against `LIVEKIT_WEBHOOK_KEY`.
- Switches on `event` field:
  - `participant_joined` / `participant_left` — Café presence; meeting participant tracking.
  - `room_finished` — Meeting cleanup.
  - `egress_started` / `egress_updated` / `egress_ended` — Recording + livestream state. Per-destination push status updates `livestream_destination_states`.
  - `ingress_ended` — Playback advance (next playlist item or stop).

The endpoint is loopback-only — Caddy doesn't expose it externally; LiveKit (host network) POSTs to `http://127.0.0.1:8080/api/v1/webhooks/livekit`.

## Background jobs

`scheduler.py` registers four APScheduler jobs at startup:

- **Recording retention sweep** — daily at 03:00 UTC, deletes `expires_at < now` recordings.
- **Disk-cap GC** — every 5 minutes, deletes oldest recordings when the recordings filesystem exceeds `RECORDING_DISK_CAP_RATIO`.
- **What's Next cache eviction** — daily, removes unused rundown slides.
- **Playback watchdog** — every minute, recovers from silent ingress crashes (advances or stops if the active ingress has gone away).

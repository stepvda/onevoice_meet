# Architecture

This document explains how the services in `onevoice-meet` fit together: what each container is responsible for, how traffic flows in and out, and where state lives.

## Top-level view

The stack runs as eight containers behind a single TLS-terminating reverse proxy. Seven of them are defined in [`docker-compose.yml`](../docker-compose.yml); the eighth — `coturn` — runs **outside** Docker on the host and is shared with other services on the same box.

| Container       | Image base                  | Role                                                              |
| --------------- | --------------------------- | ----------------------------------------------------------------- |
| `caddy`         | `caddy:2`                   | TLS termination, reverse proxy, static SPA file server.           |
| `frontend-build`| custom                      | One-shot: builds the React SPA into a shared volume, then exits.  |
| `meeting-api`   | custom (Python 3.12)        | FastAPI control plane. The brain of the application.              |
| `livekit`       | `livekit/livekit-server`    | WebRTC SFU. Host-networked so UDP 50000-60000 stays disjoint.     |
| `egress`        | `livekit/egress`            | Headless-Chrome record + livestream worker.                       |
| `ingress`       | `livekit/ingress`           | Pulls MP4s from `meeting-api` and republishes as a participant.   |
| `compositor`    | custom (Node + Puppeteer)   | Picture-in-picture composition for recordings / livestreams.      |
| `whisper`       | custom (whisper.cpp)        | Self-hosted speech-to-text for completed recordings.              |
| `redis`         | `redis:7-alpine`            | Coordination + rate-limit buckets + email-OTP cache.              |
| _coturn_        | host install (not Dockered) | TURN/STUN. Optional; the SFU works without it but NAT'd users may not. |

## Traffic flow

```
                                 ┌──────────────────────────────────────────────────────────┐
                                 │                          Internet                          │
                                 └──────────────────────────────────────────────────────────┘
                                                            │
                                                            ▼  443/tcp, 80/tcp
                                                    ┌───────────────┐
                                                    │     Caddy     │   (path-based routing)
                                                    └───────────────┘
                              ┌──────────────────────────┼──────────────────────────────────┐
                              │                          │                                  │
              /api/*  /rec/*  ▼                          ▼ /rtc/*                          ▼ /* (SPA)
                       ┌─────────────┐             ┌──────────────┐                ┌─────────────┐
                       │ meeting-api │             │   livekit    │                │  frontend   │
                       │  (FastAPI)  │             │ (host netw.) │                │   (SPA)     │
                       └─────────────┘             └──────────────┘                └─────────────┘
                              │     ▲                    │     ▲
                              │     │ /v1/webhooks/livekit│     │  signed webhooks
                              │     └────────────────────┘     │
                              │                                │
                              │                                │  participant join/leave,
                              │                                │  egress_started/ended,
                              │                                │  ingress_ended events
                              │                                │
                              │                                │  (LiveKit → meeting-api,
                              │                                │   loopback only)
                              │
       ┌──────────────────────┼────────────────────────────────┬────────────────────────┬───────────────────────┐
       ▼                      ▼                                ▼                        ▼                       ▼
  ┌─────────┐         ┌──────────────────┐            ┌────────────────┐      ┌───────────────────┐    ┌────────────────┐
  │  Redis  │         │   SQLite file    │            │ LiveKit Egress │      │ LiveKit Ingress   │    │ whisper.cpp    │
  │         │         │ /var/lib/meet.db │            │ (record+stream)│      │ (URL_INPUT only)  │    │ (HTTP /inference)│
  └─────────┘         └──────────────────┘            └────────────────┘      └───────────────────┘    └────────────────┘
                                                              │                       │
                                                              ▼                       ▼
                                                   ┌──────────────────┐      reads playlist files
                                                   │ recordings (MP4) │      from /var/lib/meet
                                                   │ /var/lib/meet/   │      shared volume
                                                   │   recordings/    │
                                                   └──────────────────┘
                                                              │
                                                              ▼
                                                   ┌──────────────────┐
                                                   │ compositor       │
                                                   │ (Puppeteer page  │
                                                   │  joins room as a │
                                                   │  publisher when  │
                                                   │  PiP is enabled) │
                                                   └──────────────────┘
```

### Why LiveKit uses `network_mode: host`

LiveKit's WebRTC media range is 50000–60000/udp — 10,001 ports. Mapping each one with `-p 50000-60000:50000-60000/udp` is supported by Docker on paper but slow to start and brittle in practice. Host networking is the canonical workaround. The signaling path (`/rtc/*`) still flows through Caddy at 443, so clients only need a single TLS connection. The two consequences:

1. The other containers (which are on the bridge network) can't reach LiveKit by Docker DNS — they use `host.docker.internal` instead, declared in `extra_hosts:` on each side.
2. The host firewall must allow 7881/tcp (LiveKit ICE/TCP) and 50000-60000/udp.

### Why coturn lives outside the compose

The reference deployment is a Hetzner box that already runs `coturn` for the companion app `one.witysk.org`. Putting coturn inside this compose would have meant either evicting the running TURN server or running two TURN servers on the same machine, both of which break the existing call platform. Instead, `meet` uses coturn via **short-lived REST-API credentials** ([RFC 5766](https://datatracker.ietf.org/doc/html/rfc5766#section-2)):

- `meeting-api` and `coturn` share a `TURN_STATIC_AUTH_SECRET`.
- When a participant joins, `meeting-api` mints a `(username=expiry-epoch, password=HMAC-SHA1(secret, username))` pair valid for `TURN_TTL_SECONDS`.
- The frontend hands this pair to the LiveKit JS SDK, which feeds it to the browser's `RTCPeerConnection`.

This means the TURN server can run anywhere (same host, different host, different provider) — the only requirement is a shared HMAC secret.

## Request lifecycle: creating + joining a meeting

```
  ┌──────┐                    ┌───────────┐          ┌─────────────┐          ┌──────────┐
  │Owner │                    │ Frontend  │          │ meeting-api │          │ LiveKit  │
  │      │                    │   (SPA)   │          │             │          │   SFU    │
  └──┬───┘                    └─────┬─────┘          └──────┬──────┘          └────┬─────┘
     │ 1. POST /api/v1/meetings    │                       │                       │
     │ (title, schedule, policy)    │                       │                       │
     ├─────────────────────────────►│                       │                       │
     │                              │ JWT (Bearer)          │                       │
     │                              ├──────────────────────►│                       │
     │                              │                       │ insert Meeting row    │
     │                              │                       │ build room_name slug  │
     │                              │ {id, room_name, ...}  │                       │
     │                              │◄──────────────────────┤                       │
     │                              │                       │                       │
     │ 2. Click "Join"              │                       │                       │
     │                              │ POST /meetings/{id}/token                     │
     │                              ├──────────────────────►│                       │
     │                              │                       │ mint LiveKit JWT      │
     │                              │                       │ (room_admin=true)     │
     │                              │ {token, ws_url}       │                       │
     │                              │◄──────────────────────┤                       │
     │                              │                       │                       │
     │                              │ WSS /rtc + LiveKit token                      │
     │                              ├───────────────────────────────────────────────►│
     │                              │ (SDP offer / answer, ICE, DTLS-SRTP) ▼        │
     │                              │◄───────────────────────────────────────────────┤
     │                              │                       │                       │
     │                              │                       │      participant_joined webhook
     │                              │                       │◄──────────────────────┤
     │                              │                       │                       │
     │                              │                       │ insert MeetingParticipant row
```

### Anonymous join

A non-owner joining via a share link follows the same shape with two differences:

- Instead of `/meetings/{id}/token`, the SPA calls `/rooms/{room_name}/anon-token` with the display name (and optional password). meeting-api enforces the moderation policy: password check, name-required check, waiting-room admit step.
- The minted token has `room_admin=false` and respects `auto_mute_new_joiners` / `auto_disable_camera_for_new` from the meeting row.

### Waiting room

When `waiting_room_enabled=true` on the meeting, anonymous-token requests return a `wait_token` instead of a LiveKit token. The SPA polls `/rooms/{room_name}/wait/{wait_token}` every few seconds; meanwhile the host sees the pending joiner in the participants panel and clicks Admit. That admit call mints the real LiveKit token and the polling endpoint returns it on the next poll.

## Recording flow

```
 host clicks "Record"
        │
        ▼
 POST /api/v1/meetings/{id}/recordings:start (layout=speaker|grid|single-speaker|pip)
        │
        ▼                                 ┌──────────────────────────────────────────┐
  meeting-api                              │ egress_mgr unifies record + stream into  │
        │ calls LiveKit egress service     │ ONE RoomCompositeEgress so the 2-vCPU    │
        ▼                                  │ Hetzner box can only run one at a time.  │
  LiveKit Egress (headless Chrome)         │ Toggling record off when stream is on    │
        │ joins room                       │ restarts the egress with the same layout.│
        │ encodes to H.264 720p30 + AAC    └──────────────────────────────────────────┘
        ▼
  /var/lib/meet/recordings/<egress_id>.mp4
        │
        │  ◄──── egress_ended webhook ───── (status, file_size, duration)
        ▼
 meeting-api persists Recording row, then dispatches:
  • transcription.py (whisper.cpp) — fire-and-forget BackgroundTask
  • email.py (Resend) — when transcript is done, mail captured participants
```

## Video playback flow

```
 host uploads MP4 → POST /api/v1/meetings/{id}/playback/items (multipart)
       │
       ▼
 meeting-api saves to /var/lib/meet/playback/<meeting_id>/<item_id>.mp4
       │
       ▼
 host clicks Play
       │
       ▼
 POST /meetings/{id}/playback:start
       │
       ▼
 meeting-api calls LiveKit Ingress with URL_INPUT pointing at
 http://meeting-api:8080/v1/ingress/playback/{meeting_id}/{item_id}?sig=<hmac>
       │
       ▼
 LiveKit Ingress pulls the MP4 via HTTP, transcodes, publishes as a
 participant track named "playback-<meeting_id>". Every client in the room
 sees it as a regular participant.
       │
       │   ◄── ingress_ended webhook ── (clip finished playing)
       ▼
 meeting-api advances to next playlist item, or stops, or replays "What's up
 next" slide if the next item is over 5 minutes and the toggle is on.
```

A "freeze-frame" stream is built on pause — a 1-frame MP4 looped — so every client sees the same static image instead of relying on browser-side pause (which drifts per client). Resume rebuilds the original ingress at the right offset.

## Picture-in-picture (PiP) composition

PiP recordings/livestreams want a custom layout that LiveKit Egress's built-in templates don't ship. The trick: meeting-api spins up a dedicated headless-Chrome page that joins the room as a normal participant and **publishes** a composed screenshare track. Then LiveKit Egress records that screenshare with the standard "single-speaker" preset.

```
  host enables PiP
       │
       ▼
  POST compositor:8090/sessions/<room>
       │
       ▼
  compositor (Puppeteer) launches a Chromium page at
  https://meet.witysk.org/egress-layout/composite?room=<...>&token=<...>
       │
       ▼
  the page joins LiveKit as "composite-<room>", draws a canvas:
    • main source = active screenshare / playback / speaker (full-bleed)
    • corner overlay = chosen participant's camera (PiP)
       │
       ▼
  publishes canvas.captureStream() as a screenshare track
       │
       ▼
  LiveKit Egress records that screenshare → MP4 / RTMPS
```

The compositor restarts the page on crash and tears it down when meeting-api sends `DELETE /sessions/<room>`.

## Authentication & sessions

Two issuers, one secret, one algorithm:

1. **`one.witysk.org` SSO** — HS256 token with `sub = one.witysk.org user_id`. No `iss` claim (historical). On first sight, `meeting-api` auto-provisions a `User` row with `kind="sso"` and `external_id=sub`.
2. **Native** — HS256 token with `sub = meet user.id (int as str)` and `iss = "meet"`. Issued by `/v1/auth/login`. Native users go through Argon2-hashed password verification and optional TOTP / email-OTP 2FA.

Both share the `JWT_SECRET_KEY` — that's how a one.witysk.org token issued elsewhere can be accepted here. `auth.py` distinguishes them by checking for the `iss` claim.

### Authorization

- **Meeting creation** is gated by `User.is_admin_now(now)`. SSO is always admin; native is admin while *any* of `trial_started_at + 10 days > now`, `entitlement_expires_at > now`. Computed lazily — no cron required.
- **Moderation actions** are gated by being the owner, a co-host (`cohost_user_ids` JSON list on the meeting), or a platform admin.
- **Platform admin actions** (user management, IP blocks, IDS) are gated by `User.is_platform_admin`, bootstrapped from `PLATFORM_ADMIN_EMAILS` in `.env`.

## State

| Where                                          | What                                                                    |
| ---------------------------------------------- | ----------------------------------------------------------------------- |
| `/var/lib/meet/meet.db` (SQLite)               | Meetings, participants, recordings, users, chat, polls, whiteboard, IDS.|
| `/var/lib/meet/recordings/*.mp4` + `.txt`      | Recordings and transcripts.                                              |
| `/var/lib/meet/playback/<meeting_id>/*.mp4`    | Uploaded playlist videos.                                                |
| `/var/lib/meet/whats_next_cache/*.mp4`         | Cached "What's up next" rundown slides (keyed by content hash).          |
| `/var/lib/meet/branding/<meeting_id>.<ext>`    | Per-meeting branding image.                                              |
| `/var/lib/meet/chat-attachments/<sha>.<ext>`   | Chat image attachments (5 MB cap).                                       |
| `/var/lib/meet/facepics/<user_id>.<ext>`       | Native-user avatars (5 MB cap).                                          |
| `/var/log/meet/{app,requests,db}.log[.gz]`     | Rotated logs (daily, gzipped, 180-day retention).                        |
| Redis                                          | Rate-limit buckets, email-OTP codes, IDS sliding-window counters.        |

SQLite is the deliberate choice: this is a self-hostable single-box service, not a cluster. There's no migration tool — `db.py:lightweight_migrate()` runs `ALTER TABLE ADD COLUMN` for new fields at startup. Switching to Postgres requires changing `DATABASE_URL` and porting the migration helper.

## Logging & observability

Three rotated files via `logging_config.py`:

- `app.log` — application logs (`log_event("event_kind", k=v, ...)`)
- `requests.log` — one line per HTTP request (method, path, status, duration, IP, UA)
- `db.log` — SQLAlchemy noise, on its own file so it doesn't drown the others

Rotation is daily at midnight UTC, gzipped on rotation, 180-day retention. Files are bind-mounted at `/var/log/meet` on the host so they survive container restarts.

There is no Prometheus exporter today. The reference operator monitors via `tail -f` and `journalctl -u docker`.

## Scaling notes

This stack is sized for the reference deployment (2 vCPUs, 4 GB RAM, one meeting at a time recording). To grow:

- **More concurrent recordings** — bump `cpu_cost.room_composite_cpu_cost` in egress config back toward the default 4.0 and add CPU. Or run egress on a separate host.
- **More concurrent meetings** — LiveKit Server itself scales to thousands of participants on bigger hosts; the SQLite/SQLAlchemy layer is the next bottleneck. Switching to Postgres lifts that. The frontend, Caddy, Redis, and compositor are all stateless or near-stateless.
- **Geographic redundancy** — run a LiveKit cluster with the [LiveKit Cloud routing](https://docs.livekit.io/realtime/concepts/geo-routing/) recipe, or stand up regional LiveKit Server instances and route via Caddy / DNS.
- **High-volume transcription** — swap `whisper.cpp` for `WHISPER_URL` pointing at an external larger-model host (the same OpenAI-compatible `/inference` API).

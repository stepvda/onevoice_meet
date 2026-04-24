# meet.witysk.org

Many-to-many video conferencing service for the TI One Voice platform, co-located on the Hetzner host that runs `turn.witysk.org`.

Target: up to 50 participants per room, with audio/video/screenshare/virtual backgrounds, moderator controls, and server-side recording.

## Stack

- **SFU:** LiveKit Server (Apache 2.0)
- **Recording:** LiveKit Egress (composite MP4)
- **Control plane:** FastAPI (Python 3.12) — `meeting-api`
- **Frontend:** React 18 + Vite + TypeScript + `@livekit/components-react`
- **TLS / reverse proxy:** Caddy 2
- **Coordination:** Redis 7
- **Storage:** local filesystem at `/var/lib/meet/recordings` (30-day retention)

## Auth integration with `one.witysk.org`

**Important divergence from the original spec:** The spec assumes DPoP JWTs with an RS256 JWKS endpoint. The existing `one.witysk.org` issues **HS256 shared-secret JWTs** (no DPoP, no JWKS).

This project matches the existing reality:

- `meeting-api` validates HS256 JWTs using the same `SECRET_KEY` env var as the onevoice backend.
- Tokens carry `sub` (user id) and `session` claims, matching `onevoice/app/utils/security.py`.
- Set `JWT_SECRET_KEY` in `.env` to the same value used by one.witysk.org.

Upgrading to DPoP + JWKS later is possible (code paths are isolated in `app/auth.py`), but requires one.witysk.org to publish a JWKS endpoint first.

## Layout

```
/opt/meet/                        ← production install path
├── docker-compose.yml
├── .env                          ← never commit; copy from .env.example
├── caddy/Caddyfile
├── livekit/livekit.yaml
├── livekit/egress.yaml
├── meeting-api/                  ← FastAPI control plane
└── frontend/                     ← React SPA
```

## Quick start (local dev)

```bash
cp .env.example .env
# Generate LiveKit keys and paste them into .env:
docker run --rm livekit/livekit-server generate-keys
# Set JWT_SECRET_KEY to match one.witysk.org's SECRET_KEY
docker compose up -d
```

Visit `http://localhost` (Caddy binds 80/443). For real TLS certs you need DNS pointing at the host and port 80 reachable from the internet.

## Production deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for SSH-based deployment to `turn.witysk.org`.

## Acceptance testing

See §18 of the spec. Tests live in:

- `meeting-api/tests/` — pytest + FastAPI `TestClient`
- `frontend/e2e/` — Playwright (to be added)

## Implementation status

Per §17 of the spec:

- [x] Phase 1: Infrastructure bring-up (compose, Caddy, LiveKit, Redis)
- [x] Phase 2: meeting-api skeleton
- [x] Phase 3: JWT validation (HS256 variant — see auth note above)
- [x] Phase 4: Meeting CRUD + anonymous join
- [x] Phase 5: Frontend scaffold
- [x] Phase 6: Prebuilt controls (`@livekit/components-react`)
- [x] Phase 7: Moderator actions — mute/kick/presenter wired to LiveKit server API
- [x] Phase 8: Presenter spotlight layout (reads `room.metadata.presenter_identity`)
- [x] Phase 9: Virtual backgrounds (`@livekit/track-processors`)
- [x] Phase 10: Recording (egress start/stop, webhook → DB, download)
- [x] Phase 11: Retention job (APScheduler, daily 03:00 UTC)
- [x] Phase 12: Hardening — CSP, rate limits, audit log, short-lived TURN creds
- [ ] Phase 13: Acceptance testing — see §18 of spec

Phase 13 requires a live deployment to exercise end-to-end (real browsers,
real TURN fallback, real egress). Local pytest covers the API surface
(6 tests passing); Playwright suite is the remaining work.

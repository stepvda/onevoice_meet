# Development

This doc covers the local-dev loop: how to run the services standalone (so you get hot reload), where tests live, and how the components interact during development.

## Prerequisites

- Docker + Docker Compose v2
- Python 3.12 (for running `meeting-api` outside Docker)
- Node 20+ (for the frontend dev server)
- A LiveKit API key/secret pair (`docker run --rm livekit/livekit-server generate-keys`)

## Run everything in Docker (easiest)

```bash
cp .env.example .env
# fill in LIVEKIT_API_KEY / LIVEKIT_API_SECRET / JWT_SECRET_KEY
docker compose up -d
# visit http://localhost
```

Caveats:

- The frontend is built once at boot by the `frontend-build` one-shot. Code changes to the SPA require `docker compose restart frontend-build` (or `docker compose up -d --build frontend-build`) to rebuild.
- The meeting-api uses uvicorn's default reloader inside the container if you set `UVICORN_RELOAD=true` and bind-mount the source dir.

## Run services standalone (better for the inner loop)

The fastest dev loop runs Docker for the **infrastructure** (LiveKit, Redis, Egress, Ingress, Whisper, Compositor, Caddy) and **native** for the bits you're actively iterating on.

### Frontend dev server

```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

Vite proxies aren't configured by default, so the SPA hits whatever URL `VITE_API_BASE` resolves to — in dev, the SPA is served on `:5173` and calls `/api/*` which Caddy needs to proxy. Either:

- **Front the whole thing through Caddy** by replacing the `frontend-build` volume in `docker-compose.yml` with a `reverse_proxy host.docker.internal:5173` route in `caddy/Caddyfile`.
- Or **point the SPA directly at the API origin** by hard-coding `https://localhost/api` in `frontend/src/lib/api.ts` for the duration of a dev session.

CORS is already configured to allow `http://localhost:5173` (see [`meeting-api/app/main.py`](../meeting-api/app/main.py)).

### meeting-api standalone

```bash
cd meeting-api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# meeting-api needs Redis + LiveKit reachable; easiest is to keep Docker
# running for those services and bind to localhost:
export DATABASE_URL=sqlite:///./meet-dev.db
export REDIS_URL=redis://localhost:6379/0
export LIVEKIT_SERVER_URL=http://localhost:7880
export $(grep -v '^#' ../.env | xargs)   # pick up LIVEKIT_API_KEY etc.
uvicorn app.main:app --reload --port 8080
```

Hot reload kicks in on `.py` saves. The DB schema is created at startup via `Base.metadata.create_all`; the `lightweight_migrate()` helper applies new `ALTER TABLE`s for fields added since the last boot.

### LiveKit standalone

For tighter iteration on signaling / webhook behavior:

```bash
docker run --rm -it \
  --network host \
  -v "$PWD/livekit/livekit.yaml.tpl:/etc/livekit.yaml.tpl:ro" \
  -e LIVEKIT_API_KEY -e LIVEKIT_API_SECRET -e LIVEKIT_WEBHOOK_KEY \
  livekit/livekit-server \
  sh -c 'sed -e "s|__API_KEY__|$LIVEKIT_API_KEY|" -e "s|__API_SECRET__|$LIVEKIT_API_SECRET|" -e "s|__WEBHOOK_KEY__|$LIVEKIT_WEBHOOK_KEY|" /etc/livekit.yaml.tpl > /tmp/livekit.yaml && exec /livekit-server --config /tmp/livekit.yaml'
```

## Tests

### Backend

```bash
cd meeting-api
pytest
```

Tests use FastAPI's `TestClient` (no Docker dependency). The fixture creates a fresh SQLite file per test session. Currently 6 tests cover the meeting-api surface (auth, meeting CRUD, anonymous join, token minting, recording webhook fan-out, voucher redemption).

### Frontend

There is no Playwright suite yet — it's listed in the [README status section](../README.md#status) as the remaining piece for full acceptance testing. Contributions welcome ([CONTRIBUTING.md](../CONTRIBUTING.md)).

The TypeScript build itself does typechecking:

```bash
cd frontend
npm run lint       # tsc --noEmit
npm run build      # tsc --noEmit && vite build
```

## Project layout

```
.
├── caddy/                  # TLS + reverse proxy (Caddyfile)
├── compositor/             # Headless-Chrome PiP composer (Node + Puppeteer)
├── frontend/               # React SPA (Vite + TypeScript + Tailwind)
│   ├── src/
│   │   ├── App.tsx         # Top-level router
│   │   ├── routes/         # Page components, one per URL
│   │   ├── components/     # Reusable UI
│   │   ├── lib/            # Domain logic (auth, livekit, prefs, i18n)
│   │   └── styles/         # Tailwind config + global CSS
│   └── public/             # Static assets
├── livekit/                # LiveKit Server config template
├── meeting-api/            # FastAPI control plane (Python 3.12)
│   ├── app/
│   │   ├── main.py         # FastAPI app factory + middleware
│   │   ├── auth.py         # JWT verification + RequireUser/RequireAdmin deps
│   │   ├── config.py       # Pydantic Settings (reads .env)
│   │   ├── db.py           # SQLAlchemy engine + lightweight migrations
│   │   ├── models.py       # ORM models for every table
│   │   ├── livekit_client.py # LiveKit Server API wrapper + token minting
│   │   ├── logging_config.py # Three rotating log files
│   │   ├── scheduler.py    # APScheduler background jobs
│   │   ├── webhooks.py     # LiveKit webhook receiver
│   │   ├── routes/         # FastAPI routers, one per area
│   │   └── services/       # Cross-route domain logic
│   └── tests/              # pytest + FastAPI TestClient
├── one-witysk-integration/ # SSO bootstrap iframe (deployed to upstream issuer)
├── redis/                  # (volume mount placeholder)
├── scripts/                # Operational helpers (deploy, i18n, YouTube OAuth)
├── whisper/                # whisper.cpp transcription Dockerfile
├── docker-compose.yml      # All eight services
├── .env.example            # Template — copy to .env, never commit
└── docs/                   # The documentation you're reading
```

## Code style

- **Python** — 4-space indent, `pep8`-ish, no Black runs configured. Imports sorted by section (stdlib → third-party → app).
- **TypeScript** — Strict mode on. Functional components, hooks, Zustand for cross-component state, TanStack-style fetch helpers in `lib/api.ts`.
- **Comments** — Explain *why*, not what. The codebase already follows this convention; bug-fix PRs that add explanatory comments for non-obvious invariants are welcome.

## Useful one-liners

```bash
# Watch the API logs in dev
docker compose logs -f meeting-api

# Tail the rotating logs (production-style)
docker compose exec meeting-api tail -f /var/log/meet/app.log

# Run a one-off DB shell
docker compose exec meeting-api python -c "
from app.db import engine
from sqlalchemy import text
print(engine.connect().execute(text('select count(*) from meetings')).scalar())
"

# Generate a YouTube refresh token (one-time)
python scripts/youtube_oauth.py

# Find missing i18n keys
python scripts/translate_missing_keys.py
```

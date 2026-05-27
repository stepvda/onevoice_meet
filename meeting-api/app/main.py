import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import scheduler
from app.auth import bootstrap_platform_admins
from app.config import settings
from app.db import engine, lightweight_migrate
from app.logging_config import log_event, setup_logging
from app.models import Base
from app.routes import admin, auth_native, billing, chat, health, meetings, moderation, playback, polls, recordings, streams, ti_cafe, tokens, totp, users, vouchers, waiting_room, youtube_oauth
from app.services import ip_block
from app.webhooks import router as webhook_router

# Configure file-backed app/requests/db logs (rotates daily, gzips on
# rotation, keeps 6 months). Called at import time so the very first log
# line from any subsequent import lands in the right file.
setup_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    lightweight_migrate()
    bootstrap_platform_admins()
    ip_block.reload()
    scheduler.start()
    try:
        yield
    finally:
        # Persist any hits accumulated since the last admin-panel fetch so
        # block_count survives restarts.
        ip_block.flush_hits()
        scheduler.stop()


app = FastAPI(
    title="meet.witysk.org meeting-api",
    version="0.1.0",
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

# CORS: same-origin in production for meet.witysk.org (Caddy serves frontend
# + proxies /api), plus the OneVoice app at one.witysk.org which integrates
# the Café widget directly in the browser (calls /api/v1/ti-cafe/token and
# /api/v1/ti-cafe/live with the shared-secret JWT). localhost:5173 is the
# Vite dev origin used by both frontends.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.public_url,
        "https://one.witysk.org",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
# Per-request access log → /var/log/meet/requests.log. One line per
# request with method, path, status, duration, client IP, and a truncated
# user-agent. Registered BEFORE the IP block middleware so a 403 from
# IPBlockMiddleware still appears in the access log (since later-added
# Starlette middleware wraps earlier-added ones).
@app.middleware("http")
async def _request_log_mw(request: Request, call_next):
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        ms = (time.perf_counter() - start) * 1000.0
        client = request.client.host if request.client else "-"
        ua = request.headers.get("user-agent", "-")
        if len(ua) > 120:
            ua = ua[:120] + "…"
        log_event(
            "http",
            m=request.method,
            p=request.url.path,
            s=status_code,
            ms=f"{ms:.1f}",
            ip=client,
            ua=f'"{ua}"',
        )


# IP blocking runs OUTERMOST: blocked addresses get a 403 before any
# request body, auth, or DB call happens. Adding it last makes Starlette
# wrap it around CORS, so it executes first on incoming requests but
# CORS preflight responses still get the right headers when allowed.
app.add_middleware(ip_block.IPBlockMiddleware)

app.include_router(health.router, prefix="/api")
app.include_router(meetings.router, prefix="/api")
app.include_router(tokens.router, prefix="/api")
app.include_router(moderation.router, prefix="/api")
app.include_router(recordings.router, prefix="/api")
app.include_router(streams.router, prefix="/api")
app.include_router(playback.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(ti_cafe.router, prefix="/api")
app.include_router(auth_native.router, prefix="/api")
app.include_router(totp.router, prefix="/api")
app.include_router(vouchers.router, prefix="/api")
app.include_router(billing.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(waiting_room.router, prefix="/api")
app.include_router(polls.router, prefix="/api")
app.include_router(youtube_oauth.router, prefix="/api")
app.include_router(webhook_router, prefix="/api")

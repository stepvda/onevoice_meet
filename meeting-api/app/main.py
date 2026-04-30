from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import scheduler
from app.config import settings
from app.db import engine, lightweight_migrate
from app.models import Base
from app.routes import auth_native, billing, chat, health, meetings, moderation, recordings, ti_cafe, tokens, totp, users, vouchers
from app.webhooks import router as webhook_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    lightweight_migrate()
    scheduler.start()
    try:
        yield
    finally:
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

app.include_router(health.router, prefix="/api")
app.include_router(meetings.router, prefix="/api")
app.include_router(tokens.router, prefix="/api")
app.include_router(moderation.router, prefix="/api")
app.include_router(recordings.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(ti_cafe.router, prefix="/api")
app.include_router(auth_native.router, prefix="/api")
app.include_router(totp.router, prefix="/api")
app.include_router(vouchers.router, prefix="/api")
app.include_router(billing.router, prefix="/api")
app.include_router(webhook_router, prefix="/api")

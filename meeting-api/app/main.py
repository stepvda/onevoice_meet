from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import scheduler
from app.config import settings
from app.db import engine
from app.models import Base
from app.routes import health, meetings, moderation, recordings, tokens
from app.webhooks import router as webhook_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
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

# CORS: same-origin in production (Caddy serves frontend + proxies /api on
# meet.witysk.org), so allow_origins is only relevant for local dev where
# Vite runs on :5173.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_url, "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health.router, prefix="/api")
app.include_router(meetings.router, prefix="/api")
app.include_router(tokens.router, prefix="/api")
app.include_router(moderation.router, prefix="/api")
app.include_router(recordings.router, prefix="/api")
app.include_router(webhook_router, prefix="/api")

"""meetpp-agent HTTP API (contract §6.1). Docker-internal, no auth.

  POST   /sessions                 start a session (joins the room)
  PATCH  /sessions/{sid}           accepted_identities / paused / glossary
  POST   /sessions/{sid}/finalize  stop capture, drain, tier-2 final pass
  DELETE /sessions/{sid}           leave the room (no final pass)
  POST   /tts                      announcement clip via tier 2 (cached)
  GET    /health
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import config
from .metrics import r
from .poster import InternalApi
from .session import AgentSession, Services
from .stt import STTEngine
from .tier2 import Tier2Client
from .tts import DEFAULT_VOICE, TTSCache, TTSUnavailable
from .vad import load_vad
from .worker import STTWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("meetpp.agent")


class State:
    services: Services | None = None
    engine: STTEngine | None = None
    worker: STTWorker | None = None
    http: httpx.AsyncClient | None = None
    tts: TTSCache | None = None
    sessions: dict[str, AgentSession] = {}
    closing: set[asyncio.Task] = set()
    load_task: asyncio.Task | None = None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    vad = load_vad(config.SILERO_VAD_MODEL)
    engine = STTEngine()
    worker = STTWorker(engine, dispatch=loop.call_soon_threadsafe)
    worker.start()
    # Model loading is slow and blocking: keep it off the event loop.
    state.load_task = asyncio.create_task(asyncio.to_thread(engine.load))
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), limits=httpx.Limits(max_connections=20))
    tier2 = None
    if config.SPEECH_URL:
        tier2 = Tier2Client(config.SPEECH_URL, config.SPEECH_SECRET, http)
        tier2.start()
    if not config.INTERNAL_SECRET:
        log.error("MEETPP_INTERNAL_SECRET is not set: meeting-api will reject every post")
    state.engine, state.worker, state.http = engine, worker, http
    state.services = Services(
        api=InternalApi(http, config.MEETING_API_URL, config.INTERNAL_SECRET),
        worker=worker,
        vad=vad,
        tier2=tier2,
        data_dir=config.DATA_DIR,
    )
    state.tts = TTSCache(config.DATA_DIR, tier2)
    log.info(
        "meetpp-agent up: stt=%s/%s threads=%d vad=%s tier2=%s data_dir=%s",
        engine.primary,
        engine.degrade,
        engine.threads,
        vad.kind,
        config.SPEECH_URL or "off",
        config.DATA_DIR,
    )
    try:
        yield
    finally:
        for s in list(state.sessions.values()):
            try:
                await asyncio.wait_for(s.close(), 30)
            except Exception:  # noqa: BLE001
                pass
        if tier2 is not None:
            await tier2.stop()
        worker.stop()
        await http.aclose()


app = FastAPI(title="meetpp-agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class StartBody(BaseModel):
    session_id: str
    room: str
    ws_url: str
    token: str
    language: str = "en"
    glossary: str = ""
    accepted_identities: list[str] = Field(default_factory=list)
    paused: bool = False


class PatchBody(BaseModel):
    accepted_identities: list[str] | None = None
    paused: bool | None = None
    glossary: str | None = None


class TtsBody(BaseModel):
    text: str
    voice: str = DEFAULT_VOICE


def _get(sid: str) -> AgentSession:
    s = state.sessions.get(sid)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s


@app.post("/sessions", status_code=201)
async def start_session(body: StartBody) -> dict:
    if body.session_id in state.sessions:
        raise HTTPException(status_code=409, detail="session already exists")
    if body.language and body.language != config.LANGUAGE:
        log.warning("session %s asked for language=%s; Release 1.1 is English only", body.session_id, body.language)
    session = AgentSession(
        body.session_id,
        body.room,
        body.ws_url,
        body.token,
        services=state.services,
        glossary=body.glossary,
        accepted=body.accepted_identities,
        paused=body.paused,
    )
    state.sessions[body.session_id] = session  # reserve against a concurrent duplicate
    try:
        await asyncio.wait_for(session.start(), 20)
    except Exception as exc:  # noqa: BLE001
        state.sessions.pop(body.session_id, None)
        log.exception("MEETPP_SESSION sid=%s start failed", body.session_id)
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=502, detail=f"could not join room: {exc}") from exc
    return {"ok": True, "session_id": body.session_id}


@app.patch("/sessions/{sid}")
async def patch_session(sid: str, body: PatchBody) -> dict:
    s = _get(sid)
    if body.glossary is not None:
        s.set_glossary(body.glossary)
    if body.paused is not None:
        s.set_paused(body.paused)
    if body.accepted_identities is not None:
        s.set_accepted(body.accepted_identities)
    return {"ok": True}


@app.post("/sessions/{sid}/finalize", status_code=202)
async def finalize_session(sid: str) -> dict:
    s = _get(sid)
    return {"ok": True, "final_pass": s.finalize()}


@app.delete("/sessions/{sid}", status_code=204, response_class=Response)
async def delete_session(sid: str) -> Response:
    s = state.sessions.pop(sid, None)
    if s is not None:
        task = asyncio.create_task(s.close())
        state.closing.add(task)
        task.add_done_callback(state.closing.discard)
    return Response(status_code=204)


@app.post("/tts")
async def tts(body: TtsBody):
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="empty text")
    try:
        return await state.tts.get(body.text, body.voice or DEFAULT_VOICE)
    except TTSUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": f"tts unavailable: {exc}"})


@app.get("/health")
async def health() -> dict:
    engine, worker, services = state.engine, state.worker, state.services
    tier2 = services.tier2 if services else None
    return {
        "ok": bool(engine and engine.ready.is_set()),
        "sessions": [s.health() for s in state.sessions.values()],
        "model": engine.model_for(worker.degraded) if engine and worker else config.STT_MODEL,
        "models_loaded": engine.loaded if engine else [],
        "model_error": engine.load_error if engine else None,
        "degraded": bool(worker and worker.degraded),
        "vad": services.vad.kind if services else "energy",
        "rtf_p50": r(services.rtf.percentile(50)) if services else None,
        "backlog_s": round(worker.backlog_s(), 1) if worker else 0.0,
        "tier2": "off" if tier2 is None else tier2.state,
    }

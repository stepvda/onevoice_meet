"""meetpp-agent session API, TTS and health.

Mirrors the compositor's session API so meeting-api drives it the same way.
Internal only; never published to the host.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .session import AgentSession, SessionConfig
from .stt import STTEngine
from .tts import PiperTTS, parse_voices

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("meetpp.agent")

app = FastAPI(title="meetpp-agent", docs_url=None, redoc_url=None, openapi_url=None)

_sessions: dict[str, AgentSession] = {}

STT_MODEL = os.environ.get("STT_MODEL", "small")
STT_DEGRADE_MODEL = os.environ.get("STT_DEGRADE_MODEL", "base")
STT_COMPUTE_TYPE = os.environ.get("STT_COMPUTE_TYPE", "int8")
STT_THREADS = int(os.environ.get("STT_THREADS", "2"))
TTS_VOICES = parse_voices(os.environ.get("TTS_VOICES", ""))
TTS_CACHE = os.environ.get("TTS_CACHE_DIR", "/var/lib/meet/meetpp/tts")

_stt = STTEngine(STT_MODEL, STT_DEGRADE_MODEL, STT_COMPUTE_TYPE, STT_THREADS)
_tts = PiperTTS(TTS_VOICES, TTS_CACHE)


class StartBody(BaseModel):
    session_id: str
    room: str
    ws_url: str
    token: str
    language: str = "en"
    stt_model: str | None = None
    accepted: list[str] = Field(default_factory=list)
    opted_out: list[str] = Field(default_factory=list)
    vocabulary: list[str] = Field(default_factory=list)


class PatchBody(BaseModel):
    accepted: list[str] | None = None
    opted_out: list[str] | None = None
    language: str | None = None
    paused: bool | None = None


class TtsBody(BaseModel):
    text: str
    lang: str = "en"
    voice: str | None = None


@app.post("/sessions", status_code=201)
async def start_session(body: StartBody) -> dict:
    if body.session_id in _sessions:
        raise HTTPException(status_code=409, detail="session already exists")
    cfg = SessionConfig(
        session_id=body.session_id,
        room=body.room,
        ws_url=body.ws_url,
        token=body.token,
        language=body.language,
        stt_model=body.stt_model or STT_MODEL,
        accepted=body.accepted,
        opted_out=body.opted_out,
        vocabulary=body.vocabulary,
    )
    session = AgentSession(cfg, _stt, _tts)
    try:
        await session.start()
    except Exception as exc:  # noqa: BLE001
        log.exception("session start failed")
        raise HTTPException(status_code=502, detail=f"could not join room: {exc}") from exc
    _sessions[body.session_id] = session
    return {"ok": True, "session_id": body.session_id}


@app.patch("/sessions/{session_id}")
async def patch_session(session_id: str, body: PatchBody) -> dict:
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if body.accepted is not None:
        session.set_accepted(body.accepted)
    if body.opted_out is not None:
        session.set_opted_out(body.opted_out)
    if body.paused is not None:
        session.set_paused(body.paused)
    if body.language:
        session.cfg.language = body.language
    return {"ok": True}


@app.delete("/sessions/{session_id}", status_code=204, response_model=None)
async def delete_session(session_id: str):
    session = _sessions.pop(session_id, None)
    if session is None:
        return
    await session.stop()


@app.get("/health")
async def health() -> dict:
    backlogs = [s.backlog_s for s in _sessions.values()]
    cpu = 0.0
    try:
        cpu = os.getloadavg()[0]
    except Exception:  # noqa: BLE001
        pass
    return {
        "sessions": len(_sessions),
        "backlog_s": max(backlogs) if backlogs else 0.0,
        "model": STT_MODEL,
        "rtf_p50": _stt.rtf,
        "cpu": cpu,
        "sessions_detail": [s.health() for s in _sessions.values()],
    }


@app.post("/tts")
async def tts(body: TtsBody) -> dict:
    result = _tts.synthesize(body.text, body.lang)
    if result is None:
        raise HTTPException(status_code=503, detail="tts unavailable")
    return result

"""FastAPI app: POST /transcribe, POST /tts, GET /health (contract section 6.2)."""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .audio import TARGET_SR, AudioDecodeError, decode_to_16k_mono, encode, resample
from .auth import SIG_HEADER, TS_HEADER, check_timestamp, verify
from .config import Settings
from .repetition import detect_repetition
from .stt import Transcriber
from .tts import DEFAULT_VOICE, Synthesizer, cap_text

log = logging.getLogger("meetpp_speech")

WARMUP_TEXT = (
    "Good evening. The board approved the budget for the community hall, "
    "and the treasurer will report at the next meeting."
)
MIN_AUDIO_S = 0.1  # shorter than this is answered without running the model


class Busy(Exception):
    pass


class SttGate:
    """At most `max_parallel` transcriptions run; at most `queue_max_s` seconds of
    audio may wait for a slot. Single event loop, so no lock is needed: there is
    no await between the admission check and the counter updates."""

    def __init__(self, max_parallel: int, queue_max_s: float):
        self.max_parallel = max_parallel
        self.queue_max_ms = int(queue_max_s * 1000)
        self.sem = asyncio.Semaphore(max_parallel)
        self.running = 0
        self.waiting = 0
        self.waiting_ms = 0

    @asynccontextmanager
    async def slot(self, duration_s: float):
        dur_ms = int(duration_s * 1000)
        must_wait = self.running + self.waiting >= self.max_parallel
        if must_wait and self.waiting_ms + dur_ms > self.queue_max_ms:
            raise Busy()
        self.waiting += 1
        self.waiting_ms += dur_ms
        try:
            await self.sem.acquire()
        finally:
            self.waiting -= 1
            self.waiting_ms -= dur_ms
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self.sem.release()


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    voice: str = DEFAULT_VOICE
    format: Literal["ogg", "opus", "wav"] = "ogg"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class Service:
    def __init__(self, settings: Settings, transcriber: Any = None, synthesizer: Any = None):
        self.settings = settings
        self.stt = transcriber or Transcriber(settings.model, settings.mlx_cache_mb)
        if synthesizer is None:
            synthesizer = Synthesizer(settings.kokoro_model, settings.kokoro_voices, settings.kokoro_threads)
        self.tts = synthesizer
        self.gate = SttGate(settings.max_parallel, settings.queue_max_s)
        self.tts_sem = asyncio.Semaphore(settings.tts_parallel)
        self.tts_running = 0
        self.tts_waiting = 0
        self.stt_pool = ThreadPoolExecutor(settings.max_parallel, thread_name_prefix="stt")
        self.tts_pool = ThreadPoolExecutor(settings.tts_parallel, thread_name_prefix="tts")
        self.rtfs: deque[float] = deque(maxlen=100)
        self.started = time.time()
        self.warmup_s: float | None = None

    # ------------------------------------------------------------------ startup
    def warm_up(self) -> None:
        """Load both models and run the STT model twice (compile, then measure)."""
        sample: np.ndarray | None = None
        if not getattr(self.tts, "loaded", False):
            if self.tts.available():
                try:
                    dt = self.tts.load()
                    log.info("Kokoro loaded in %.2fs (%d voices)", dt, len(self.tts.voices))
                except Exception:
                    log.exception("Kokoro failed to load; /tts disabled")
            else:
                log.warning(
                    "Kokoro model files missing (%s, %s); /tts disabled. Run install.sh.",
                    getattr(self.tts, "model_path", "?"), getattr(self.tts, "voices_path", "?"),
                )
        if getattr(self.tts, "loaded", False):
            try:
                t = time.perf_counter()
                pcm, sr = self.tts.synthesize(WARMUP_TEXT, DEFAULT_VOICE)
                log.info("Kokoro warm-up: %.1fs of audio in %.2fs", len(pcm) / sr, time.perf_counter() - t)
                sample = resample(pcm, sr, TARGET_SR)
            except Exception:
                log.exception("Kokoro warm-up failed; /tts disabled")
                self.tts.loaded = False
        if sample is None:
            sample = (0.01 * np.random.default_rng(0).standard_normal(3 * TARGET_SR)).astype(np.float32)

        if getattr(self.stt, "loaded", False):
            return
        t0 = time.perf_counter()
        load_s = self.stt.load()
        t1 = time.perf_counter()
        first = self.stt.transcribe(sample, "en", None)
        t2 = time.perf_counter()
        self.stt.transcribe(sample, "en", None)
        t3 = time.perf_counter()
        dur = len(sample) / TARGET_SR
        self.warmup_s = round(t2 - t0, 2)
        self.rtfs.append((t3 - t2) / dur)
        log.info(
            "Whisper %s ready: load %.2fs, first inference %.2fs (warm-up %.2fs), "
            "warm RTF %.3f on %.1fs; warm-up text ok=%s",
            self.settings.model_name, load_s, t2 - t1, t2 - t0, (t3 - t2) / dur, dur,
            bool(first.get("text")),
        )

    def shutdown(self) -> None:
        self.stt_pool.shutdown(wait=False, cancel_futures=True)
        self.tts_pool.shutdown(wait=False, cancel_futures=True)

    def rtf_p50(self) -> float | None:
        return round(statistics.median(self.rtfs), 3) if self.rtfs else None


async def read_body(request: Request, limit: int) -> bytes:
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > limit:
        raise HTTPException(413, "body too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "body too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def authenticated_body(request: Request, settings: Settings, limit: int) -> bytes:
    peer = request.client.host if request.client else "?"
    # Cheap timestamp check before reading the body.
    reason = check_timestamp(request.headers.get(TS_HEADER), settings.clock_skew_s)
    if reason is None:
        body = await read_body(request, limit)
        reason = verify(
            settings.secret, request.headers.get(TS_HEADER), request.headers.get(SIG_HEADER),
            body, settings.clock_skew_s,
        )
        if reason is None:
            return body
    log.warning("auth rejected %s from %s: %s", request.url.path, peer, reason)
    raise HTTPException(401, reason)


def create_app(settings: Settings, transcriber: Any = None, synthesizer: Any = None) -> FastAPI:
    svc_holder: dict[str, Service] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = Service(settings, transcriber, synthesizer)
        await asyncio.to_thread(svc.warm_up)
        app.state.svc = svc
        svc_holder["svc"] = svc
        yield
        svc.shutdown()

    app = FastAPI(title="meetpp-speech", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        t = time.perf_counter()
        response = await call_next(request)
        # Path only: the query string carries the prompt (meeting text).
        if not (request.url.path == "/health" and response.status_code == 200):
            log.info("%s %s %d %.3fs", request.method, request.url.path,
                     response.status_code, time.perf_counter() - t)
        return response

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        svc: Service = request.app.state.svc
        out: dict[str, Any] = {
            "ok": bool(getattr(svc.stt, "loaded", False)),
            "model": settings.model_name,
            "tts": "kokoro" if getattr(svc.tts, "loaded", False) else "off",
            "queue": svc.gate.waiting,
            "queue_s": round(svc.gate.waiting_ms / 1000, 1),
            "busy": svc.gate.running,
            "max_parallel": settings.max_parallel,
            "rtf_p50": svc.rtf_p50(),
            "tts_busy": svc.tts_running,
            "warmup_s": svc.warmup_s,
            "uptime_s": int(time.time() - svc.started),
            "version": __version__,
        }
        mem = getattr(svc.stt, "memory_mb", None)
        if callable(mem) and out["ok"]:
            try:
                out["mlx_mem"] = mem()
            except Exception:
                pass
        return out

    @app.post("/transcribe")
    async def transcribe(
        request: Request,
        language: str = Query("en", max_length=8),
        prompt: str | None = Query(None, max_length=8000),
    ) -> dict[str, Any]:
        svc: Service = request.app.state.svc
        body = await authenticated_body(request, settings, settings.max_body_bytes)
        language = language.strip().lower() or "en"
        if hasattr(svc.stt, "supports_language") and not svc.stt.supports_language(language):
            raise HTTPException(422, f"unsupported language {language!r}")
        if prompt:
            prompt = prompt.strip()
            # Whisper keeps the end of a long prompt; do the same (contract: <= 600 chars).
            prompt = prompt[-settings.prompt_max_chars:] or None
        try:
            audio = await asyncio.to_thread(decode_to_16k_mono, body)
        except AudioDecodeError as exc:
            raise HTTPException(415, str(exc)) from None
        duration = len(audio) / TARGET_SR
        if duration > settings.max_audio_s:
            raise HTTPException(413, f"audio longer than {settings.max_audio_s:.0f}s")
        if duration < MIN_AUDIO_S:
            return {"text": "", "avg_logprob": None, "duration_s": round(duration, 3), "rtf": 0.0,
                    "model": settings.model_name, "repetition": False}

        t_queued = time.perf_counter()
        try:
            async with svc.gate.slot(duration):
                t_start = time.perf_counter()
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(svc.stt_pool, svc.stt.transcribe, audio, language, prompt)
                elapsed = time.perf_counter() - t_start
        except Busy:
            log.warning("transcribe rejected: queue full (%d waiting, %.1fs audio)",
                        svc.gate.waiting, svc.gate.waiting_ms / 1000)
            raise HTTPException(503, "queue full", headers={"Retry-After": "2"}) from None
        except HTTPException:
            raise
        except Exception:
            log.exception("transcription failed (%.1fs audio)", duration)
            raise HTTPException(500, "transcription failed") from None

        rtf = elapsed / duration
        if duration >= 1.0:
            svc.rtfs.append(rtf)
        text = result["text"]
        out: dict[str, Any] = {
            "text": text,
            "avg_logprob": result.get("avg_logprob"),
            "duration_s": round(duration, 3),
            "rtf": round(rtf, 3),
            "model": settings.model_name,
            "repetition": False,
            "no_speech_prob": result.get("no_speech_prob"),
            "compression_ratio": result.get("compression_ratio"),
            "temperature": result.get("temperature"),
            "wait_s": round(t_start - t_queued, 3),
        }
        rep = detect_repetition(text)
        if rep.found:
            out.update(text=rep.text, repetition=True, text_raw=text,
                       repetition_ngram=rep.ngram, repetition_count=rep.repeats)
            log.warning("repetition loop: %d x %d-word n-gram in %.1fs utterance",
                        rep.repeats, len(rep.ngram.split()), duration)
        return out

    @app.post("/tts")
    async def tts(request: Request) -> Response:
        svc: Service = request.app.state.svc
        body = await authenticated_body(request, settings, 64 * 1024)
        try:
            req = TTSRequest.model_validate_json(body)
        except ValidationError as exc:
            return JSONResponse({"detail": exc.errors(include_url=False, include_context=False,
                                                       include_input=False)}, status_code=422)
        if not getattr(svc.tts, "loaded", False):
            raise HTTPException(503, "tts unavailable")
        text, truncated = cap_text(req.text, settings.tts_max_chars)
        if not text:
            raise HTTPException(422, "empty text")
        if req.voice not in svc.tts.voices:
            raise HTTPException(422, f"unknown voice {req.voice!r}")
        if svc.tts_running + svc.tts_waiting >= settings.tts_parallel + settings.tts_queue_max:
            raise HTTPException(503, "tts busy", headers={"Retry-After": "2"})
        fmt = "wav" if req.format == "wav" else "ogg"
        svc.tts_waiting += 1
        try:
            await svc.tts_sem.acquire()
        finally:
            svc.tts_waiting -= 1
        svc.tts_running += 1
        try:
            loop = asyncio.get_running_loop()
            t = time.perf_counter()
            pcm, sr = await loop.run_in_executor(svc.tts_pool, svc.tts.synthesize, text, req.voice, req.speed)
            data, ctype = await asyncio.to_thread(encode, pcm, sr, fmt)
            elapsed = time.perf_counter() - t
        except Exception:
            log.exception("tts failed (%d chars)", len(text))
            raise HTTPException(500, "tts failed") from None
        finally:
            svc.tts_running -= 1
            svc.tts_sem.release()
        headers = {
            "X-Meetpp-Duration-S": f"{len(pcm) / sr:.3f}",
            "X-Meetpp-Synth-S": f"{elapsed:.3f}",
            "X-Meetpp-Voice": req.voice,
        }
        if truncated:
            headers["X-Meetpp-Truncated"] = "1"
        return Response(content=data, media_type=ctype, headers=headers)

    return app

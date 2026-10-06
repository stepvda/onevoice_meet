"""Tier-2 client for meetpp-speech on the Mac Studio (contract §6.2).

* ``POST /transcribe?language=en&prompt=…`` with the Ogg/Opus utterance as
  body, HMAC-signed with ``MEETPP_SPEECH_SECRET``; at most 2 in flight.
* ``POST /tts`` for announcement clips.
* ``GET /health`` every 30 s; 3 consecutive failures → "down" (tier 1 keeps
  running); one success → "up" again.
* Repetition guard: large-v3-turbo can fall into loops ("I'm going to say"
  × 40). Such text is discarded and the tier-1 text kept.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import zlib
from typing import Callable

import httpx

from . import config
from .poster import dumps, sign
from .stt import tail_text

log = logging.getLogger("meetpp.agent")

PROMPT_MAX_CHARS = 600
CONTEXT_MAX_CHARS = 300
NEAR_LIVE_BUDGET_S = 8.0
FINAL_TIMEOUT_S = 60.0
HEALTH_INTERVAL_S = 30.0
FAIL_THRESHOLD = 3
CONCURRENCY = 2
CUT_TEXT_MIN_RATIO = 0.8
BUSY_RETRY_CAP_S = 10.0

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


# ─── repetition guard ──────────────────────────────────────────────────────


def has_repetition_loop(text: str, min_n: int = 3, max_n: int = 6, min_repeats: int = 4) -> bool:
    """True when some n-gram (min_n ≤ n ≤ max_n words) is repeated back to
    back at least ``min_repeats`` times, or a 1–2-word unit ≥ 8 times."""
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    checks = [(n, min_repeats) for n in range(min_n, max_n + 1)] + [(1, 8), (2, 8)]
    for n, reps in checks:
        if len(words) < n * reps:
            continue
        for i in range(0, len(words) - n * reps + 1):
            gram = words[i : i + n]
            k = 1
            j = i + n
            while j + n <= len(words) and words[j : j + n] == gram:
                k += 1
                if k >= reps:
                    return True
                j += n
    return False


def compression_ratio(text: str) -> float:
    raw = (text or "").encode("utf-8")
    if not raw:
        return 0.0
    return len(raw) / len(zlib.compress(raw))


def degenerate_reason(
    text: str,
    duration_s: float,
    reported_repetition: bool = False,
    tier1_text: str = "",
) -> str | None:
    """Why a tier-2 text must not replace tier 1, or None if it is fine.

    meetpp-speech already cuts a detected loop (``repetition: true``, text
    cut before the loop). That cut text is kept only when it is not much
    shorter than the tier-1 text (the loop usually eats the end of the
    utterance); our own n-gram guard always runs as a second check."""
    if reported_repetition:
        if not tier1_text or len(text) < CUT_TEXT_MIN_RATIO * len(tier1_text):
            return "repetition_cut_short"
    if has_repetition_loop(text):
        return "ngram_loop"
    if len(text) >= 120 and compression_ratio(text) > 2.4:
        return "compression_ratio"
    # Conversational speech rarely exceeds ~20 characters per second.
    if duration_s > 0 and len(text) > max(80.0, 30.0 * duration_s):
        return "too_long"
    return None


def build_tier2_prompt(glossary: str, context: str, max_chars: int = PROMPT_MAX_CHARS) -> str:
    ctx = tail_text(context, CONTEXT_MAX_CHARS)
    room = max_chars - len(ctx) - (1 if ctx else 0)
    g = re.sub(r"\s+", " ", glossary or "").strip()
    if len(g) > room:
        g = g[: max(0, room)].rsplit(" ", 1)[0] if room > 0 else ""
    # Glossary first, rolling context last, ending exactly where the previous
    # utterance ended (Whisper reads the prompt as the words just before the
    # audio). Trim from the front if anything is too long.
    return f"{g} {ctx}".strip()[-max_chars:]


# ─── client ────────────────────────────────────────────────────────────────


class Tier2Error(RuntimeError):
    pass


class Tier2Busy(Tier2Error):
    """503 from meetpp-speech: its queue is full. Retry after ``retry_after``
    (final pass) or give up and keep tier 1 (near-live)."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"busy (Retry-After {retry_after:g}s)")
        self.retry_after = retry_after


class Tier2Client:
    def __init__(
        self,
        base_url: str,
        secret: str,
        client: httpx.AsyncClient,
        *,
        concurrency: int = CONCURRENCY,
        health_interval_s: float = HEALTH_INTERVAL_S,
        fail_threshold: int = FAIL_THRESHOLD,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.client = client
        self.health_interval_s = health_interval_s
        self.fail_threshold = fail_threshold
        self._sem = asyncio.Semaphore(concurrency)
        self._clock = clock
        self.state = "down"  # until the first successful health check
        self.consecutive_failures = 0
        self.last_health: dict | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def up(self) -> bool:
        return self.state == "up"

    # ── health ──
    async def check_health(self) -> bool:
        try:
            r = await self.client.get(f"{self.base_url}/health", timeout=5.0)
            ok = r.status_code < 400 and bool((r.json() or {}).get("ok", True))
            if ok:
                self.last_health = r.json()
            else:
                self.last_error = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            ok = False
            self.last_error = f"{type(exc).__name__}: {exc}"
        self._record_health(ok)
        return ok

    def _record_health(self, ok: bool) -> None:
        if ok:
            self.consecutive_failures = 0
            if self.state != "up":
                log.info("MEETPP_TIER2 up (%s) model=%s", self.base_url, (self.last_health or {}).get("model"))
            self.state = "up"
            return
        self.consecutive_failures += 1
        if self.state == "up" and self.consecutive_failures >= self.fail_threshold:
            self.state = "down"
            log.warning(
                "MEETPP_TIER2 down after %d failed health checks (%s): tier-1 text only",
                self.consecutive_failures,
                self.last_error,
            )
        elif self.state != "up" and self.consecutive_failures == 1:
            log.warning("MEETPP_TIER2 unreachable (%s): %s", self.base_url, self.last_error)

    async def health_loop(self) -> None:
        while True:
            await self.check_health()
            await asyncio.sleep(self.health_interval_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.health_loop(), name="tier2-health")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ── calls ──
    async def transcribe(
        self,
        audio: bytes,
        prompt: str,
        *,
        timeout: float,
        content_type: str = "audio/ogg",
        language: str = config.LANGUAGE,
    ) -> dict:
        """Returns the service JSON ``{text, avg_logprob, duration_s, rtf,
        model, repetition}``. ``timeout`` covers queueing for a slot too."""

        async def _call() -> dict:
            async with self._sem:
                headers = {"Content-Type": content_type, **sign(self.secret, audio)}
                r = await self.client.post(
                    f"{self.base_url}/transcribe",
                    params={"language": language, "prompt": (prompt or "")[-PROMPT_MAX_CHARS:]},
                    content=audio,
                    headers=headers,
                    timeout=timeout,
                )
            if r.status_code == 503:
                try:
                    retry_after = float(r.headers.get("Retry-After") or 2)
                except ValueError:
                    retry_after = 2.0
                raise Tier2Busy(max(0.0, min(retry_after, BUSY_RETRY_CAP_S)))
            if r.status_code >= 400:
                raise Tier2Error(f"HTTP {r.status_code}: {(r.text or '')[:200]}")
            data = r.json()
            if not isinstance(data, dict) or "text" not in data:
                raise Tier2Error("malformed response")
            return data

        try:
            return await asyncio.wait_for(_call(), timeout)
        except asyncio.TimeoutError as exc:
            raise Tier2Error(f"budget {timeout:.0f}s exceeded") from exc
        except httpx.HTTPError as exc:
            raise Tier2Error(f"{type(exc).__name__}: {exc}") from exc

    async def tts(self, text: str, voice: str, timeout: float = 30.0) -> tuple[bytes, str]:
        """Returns (audio bytes, content type): audio/ogg, or audio/wav when
        the service could not encode Opus."""
        raw = dumps({"text": text, "voice": voice, "format": "ogg"})
        headers = {"Content-Type": "application/json", **sign(self.secret, raw)}
        try:
            r = await self.client.post(f"{self.base_url}/tts", content=raw, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise Tier2Error(f"{type(exc).__name__}: {exc}") from exc
        if r.status_code >= 400 or not r.content:
            raise Tier2Error(f"HTTP {r.status_code}")
        if r.headers.get("X-Meetpp-Truncated") == "1":
            log.info("MEETPP_TTS text truncated by meetpp-speech (%d chars)", len(text))
        return r.content, r.headers.get("content-type", "audio/ogg")

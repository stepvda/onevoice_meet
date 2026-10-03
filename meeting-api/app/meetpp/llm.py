"""Provider-agnostic LLM gateway.

One implementation speaks the OpenAI-compatible chat-completions API over
plain httpx, so DeepSeek (R1) and any local OpenAI-compatible server (R2:
llama.cpp, Ollama, LM Studio, mlx-lm) work through configuration only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.meetpp.models import MeetppLlmCall

log = logging.getLogger("app.meetpp")


class LLMError(Exception):
    pass


class LLMBudgetExceeded(LLMError):
    pass


class LLMCircuitOpen(LLMError):
    pass


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


@dataclass
class LLMProvider(Protocol):
    async def complete_json(
        self, *, purpose: str, messages: list[dict], max_tokens: int, temperature: float
    ) -> LLMResult: ...


@dataclass
class _Breaker:
    failures: list[float] = field(default_factory=list)
    opened_at: float | None = None
    _probe_at: float = 0.0

    def available(self) -> bool:
        if self.opened_at is None:
            return True
        return time.monotonic() >= self._probe_at

    def record_success(self) -> None:
        self.failures.clear()
        self.opened_at = None

    def record_failure(self) -> None:
        now = time.monotonic()
        self.failures = [t for t in self.failures if now - t < 120]
        self.failures.append(now)
        if len(self.failures) >= 5:
            self.opened_at = now
            self._probe_at = now + 60

    @property
    def state(self) -> str:
        return "open" if self.opened_at is not None and not self.available() else "closed"


_breaker = _Breaker()


class OpenAIChatProvider:
    """httpx client for any OpenAI-compatible `/chat/completions` endpoint."""

    def __init__(self, base_url: str, api_key: str, model: str, model_final: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.model_final = model_final or model

    async def complete_json(
        self, *, purpose: str, messages: list[dict], max_tokens: int, temperature: float
    ) -> LLMResult:
        model = self.model_final if purpose in ("finalise",) else self.model
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=5.0)) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        latency_ms = int((time.monotonic() - started) * 1000)
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            model=model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            cached_tokens=int(usage.get("prompt_cache_hit_tokens") or usage.get("prompt_tokens_details", {}).get("cached_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
        )


def provider() -> LLMProvider | None:
    if not settings.llm_api_key:
        return None
    return OpenAIChatProvider(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model,
        settings.llm_model_final,
    )


def llm_configured() -> bool:
    return bool(settings.llm_api_key)


def _tokens_this_hour(db: Session, session_id: str | None) -> int:
    from datetime import timedelta

    from sqlalchemy import func

    if session_id is None:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    total = (
        db.query(func.coalesce(func.sum(MeetppLlmCall.prompt_tokens), 0))
        .filter(MeetppLlmCall.session_id == session_id, MeetppLlmCall.created_at >= cutoff)
        .scalar()
    )
    return int(total or 0)


async def complete_json(
    *,
    db: Session | None,
    purpose: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    session_id: str | None = None,
    enforce_budget: bool = True,
) -> LLMResult:
    """One LLM call with retries, circuit breaker, token accounting and
    optional per-session budget enforcement."""
    global _breaker
    prov = provider()
    if prov is None:
        raise LLMError("LLM is not configured (LLM_API_KEY empty)")
    if not _breaker.available():
        raise LLMCircuitOpen("LLM circuit breaker is open")
    if enforce_budget and db is not None:
        budget = settings.meetpp_max_tokens_per_hour
        if _tokens_this_hour(db, session_id) >= budget:
            raise LLMBudgetExceeded("session token budget reached")

    last_exc: Exception | None = None
    for attempt in range(settings.llm_max_retries + 1):
        try:
            result = await prov.complete_json(
                purpose=purpose, messages=messages, max_tokens=max_tokens, temperature=temperature
            )
            _breaker.record_success()
            _record(db, session_id, purpose, result, "ok")
            return result
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
                if status in (429, 500, 502, 503, 504):
                    _breaker.record_failure()
                    await asyncio.sleep(min(2 ** attempt + 0.2 * attempt, 5.0))
                    continue
            _breaker.record_failure()
            break
    _record_failure(db, session_id, purpose, str(last_exc))
    raise LLMError(f"LLM call failed: {last_exc}")


def _record(db: Session | None, session_id: str | None, purpose: str, result: LLMResult, status: str) -> None:
    if db is None:
        return
    try:
        db.add(
            MeetppLlmCall(
                session_id=session_id,
                purpose=purpose,
                model=result.model,
                prompt_tokens=result.prompt_tokens,
                cached_tokens=result.cached_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=result.latency_ms,
                status=status,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _record_failure(db: Session | None, session_id: str | None, purpose: str, error: str) -> None:
    if db is None:
        return
    try:
        db.add(
            MeetppLlmCall(
                session_id=session_id,
                purpose=purpose,
                model=None,
                latency_ms=0,
                status="error",
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def circuit_state() -> str:
    return _breaker.state


def _extract_json(text: str) -> dict:
    """Parse a JSON object from the model output; one repair attempt is done
    by the caller by re-sending with the error message."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    # Find the outermost object.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def parse_json(text: str) -> dict:
    return _extract_json(text)

"""Provider-agnostic LLM gateway (OpenAI-compatible chat completions).

- one circuit breaker per purpose group: live ticks ("tick") and everything
  else ("compose": compositions and PDF structuring), FDD §8.8;
- every call (ok or error) is recorded in meetpp_llm_calls with its error;
- `complete_parsed()` parses the JSON reply and re-prompts once with the
  parse error before giving up (`LLMParseError`);
- the per-session budget never stops interpretation: `over_budget()` lets
  the runtime slow ticks down to one per 30 s.

Tests replace `complete_json` (the raw call returning an `LLMResult`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.meetpp import util
from app.meetpp.models import MeetppLlmCall

log = logging.getLogger("app.meetpp")


class LLMError(Exception):
    pass


class LLMCircuitOpen(LLMError):
    pass


class LLMParseError(LLMError):
    pass


# Kept for import compatibility; the budget no longer raises.
class LLMBudgetExceeded(LLMError):
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
class _Breaker:
    failures: list[float] = field(default_factory=list)
    opened_at: float | None = None
    probe_at: float = 0.0

    def available(self) -> bool:
        return self.opened_at is None or time.monotonic() >= self.probe_at

    def record_success(self) -> None:
        self.failures.clear()
        self.opened_at = None

    def record_failure(self) -> None:
        now = time.monotonic()
        self.failures = [t for t in self.failures if now - t < 120]
        self.failures.append(now)
        if len(self.failures) >= 5:
            self.opened_at = now
            self.probe_at = now + 60

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "open" if not self.available() else "half-open"


_breakers: dict[str, _Breaker] = {"tick": _Breaker(), "compose": _Breaker()}
# Sessions whose last tick failed (LLM down / invalid output), for ai.status.
_tick_failing: dict[str, float] = {}


def purpose_group(purpose: str) -> str:
    return "tick" if purpose in ("tick", "eval") else "compose"


def breaker(purpose: str) -> _Breaker:
    return _breakers[purpose_group(purpose)]


def circuit_state(purpose: str = "tick") -> str:
    return breaker(purpose).state


def reset_breakers() -> None:
    for b in _breakers.values():
        b.record_success()
    _tick_failing.clear()


def mark_tick(session_id: str, ok: bool) -> None:
    if ok:
        _tick_failing.pop(session_id, None)
    else:
        _tick_failing.setdefault(session_id, time.monotonic())


def forget(session_id: str) -> None:
    _tick_failing.pop(session_id, None)


def llm_configured() -> bool:
    return bool(settings.llm_api_key)


def ai_status(session_id: str | None = None) -> str:
    if not llm_configured():
        return "unavailable"
    if breaker("tick").state == "open" or (session_id and session_id in _tick_failing):
        return "paused"
    return "ok"


class OpenAIChatProvider:
    def __init__(self, base_url: str, api_key: str, model: str, model_final: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.model_final = model_final or model

    async def complete(self, *, purpose: str, messages: list[dict], max_tokens: int, temperature: float) -> LLMResult:
        model = self.model_final if purpose in ("compose_final",) else self.model
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
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return LLMResult(
            text=((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "",
            model=model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            cached_tokens=int(usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def provider() -> OpenAIChatProvider | None:
    if not settings.llm_api_key:
        return None
    return OpenAIChatProvider(settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_model_final)


def tokens_last_hour(db: Session, session_id: str | None) -> int:
    if not session_id:
        return 0
    cutoff = util.now() - timedelta(hours=1)
    total = (
        db.query(func.coalesce(func.sum(MeetppLlmCall.prompt_tokens), 0))
        .filter(MeetppLlmCall.session_id == session_id, MeetppLlmCall.created_at >= cutoff)
        .scalar()
    )
    return int(total or 0)


def over_budget(db: Session, session_id: str | None) -> bool:
    return tokens_last_hour(db, session_id) >= int(settings.meetpp_max_tokens_per_hour or 0) > 0


def _record(db: Session | None, session_id: str | None, purpose: str, result: LLMResult | None, status: str, error: str | None = None) -> None:
    if db is None:
        return
    try:
        db.add(
            MeetppLlmCall(
                session_id=session_id,
                purpose=purpose[:30],
                model=result.model if result else None,
                prompt_tokens=result.prompt_tokens if result else 0,
                cached_tokens=result.cached_tokens if result else 0,
                completion_tokens=result.completion_tokens if result else 0,
                latency_ms=result.latency_ms if result else 0,
                status=status,
                error=(error or None) and error[:300],
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


async def complete_json(
    *,
    db: Session | None,
    purpose: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float = 0.2,
    session_id: str | None = None,
) -> LLMResult:
    """One LLM call with retries on 429/5xx and the purpose's breaker."""
    prov = provider()
    if prov is None:
        raise LLMError("LLM is not configured (LLM_API_KEY empty)")
    brk = breaker(purpose)
    if not brk.available():
        raise LLMCircuitOpen(f"LLM circuit open ({purpose_group(purpose)})")
    last_exc: Exception | None = None
    for attempt in range(int(settings.llm_max_retries) + 1):
        try:
            result = await prov.complete(purpose=purpose, messages=messages, max_tokens=max_tokens, temperature=temperature)
            brk.record_success()
            _record(db, session_id, purpose, result, "ok")
            return result
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            brk.record_failure()
            if exc.response.status_code in (429, 500, 502, 503, 504) and attempt < settings.llm_max_retries:
                await asyncio.sleep(min(2**attempt, 5))
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            brk.record_failure()
            break
    _record(db, session_id, purpose, None, "error", f"{type(last_exc).__name__}: {last_exc}")
    raise LLMError(f"LLM call failed: {last_exc}")


def parse_json(text: str) -> dict:
    """Parse one JSON object from a model reply (code fences and leading or
    trailing prose tolerated). Raises ValueError."""
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    value = json.loads(raw[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("the reply is not a JSON object")
    return value


async def complete_parsed(
    *,
    db: Session | None,
    purpose: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float = 0.2,
    session_id: str | None = None,
    validate: Callable[[dict], str | None] | None = None,
) -> tuple[dict, LLMResult]:
    """Call, parse and validate; on failure re-prompt once with the error.
    Raises LLMParseError when the repair fails too (LLMError when the call
    itself fails)."""
    result = await complete_json(
        db=db, purpose=purpose, messages=messages, max_tokens=max_tokens, temperature=temperature, session_id=session_id
    )
    error = None
    try:
        parsed = parse_json(result.text)
        error = validate(parsed) if validate else None
        if error is None:
            return parsed, result
    except (ValueError, json.JSONDecodeError) as exc:
        error = f"invalid JSON: {exc}"
    log.info("meetpp: %s reply rejected (%s); repairing once", purpose, error)
    repair = messages + [
        {"role": "assistant", "content": (result.text or "")[:6000]},
        {
            "role": "user",
            "content": f"Your reply could not be used: {error}. Reply again with only the corrected json object.",
        },
    ]
    result2 = await complete_json(
        db=db, purpose=purpose, messages=repair, max_tokens=max_tokens, temperature=0.0, session_id=session_id
    )
    try:
        parsed = parse_json(result2.text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise LLMParseError(f"invalid JSON after repair: {exc}") from exc
    error = validate(parsed) if validate else None
    if error is not None:
        raise LLMParseError(f"invalid output after repair: {error}")
    return parsed, result2

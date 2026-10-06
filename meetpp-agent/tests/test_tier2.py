from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging

import httpx
import pytest

from agent.tier2 import (
    Tier2Busy,
    Tier2Client,
    Tier2Error,
    build_tier2_prompt,
    degenerate_reason,
    has_repetition_loop,
)
from agent.tts import TTSCache, TTSUnavailable, clip_hash

SPEECH_SECRET = "speech-secret"


@pytest.mark.parametrize(
    "text,loop",
    [
        ("I'm going to say I'm going to say I'm going to say I'm going to say I'm going to say", True),
        ("we need to we need to we need to we need to", True),  # 3-gram × 4
        ("the the the the the the the the", True),
        ("we need to we need to we need to finish", False),  # only 3 repeats
        ("The board approves the budget. The board also approves the plan.", False),
        ("", False),
    ],
)
def test_repetition_guard(text, loop):
    assert has_repetition_loop(text) is loop


def test_degenerate_reason():
    t1 = "The board approves the budget for next year."
    # service cut the loop: accept the cut text only if it is not much shorter than tier 1
    assert degenerate_reason("The board approves the budget for next", 3.0, True, t1) is None
    assert degenerate_reason("The board approves", 3.0, True, t1) == "repetition_cut_short"
    assert degenerate_reason("fine text", 2.0, reported_repetition=True) == "repetition_cut_short"
    assert degenerate_reason("go on " * 10, 5.0) == "ngram_loop"
    assert degenerate_reason("a" * 200, 1.0) is not None
    assert degenerate_reason("The motion is carried unanimously.", 2.5) is None


def test_tier2_prompt_budget():
    glossary = "Meeting of OneVoice. Participants: " + ", ".join(f"Person{i}" for i in range(200))
    ctx = " ".join(f"w{i}" for i in range(300))
    p = build_tier2_prompt(glossary, ctx)
    assert len(p) <= 600
    assert p.startswith("Meeting of OneVoice.") and p.endswith("w299")
    assert build_tier2_prompt("Short glossary.", "") == "Short glossary."


def verify(request: httpx.Request) -> None:
    ts = request.headers["X-Meetpp-Timestamp"]
    expected = hmac.new(SPEECH_SECRET.encode(), ts.encode() + b"." + request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-Meetpp-Signature"] == expected


async def test_transcribe_signs_body_and_limits_concurrency():
    active = {"now": 0, "max": 0}
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        verify(request)
        seen.append(request)
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.05)
        active["now"] -= 1
        return httpx.Response(200, json={"text": "Refined.", "avg_logprob": -0.1, "duration_s": 1.0, "rtf": 0.05, "model": "large-v3-turbo", "repetition": False})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    t2 = Tier2Client("http://mac:9310", SPEECH_SECRET, client)
    results = await asyncio.gather(*(t2.transcribe(b"OggS-audio", "Meeting of X. previous text", timeout=8) for _ in range(5)))
    assert all(r["text"] == "Refined." for r in results)
    assert active["max"] == 2
    req = seen[0]
    assert req.url.path == "/transcribe"
    assert req.url.params["language"] == "en" and req.url.params["prompt"] == "Meeting of X. previous text"
    assert req.headers["content-type"] == "audio/ogg" and req.content == b"OggS-audio"


async def test_transcribe_budget_and_errors():
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"text": "late"})

    t2 = Tier2Client("http://mac", SPEECH_SECRET, httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    with pytest.raises(Tier2Error, match="budget"):
        await t2.transcribe(b"x", "", timeout=0.1)

    t2 = Tier2Client("http://mac", SPEECH_SECRET, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(Tier2Error, match="500"):
        await t2.transcribe(b"x", "", timeout=1)


async def test_health_three_failures_mark_down(caplog):
    ok = {"v": True}

    def handler(request):
        if not ok["v"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"ok": True, "model": "large-v3-turbo"})

    t2 = Tier2Client("http://mac", SPEECH_SECRET, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert t2.state == "down"
    assert await t2.check_health() and t2.state == "up"
    ok["v"] = False
    with caplog.at_level(logging.WARNING, logger="meetpp.agent"):
        await t2.check_health()
        await t2.check_health()
        assert t2.state == "up"  # two failures are tolerated
        await t2.check_health()
    assert t2.state == "down"
    assert any("MEETPP_TIER2 down" in r.message for r in caplog.records)
    ok["v"] = True
    await t2.check_health()
    assert t2.state == "up"


async def test_tts_cache_proxy(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        verify(request)
        assert request.url.path == "/tts"
        return httpx.Response(200, content=b"OggS-tts-bytes", headers={"content-type": "audio/ogg"})

    t2 = Tier2Client("http://mac", SPEECH_SECRET, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    cache = TTSCache(tmp_path, t2)
    with pytest.raises(TTSUnavailable):
        await cache.get("Next: item three", "am_michael")  # tier 2 not up yet
    await t2.check_health()
    out = await cache.get("Next: item three", "am_michael")
    h = hashlib.sha256("am_michael|Next: item three".encode()).hexdigest()
    assert out == {"hash": h, "path": str(tmp_path / "tts" / f"{h}.ogg")}
    assert clip_hash("am_michael", "Next: item three") == h
    assert (tmp_path / "tts" / f"{h}.ogg").read_bytes() == b"OggS-tts-bytes"
    n = len(calls)
    t2.state = "down"
    assert await cache.get("Next: item three", "am_michael") == out  # cached, no call
    assert len(calls) == n
    with pytest.raises(TTSUnavailable):
        await cache.get("Something new", "am_michael")


async def test_tts_off_without_tier2(tmp_path):
    with pytest.raises(TTSUnavailable):
        await TTSCache(tmp_path, None).get("hello")


async def test_503_is_busy_with_retry_after_and_prompt_keeps_its_end():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(503, headers={"Retry-After": "2"}, json={"detail": "queue full"})

    t2 = Tier2Client("http://mac", SPEECH_SECRET, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    long_prompt = "G" * 500 + " " + "end of the previous utterance."
    with pytest.raises(Tier2Busy) as ei:
        await t2.transcribe(b"x", long_prompt, timeout=1)
    assert ei.value.retry_after == 2.0
    sent = seen[0].url.params["prompt"]
    assert len(sent) <= 600 and sent.endswith("end of the previous utterance.")


async def test_tts_wav_fallback_is_transcoded_to_ogg(tmp_path):
    import io
    import wave

    import numpy as np

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes((0.2 * np.sin(np.arange(24000) / 10) * 32767).astype(np.int16).tobytes())

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, content=buf.getvalue(), headers={"content-type": "audio/wav", "X-Meetpp-Truncated": "1"})

    t2 = Tier2Client("http://mac", SPEECH_SECRET, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await t2.check_health()
    out = await TTSCache(tmp_path, t2).get("Hello there", "am_michael")
    data = open(out["path"], "rb").read()
    assert data[:4] == b"OggS" and b"OpusHead" in data[:100]

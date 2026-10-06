"""API behaviour with fake engines (fast; no models needed)."""

from __future__ import annotations

import functools
import io
import json
import time

import anyio
import httpx
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from conftest import TEST_SECRET, FakeSynthesizer, FakeTranscriber, make_settings, tone_wav
from meetpp_speech.app import create_app
from meetpp_speech.auth import sign, signed_headers, verify


@pytest.fixture
def anyio_backend():
    return "asyncio"


def client_for(transcriber=None, synthesizer=None, **settings):
    app = create_app(make_settings(**settings), transcriber or FakeTranscriber(), synthesizer or FakeSynthesizer())
    return TestClient(app)


# --------------------------------------------------------------------------- auth unit
def test_sign_matches_contract_formula():
    import hashlib
    import hmac

    body = b"\x00\x01audio"
    want = hmac.new(b"k", b"1700000000." + body, hashlib.sha256).hexdigest()
    assert sign(b"k", 1700000000, body) == want == sign("k", "1700000000", body)


def test_verify_window_and_errors():
    body = b"x"
    now = 1_800_000_000
    ok = sign(b"k", now, body)
    assert verify(b"k", str(now), ok, body, 60, now=now + 59) is None
    assert verify(b"k", str(now), ok, body, 60, now=now - 59) is None
    assert verify(b"k", str(now), ok, body, 60, now=now + 61) == "stale timestamp"
    assert verify(b"k", str(now), ok, body, 60, now=now - 61) == "stale timestamp"
    assert verify(b"k", str(now), ok.upper(), body, 60, now=now) is None
    assert verify(b"k", str(now), ok, b"y", 60, now=now) == "bad signature"
    assert verify(b"other", str(now), ok, body, 60, now=now) == "bad signature"
    assert verify(b"k", None, ok, body, 60, now=now) == "missing timestamp"
    assert verify(b"k", "12.5", ok, body, 60, now=now) == "malformed timestamp"
    assert verify(b"k", str(now), None, body, 60, now=now) == "missing signature"


# --------------------------------------------------------------------------- auth via HTTP
def test_transcribe_accepts_valid_signature():
    body = tone_wav(1.5)
    with client_for() as c:
        r = c.post("/transcribe?language=en&prompt=Meeting%20of%20Witysk", content=body,
                   headers={**signed_headers(TEST_SECRET, body), "Content-Type": "audio/wav"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert set(j) >= {"text", "avg_logprob", "duration_s", "rtf", "model", "repetition"}
    assert j["text"] == "hello world" and j["repetition"] is False
    assert j["model"] == "large-v3-turbo" and abs(j["duration_s"] - 1.5) < 0.01


@pytest.mark.parametrize("case", ["bad_sig", "stale_ts", "future_ts", "no_headers", "tampered", "wrong_secret"])
def test_transcribe_rejects(case):
    body = tone_wav(1.0)
    ts = int(time.time())
    headers = signed_headers(TEST_SECRET, body, ts)
    if case == "bad_sig":
        headers["X-Meetpp-Signature"] = "0" * 64
    elif case == "stale_ts":
        headers = signed_headers(TEST_SECRET, body, ts - 61)
    elif case == "future_ts":
        headers = signed_headers(TEST_SECRET, body, ts + 61)
    elif case == "no_headers":
        headers = {}
    elif case == "tampered":
        body = body[:-2] + b"\x01\x02"
    elif case == "wrong_secret":
        headers = signed_headers("not-the-secret", body, ts)
    t = FakeTranscriber()
    with client_for(t) as c:
        r = c.post("/transcribe", content=body, headers=headers)
    assert r.status_code == 401
    assert t.calls == []  # never reached the model


def test_tts_requires_signature():
    body = json.dumps({"text": "hi"}).encode()
    with client_for() as c:
        r = c.post("/tts", content=body, headers={"X-Meetpp-Timestamp": str(int(time.time())),
                                                  "X-Meetpp-Signature": "ab" * 32})
        assert r.status_code == 401
        r = c.post("/tts", content=body, headers=signed_headers(TEST_SECRET, body, int(time.time()) - 120))
        assert r.status_code == 401


def test_health_is_open_and_not_sensitive():
    with client_for() as c:
        r = c.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["model"] == "large-v3-turbo" and j["tts"] == "kokoro"
    assert j["queue"] == 0 and j["busy"] == 0 and "rtf_p50" in j
    assert TEST_SECRET not in r.text


# --------------------------------------------------------------------------- transcribe behaviour
def _post_audio(c, body: bytes, query: str = ""):
    return c.post(f"/transcribe{query}", content=body, headers=signed_headers(TEST_SECRET, body))


def test_prompt_and_language_reach_the_model():
    t = FakeTranscriber()
    with client_for(t) as c:
        assert _post_audio(c, tone_wav(1.0), "?language=fr&prompt=" + "x" * 700).status_code == 200
        assert _post_audio(c, tone_wav(1.0), "?language=xx").status_code == 422
    dur, lang, prompt = t.calls[0]
    assert lang == "fr" and len(prompt) == 600  # tail kept, contract <= 600


def test_repetition_is_flagged_and_truncated():
    t = FakeTranscriber("We move to the budget. I'm going to say " + "I'm going to say " * 10)
    with client_for(t) as c:
        j = _post_audio(c, tone_wav(2.0)).json()
    assert j["repetition"] is True
    assert j["text"] == "We move to the budget."
    assert j["text_raw"].startswith("We move to the budget. I'm going")


def test_undecodable_audio_is_415():
    with client_for() as c:
        assert _post_audio(c, b"garbage" * 50).status_code == 415


def test_too_long_audio_is_413():
    with client_for(max_audio_s=2.0) as c:
        assert _post_audio(c, tone_wav(3.0)).status_code == 413


def test_tiny_audio_skips_model():
    t = FakeTranscriber()
    with client_for(t) as c:
        j = _post_audio(c, tone_wav(0.05)).json()
    assert j["text"] == "" and t.calls == []


# --------------------------------------------------------------------------- concurrency
@pytest.mark.anyio
async def test_flood_gets_503_when_more_than_30s_waiting():
    """2 run at once; 30 s of audio may wait; the rest is rejected with 503."""
    slow = FakeTranscriber(delay=0.6)
    app = create_app(make_settings(max_parallel=2, queue_max_s=30.0), slow, FakeSynthesizer())
    body = tone_wav(10.0)  # 10 s utterances: 2 running + 3 waiting (30 s) fit; the rest is refused
    results: list[int] = []

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            async def one():
                r = await c.post("/transcribe", content=body, headers=signed_headers(TEST_SECRET, body))
                results.append(r.status_code)
                if r.status_code == 503:
                    assert r.headers.get("retry-after") == "2"

            async def peek_health():
                await anyio.sleep(0.3)
                h = (await c.get("/health")).json()
                assert h["busy"] == 2 and h["queue"] == 3 and h["queue_s"] == 30.0

            async with anyio.create_task_group() as tg:
                for _ in range(8):
                    tg.start_soon(one)
                tg.start_soon(peek_health)

            assert sorted(results) == [200] * 5 + [503] * 3
            # once drained, new work is accepted again
            r = await c.post("/transcribe", content=body, headers=signed_headers(TEST_SECRET, body))
            assert r.status_code == 200
            h = (await c.get("/health")).json()
            assert h["busy"] == 0 and h["queue"] == 0
    assert len(slow.calls) == 6


@pytest.mark.anyio
async def test_parallelism_is_capped_at_two():
    import threading

    active, peak = 0, 0
    lock = threading.Lock()

    class Probe(FakeTranscriber):
        def transcribe(self, audio, language="en", prompt=None):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.2)
            with lock:
                active -= 1
            return super().transcribe(audio, language, prompt)

    app = create_app(make_settings(max_parallel=2, queue_max_s=1000), Probe(), FakeSynthesizer())
    body = tone_wav(1.0)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            async with anyio.create_task_group() as tg:
                for _ in range(6):
                    tg.start_soon(functools.partial(c.post, "/transcribe", content=body,
                                                    headers=signed_headers(TEST_SECRET, body)))
    assert peak == 2


# --------------------------------------------------------------------------- tts
def _post_tts(c, payload: dict):
    body = json.dumps(payload).encode()
    return c.post("/tts", content=body, headers={**signed_headers(TEST_SECRET, body),
                                                  "Content-Type": "application/json"})


def test_tts_returns_ogg_opus():
    with client_for() as c:
        r = _post_tts(c, {"text": "Item three.", "voice": "am_michael", "format": "ogg"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/ogg"
    assert r.content[:4] == b"OggS" and b"OpusHead" in r.content[:64]
    x, sr = sf.read(io.BytesIO(r.content))
    assert len(x) / sr > 0.5


def test_tts_wav_fallback_when_opus_unavailable(monkeypatch):
    import meetpp_speech.audio as audio

    monkeypatch.setattr(audio, "encode_ogg_opus", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no opus")))
    with client_for() as c:
        r = _post_tts(c, {"text": "Item three.", "voice": "am_michael", "format": "ogg"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"


def test_tts_caps_text_at_400_chars():
    seen = []

    class Rec(FakeSynthesizer):
        def synthesize(self, text, voice="am_michael", speed=1.0):
            seen.append(text)
            return super().synthesize(text, voice, speed)

    with client_for(synthesizer=Rec()) as c:
        r = _post_tts(c, {"text": "word " * 200})
    assert r.status_code == 200 and r.headers.get("x-meetpp-truncated") == "1"
    assert len(seen[0]) <= 400


def test_tts_validation():
    with client_for() as c:
        assert _post_tts(c, {"text": ""}).status_code == 422
        assert _post_tts(c, {"text": "hi", "voice": "nobody"}).status_code == 422
        assert _post_tts(c, {"text": "hi", "format": "mp3"}).status_code == 422
        r = c.post("/tts", content=b"{not json", headers=signed_headers(TEST_SECRET, b"{not json"))
        assert r.status_code == 422


def test_tts_unavailable_is_503():
    class Off(FakeSynthesizer):
        loaded = False

        def available(self):
            return False

    with client_for(synthesizer=Off()) as c:
        assert _post_tts(c, {"text": "hi"}).status_code == 503
        assert c.get("/health").json()["tts"] == "off"

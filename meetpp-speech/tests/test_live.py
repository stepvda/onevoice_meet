"""Integration tests against a running service with the real models.

Started automatically via ./run.sh on a free port, or point at a running one:
    MEETPP_SPEECH_TEST_URL=http://127.0.0.1:9310 MEETPP_SPEECH_TEST_SECRET=... pytest -m integration -s
"""

from __future__ import annotations

import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import soundfile as sf

from client_example import health, transcribe, tts

pytestmark = pytest.mark.integration


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


KEY_WORDS = {"board", "approved", "budget", "community", "hall", "renovation"}


def test_health(live):
    h = health(live["url"])
    assert h["ok"] is True and h["model"] == "large-v3-turbo" and h["tts"] == "kokoro"
    assert isinstance(h["rtf_p50"], float) and h["busy"] == 0
    print(f"\n[health] {json.dumps(h)}")
    if live.get("startup_s"):
        print(f"[startup] process start -> healthy: {live['startup_s']:.1f}s (warm-up {h['warmup_s']}s)")


@pytest.mark.parametrize("fmt,ctype", [("wav", "audio/wav"), ("ogg", "audio/ogg"), ("ogg_ffmpeg", "audio/ogg")])
def test_transcribe_short(live, speech, fmt, ctype):
    if fmt not in speech["short"]:
        pytest.skip("ffmpeg not installed (only used to make an agent-style fixture)")
    status, j = transcribe(live["url"], live["secret"], speech["short"][fmt], ctype,
                           prompt="Meeting of the Witysk association. Topics: budget, community hall.")
    assert status == 200, j
    assert KEY_WORDS <= words(j["text"]), j["text"]
    assert j["repetition"] is False and j["model"] == "large-v3-turbo"
    assert j["avg_logprob"] > -1.0
    print(f"\n[{fmt}] {j['duration_s']:.1f}s rtf={j['rtf']} '{j['text']}'")


def test_transcribe_long_and_measure_rtf(live, speech):
    data = speech["long"]["ogg"]
    runs = []
    for _ in range(3):
        status, j = transcribe(live["url"], live["secret"], data, "audio/ogg")
        assert status == 200, j
        runs.append(j)
    w = words(runs[-1]["text"])
    for k in ("board", "meeting", "association", "minutes", "budget", "renovation", "treasurer",
              "financial", "membership", "fee"):
        assert k in w, (k, runs[-1]["text"])
    rtfs = [r["rtf"] for r in runs]
    print(f"\n[long] duration {runs[0]['duration_s']:.1f}s rtf runs={rtfs} -> "
          f"{min(rtfs) * runs[0]['duration_s']:.2f}s best; text: {runs[-1]['text']}")


def test_bad_signature_rejected_by_real_service(live, speech):
    status, j = transcribe(live["url"], "wrong-secret", speech["short"]["wav"], "audio/wav")
    assert status == 401


def test_tts_ogg_playable_and_roundtrip(live):
    text = "The meeting now moves to item three, the approval of the budget."
    t = time.perf_counter()
    status, ctype, data = tts(live["url"], live["secret"], text, "am_michael", "ogg")
    synth_s = time.perf_counter() - t
    assert status == 200 and ctype == "audio/ogg"
    assert data[:4] == b"OggS" and b"OpusHead" in data[:64]
    x, sr = sf.read(io.BytesIO(data), dtype="float32")
    dur = len(x) / sr
    assert 2.0 < dur < 10.0 and abs(x).max() > 0.05
    print(f"\n[tts] {len(data)} bytes, {dur:.1f}s audio in {synth_s:.2f}s")
    # what Kokoro said is what Whisper hears
    status, j = transcribe(live["url"], live["secret"], data, "audio/ogg")
    w = words(j["text"])
    assert status == 200 and {"meeting", "item", "approval", "budget"} <= w and w & {"three", "3"}, j


def test_tts_wav_format(live):
    status, ctype, data = tts(live["url"], live["secret"], "Quorum is reached.", "am_michael", "wav")
    assert status == 200 and ctype == "audio/wav" and data[:4] == b"RIFF"


def test_real_flood_gets_503(live, speech):
    """8 simultaneous 18 s utterances: 2 run, 1 waits (18 s <= 30 s), the 4th would exceed 30 s."""
    data = speech["long"]["wav"]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: transcribe(live["url"], live["secret"], data, "audio/wav", timeout=120)[0],
                                range(8)))
    print(f"\n[flood] statuses {sorted(results)}")
    assert results.count(503) >= 4 and results.count(200) >= 2
    assert health(live["url"])["busy"] == 0

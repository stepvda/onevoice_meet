"""Against a running meetpp-speech (opt-in):

    MEETPP_SPEECH_TEST_URL=http://127.0.0.1:9310 MEETPP_SPEECH_TEST_SECRET=… \\
        .venv/bin/python -m pytest -m integration -s
"""
from __future__ import annotations

import asyncio
import os
import re

import httpx
import numpy as np
import pytest

from agent.audio_store import encode_opus
from agent.session import AgentSession
from agent.tier2 import Tier2Client, build_tier2_prompt
from agent.tts import TTSCache
from tests.fakes import Recorder, make_services

URL = os.environ.get("MEETPP_SPEECH_TEST_URL", "")
SECRET = os.environ.get("MEETPP_SPEECH_TEST_SECRET", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="set MEETPP_SPEECH_TEST_URL / MEETPP_SPEECH_TEST_SECRET"),
]

GLOSSARY = "Meeting of the OneVoice board. Participants: Maria. Topics: budget, minutes."


def words(t: str) -> set[str]:
    return set(re.findall(r"[a-z]+", t.lower()))


async def client() -> Tier2Client:
    t2 = Tier2Client(URL, SECRET, httpx.AsyncClient())
    assert await t2.check_health(), t2.last_error
    return t2


async def test_transcribe_our_opus(speech):
    t2 = await client()
    data = encode_opus(np.concatenate([np.zeros(4800, np.float32), speech, np.zeros(2400, np.float32)]))
    res = await t2.transcribe(data, build_tier2_prompt(GLOSSARY, "Let us start with item one."), timeout=30)
    print("\ntier2:", res)
    assert {"board", "approves", "budget", "minutes", "friday"} <= words(res["text"])
    assert res["repetition"] is False


async def test_final_pass_against_real_service(tmp_path, speech):
    t2 = await client()
    rec = Recorder()
    services = make_services(tmp_path, rec, tier2=t2)
    s = AgentSession("INT", "room", "ws://lk", "tok", services=services, glossary=GLOSSARY)
    await s.start()
    half = len(speech) // 2
    cut = half + int(np.argmin(np.abs(speech[half : half + 16000])))
    parts = [("u1", speech[:cut], "10:00:01"), ("u2", speech[cut:], "10:00:05")]
    for uid, audio, t0 in parts:
        s.store.write(uid, audio, identity="user-maria", name="Maria", t_start=f"2026-10-06T{t0}.000Z", t_end=f"2026-10-06T{t0}.900Z", text="tier one")
    s.finalize()
    await asyncio.wait_for(s._final_task, 120)
    await s.poster.drain(5)
    refs = rec.items("refinements")
    print("\nfinal:", refs)
    assert [r["utterance_id"] for r in refs] == ["u1", "u2"] and all(r["final"] for r in refs)
    assert {"budget", "minutes"} <= words(" ".join(r["text"] for r in refs))
    assert rec.of("agent-status")[-1]["final_pass"] == "done"
    await s.close()


async def test_tts_clip_cached_as_ogg(tmp_path):
    t2 = await client()
    out = await TTSCache(tmp_path, t2).get("Item three: approval of the budget.", "am_michael")
    data = open(out["path"], "rb").read()
    assert data[:4] == b"OggS" and len(data) > 1000

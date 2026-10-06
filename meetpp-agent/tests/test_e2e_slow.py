"""Real models end to end (RUN_SLOW=1): macOS `say` speech → Silero VAD →
faster-whisper small → segment posted to a mock meeting-api."""
from __future__ import annotations

import asyncio
import re
import threading

import numpy as np
import pytest

from agent.session import AgentSession
from agent.stt import STTEngine, build_prompt
from agent.vad import UtteranceSegmenter, load_vad
from agent.worker import STTWorker
from tests.fakes import FakeParticipant, FakePub, FakeRoom, Recorder, make_services

pytestmark = pytest.mark.slow

GLOSSARY = "Meeting of the OneVoice board. Participants: Maria. Topics: budget, minutes."


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


@pytest.fixture(scope="module")
def engine():
    eng = STTEngine("small", "base", threads=4, download_root=None)
    eng.load()
    assert eng.ready.is_set() and eng.load_error is None
    return eng


def test_vad_to_stt_direct(speech, silero_path, engine):
    audio = np.concatenate([np.zeros(16000, np.float32), speech, np.zeros(24000, np.float32)])
    seg = UtteranceSegmenter(load_vad(silero_path).new_stream())
    utts = seg.process(audio)
    tail = seg.flush()
    utts += [tail] if tail else []
    assert utts
    transcript = ""
    for u in utts:
        res = engine.transcribe(u.audio, build_prompt(GLOSSARY, transcript))
        assert res.dropped is None, res
        transcript += " " + res.text
        assert res.rtf < 1.0
    got = words(transcript)
    for w in ("board", "approves", "budget", "minutes", "friday", "maria"):
        assert w in got, transcript


async def test_session_end_to_end_real_models(tmp_path, speech, silero_path, engine):
    rec = Recorder()
    clip = np.concatenate([np.zeros(8000, np.float32), speech, np.zeros(24000, np.float32)])

    async def stream(track):
        for i in range(0, len(clip), 160):  # ~10x real time
            await asyncio.sleep(0.001)
            yield clip[i : i + 160]
        while True:
            await asyncio.sleep(0.01)
            yield np.zeros(160, np.float32)

    services = make_services(tmp_path, rec, engine=engine, stream_factory=stream)
    services.vad = load_vad(silero_path)
    services.offload = None  # real asyncio.to_thread
    loop = asyncio.get_running_loop()
    services.worker = STTWorker(engine, dispatch=loop.call_soon_threadsafe)
    services.worker.start()
    rooms = []

    def room_factory():
        room = FakeRoom()
        p = room.add(FakeParticipant("user-maria", "Maria"))
        p.add(FakePub("TR_m"))
        rooms.append(room)
        return room

    services.room_factory = room_factory
    s = AgentSession("E2E", "room", "ws://lk", "tok", services=services, accepted=["user-maria"], glossary=GLOSSARY)
    await s.start()
    room = rooms[0]
    p = room.remote_participants["user-maria"]
    room.subscribe_complete(p, p.track_publications["TR_m"])
    for _ in range(600):
        await asyncio.sleep(0.05)
        if s.decoded >= 1 and s._stt_outstanding == 0:
            break
    await s.poster.drain(5)
    segs = rec.items("segments")
    assert segs, f"no segment posted; dropped={dict(s.dropped)}"
    text = " ".join(x["text"] for x in segs)
    assert {"board", "budget", "minutes"} <= words(text), text
    assert all(x["identity"] == "user-maria" and x["lang"] == "en" for x in segs)
    await asyncio.wait_for(asyncio.gather(*list(s._store_tasks)), 10)
    assert (tmp_path / "E2E" / "audio" / "index.jsonl").exists()
    print("\nE2E transcript:", text, "\n", s.summary_line())
    await s.close()
    services.worker.stop()

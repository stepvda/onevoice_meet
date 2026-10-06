from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from agent import config
from agent import main as main_mod
from agent.poster import InternalApi
from tests.fakes import SECRET, FakeEngine, FakePub, FakeParticipant, FakeRoom, Recorder, silent_stream

SID = "01K6Z8Q3J5V7W9X1Y2Z3A4B5C6"
SID2 = "01K6Z8Q3J5V7W9X1Y2Z3A4B5C7"


class EngineStub(FakeEngine):
    def __init__(self, *a, **kw):
        super().__init__()
        self.threads = 2
        self.loaded = ["small", "base"]
        self.load_error = None

    def load(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "STTEngine", EngineStub)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SPEECH_URL", "")
    rec = Recorder()
    rooms = []

    def room_factory():
        room = FakeRoom()
        p = room.add(FakeParticipant("user-alice", "Alice"))
        p.add(FakePub("TR_a"))
        rooms.append(room)
        return room

    with TestClient(main_mod.app) as c:
        svc = main_mod.state.services
        svc.api = InternalApi(httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)), "http://api", SECRET)
        svc.room_factory = room_factory
        svc.stream_factory = silent_stream
        c.rec, c.rooms = rec, rooms
        yield c
    main_mod.state.sessions.clear()


def start_body(**kw):
    body = {
        "session_id": SID,
        "room": "room-1",
        "ws_url": "ws://lk",
        "token": "tok",
        "language": "en",
        "glossary": "Meeting of OneVoice. Participants: Alice.",
        "accepted_identities": ["user-alice"],
        "paused": False,
    }
    body.update(kw)
    return body


def test_health_shape(client):
    h = client.get("/health").json()
    assert h["ok"] is True
    assert h["sessions"] == []
    assert h["model"] == "small"
    assert h["vad"] == "silero"
    assert h["tier2"] == "off"
    assert "rtf_p50" in h


def test_session_lifecycle(client):
    r = client.post("/sessions", json=start_body())
    assert r.status_code == 201
    assert client.post("/sessions", json=start_body()).status_code == 409
    assert client.rooms[0].remote_participants["user-alice"].track_publications["TR_a"].calls == [True]
    s = client.get("/health").json()["sessions"][0]
    assert s == {**s, "sid": SID, "connected": True, "consumers": 0, "paused": False, "tier2": "off"}
    assert client.patch(f"/sessions/{SID}", json={"paused": True, "glossary": "New glossary."}).status_code == 200
    assert client.get("/health").json()["sessions"][0]["paused"] is True
    assert main_mod.state.sessions[SID].glossary == "New glossary."
    assert client.patch(f"/sessions/{SID}", json={"accepted_identities": []}).status_code == 200
    assert client.rooms[0].remote_participants["user-alice"].track_publications["TR_a"].calls == [True, False]
    assert client.patch(f"/sessions/{SID2}", json={"paused": True}).status_code == 404
    r = client.post(f"/sessions/{SID}/finalize")
    assert r.status_code == 202 and r.json()["final_pass"] == "skipped"
    assert client.delete(f"/sessions/{SID}").status_code == 204
    assert client.delete(f"/sessions/{SID}").status_code == 204  # idempotent
    assert client.get("/health").json()["sessions"] == []


def test_start_failure_is_502(client):
    def broken():
        room = FakeRoom()
        room.fail_connect = True
        return room

    main_mod.state.services.room_factory = broken
    r = client.post("/sessions", json=start_body(session_id=SID2))
    assert r.status_code == 502
    assert SID2 not in main_mod.state.sessions


@pytest.mark.parametrize("bad", ["S1", "../..", "..%2F..", "01k6z8q3j5v7w9x1y2z3a4b5c6", "01K6Z8Q3J5V7W9X1Y2Z3A4B5CU", SID + "0", ""])
def test_session_id_must_be_a_ulid(client, tmp_path, bad):
    assert client.post("/sessions", json=start_body(session_id=bad)).status_code == 422
    assert main_mod.state.sessions == {} and client.rooms == []  # never joined, nothing on disk
    assert list(tmp_path.iterdir()) == []
    if bad and "/" not in bad and "%" not in bad:
        assert client.patch(f"/sessions/{bad}", json={"paused": True}).status_code == 422
        assert client.post(f"/sessions/{bad}/finalize").status_code == 422
        assert client.delete(f"/sessions/{bad}").status_code == 422


def test_start_timeout_never_cancels_the_pending_connect(client, monkeypatch):
    """A join slower than the start budget: 502 at once, and the late room is
    left as soon as its connect completes (no ghost participant)."""
    monkeypatch.setattr(main_mod, "START_TIMEOUT_S", 0.2)
    release = None
    rooms = []

    class SlowRoom(FakeRoom):
        cancelled = False

        async def connect(self, url, token, options=None):
            try:
                await release.wait()
            except BaseException:
                self.cancelled = True
                raise
            await super().connect(url, token, options)

    def factory():
        nonlocal release
        release = asyncio.Event()
        rooms.append(SlowRoom())
        return rooms[-1]

    main_mod.state.services.room_factory = factory
    assert client.post("/sessions", json=start_body()).status_code == 502
    assert SID not in main_mod.state.sessions
    room = rooms[0]
    assert not room.cancelled and room.connected_with is None and not room.disconnected
    client.portal.call(release.set)
    for _ in range(100):
        if room.disconnected:
            break
        time.sleep(0.01)
    assert not room.cancelled and room.connected_with == ("ws://lk", "tok") and room.disconnected


def test_sessions_that_gave_up_or_stopped_long_ago_are_retired(client):
    from agent import session as session_mod

    assert client.post("/sessions", json=start_body()).status_code == 201
    assert client.post("/sessions", json=start_body(session_id=SID2)).status_code == 201
    s1, s2 = main_mod.state.sessions[SID], main_mod.state.sessions[SID2]
    assert client.portal.call(main_mod.reap_sessions) == []
    s1.gave_up = True  # agent-token answered 404/410
    assert client.portal.call(main_mod.reap_sessions) == [SID]
    assert client.post(f"/sessions/{SID2}/finalize").status_code == 202
    time.sleep(0.2)
    assert client.portal.call(main_mod.reap_sessions) == []  # stopped, but recently
    s2._stopped_at -= session_mod.STOPPED_TTL_S  # ...and its DELETE never came
    assert client.portal.call(main_mod.reap_sessions) == [SID2]
    assert client.get("/health").json()["sessions"] == []
    for _ in range(100):
        if s1.closed and s2.closed:
            break
        time.sleep(0.01)
    assert s1.closed and s2.closed


def test_tts_503_without_tier2(client):
    r = client.post("/tts", json={"text": "Next item: budget", "voice": "am_michael"})
    assert r.status_code == 503

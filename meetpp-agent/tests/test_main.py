from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from agent import config
from agent import main as main_mod
from agent.poster import InternalApi
from tests.fakes import SECRET, FakeEngine, FakePub, FakeParticipant, FakeRoom, Recorder, silent_stream


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
        "session_id": "S1",
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
    assert s == {**s, "sid": "S1", "connected": True, "consumers": 0, "paused": False, "tier2": "off"}
    assert client.patch("/sessions/S1", json={"paused": True, "glossary": "New glossary."}).status_code == 200
    assert client.get("/health").json()["sessions"][0]["paused"] is True
    assert main_mod.state.sessions["S1"].glossary == "New glossary."
    assert client.patch("/sessions/S1", json={"accepted_identities": []}).status_code == 200
    assert client.rooms[0].remote_participants["user-alice"].track_publications["TR_a"].calls == [True, False]
    assert client.patch("/sessions/nope", json={"paused": True}).status_code == 404
    r = client.post("/sessions/S1/finalize")
    assert r.status_code == 202 and r.json()["final_pass"] == "skipped"
    assert client.delete("/sessions/S1").status_code == 204
    assert client.delete("/sessions/S1").status_code == 204  # idempotent
    assert client.get("/health").json()["sessions"] == []


def test_start_failure_is_502(client):
    def broken():
        room = FakeRoom()
        room.fail_connect = True
        return room

    main_mod.state.services.room_factory = broken
    r = client.post("/sessions", json=start_body(session_id="S2"))
    assert r.status_code == 502
    assert "S2" not in main_mod.state.sessions


def test_tts_503_without_tier2(client):
    r = client.post("/tts", json={"text": "Next item: budget", "voice": "am_michael"})
    assert r.status_code == 503

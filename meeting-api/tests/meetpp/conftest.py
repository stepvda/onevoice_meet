"""Shared Meet++ fixtures: database, fake LiveKit bus, fake agent, fake LLM."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from app.config import settings
from app.db import SessionLocal, engine
from app.meetpp import agent as agent_mod
from app.meetpp import bus, compose, llm, outline, runtime as rt, util
from app.meetpp.models import (
    MeetppRoster,
    MeetppSegment,
    MeetppSeries,
    MeetppSession,
    ensure_schema,
)
from app.models import Base, Meeting


@pytest.fixture(scope="session", autouse=True)
def _schema():
    ensure_schema(engine)
    Base.metadata.create_all(bind=engine)
    yield


class FakeBus:
    def __init__(self) -> None:
        self.sent: list[tuple[str | None, dict, list | None]] = []
        self.room_meetpp: list[tuple[str | None, dict, str | None]] = []

    async def send(self, room, msg, destination_identities=None):
        self.sent.append((room, msg, destination_identities))
        return True

    async def set_room_meetpp(self, room, payload, *, board=None):
        self.room_meetpp.append((room, payload, board))
        return True

    def of(self, mtype: str) -> list[dict]:
        return [m for _r, m, _d in self.sent if m.get("type") == mtype]

    def last(self, mtype: str) -> dict:
        found = self.of(mtype)
        assert found, f"no {mtype} message sent; got {[m.get('type') for _r, m, _d in self.sent]}"
        return found[-1]


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.finalize_ok = True
        self.health_sessions: list[dict] = []

    async def start(self, payload):
        self.calls.append(("start", payload))
        self.health_sessions = [{"sid": payload["session_id"], "connected": True}]
        return True

    async def patch(self, sid, payload):
        self.calls.append(("patch", payload))
        return True

    async def finalize(self, sid):
        self.calls.append(("finalize", sid))
        return self.finalize_ok

    async def stop(self, sid):
        self.calls.append(("stop", sid))
        return True

    async def tts(self, text, voice=None):
        self.calls.append(("tts", text))
        return {"hash": "ab" * 16, "path": "/x"}

    async def health(self):
        return {"ok": True, "sessions": self.health_sessions}

    def named(self, name: str) -> list:
        return [p for n, p in self.calls if n == name]


class FakeLLM:
    """Scripted replies per purpose (dict, str, Exception or callable)."""

    def __init__(self) -> None:
        self.replies: dict[str, list] = {}
        self.calls: list[dict] = []

    def add(self, purpose: str, *replies) -> None:
        self.replies.setdefault(purpose, []).extend(replies)

    async def complete_json(self, *, db, purpose, messages, max_tokens, temperature=0.2, session_id=None):
        self.calls.append({"purpose": purpose, "messages": messages})
        queue = self.replies.get(purpose) or []
        reply = queue.pop(0) if queue else {}
        if callable(reply) and not isinstance(reply, dict):
            reply = reply(messages)
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return llm.LLMResult(text=text, model="fake", prompt_tokens=100, completion_tokens=10, latency_ms=5)

    def prompts(self, purpose: str) -> list[str]:
        return [c["messages"][-1]["content"] for c in self.calls if c["purpose"] == purpose]


@pytest.fixture
def fakes(monkeypatch):
    fb, fa, fl = FakeBus(), FakeAgent(), FakeLLM()
    monkeypatch.setattr(bus, "send", fb.send)
    monkeypatch.setattr(bus, "set_room_meetpp", fb.set_room_meetpp)
    monkeypatch.setattr(agent_mod, "client", fa)
    monkeypatch.setattr(llm, "complete_json", fl.complete_json)
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "meetpp_enabled", True)
    monkeypatch.setattr(settings, "meetpp_internal_secret", "internal-secret")
    monkeypatch.setattr(rt.runtime, "start_session", lambda sid: None)
    monkeypatch.setattr(rt.runtime, "start_finalisation", lambda sid: None)
    scheduled: list[tuple[str, str, dict]] = []

    def _schedule(sid, section_id, **kw):
        scheduled.append((sid, section_id, kw))
        return None

    monkeypatch.setattr(compose, "schedule_section", _schedule)
    llm.reset_breakers()
    rt._last_announce.clear()

    class F:
        pass

    f = F()
    f.bus, f.agent, f.llm, f.scheduled = fb, fa, fl, scheduled
    yield f
    llm.reset_breakers()


@pytest.fixture
def api(fakes, monkeypatch, tmp_path):
    """The app over HTTP (no session resume at startup)."""
    from fastapi.testclient import TestClient

    from app.main import app

    async def _no_resume():
        return None

    monkeypatch.setattr(rt.runtime, "start", _no_resume)
    monkeypatch.setattr(settings, "meetpp_data_dir", str(tmp_path))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def meeting():
    """A meeting owned by user 42 with user 43 as co-host."""
    db = SessionLocal()
    try:
        m = Meeting(id=util.ulid(), room_name=f"room-{util.ulid().lower()}", display_title="Board meeting #9",
                    owner_user_id="42", owner_name="Chair Person", owner_email="chair@example.org", cohost_user_ids='["43"]')
        db.add(m)
        db.commit()
        return m
    finally:
        db.close()


async def drain():
    """Let fire-and-forget tasks (announcements) finish."""
    for _ in range(5):
        await asyncio.sleep(0)


def make_session(
    *,
    template: str = "agenda",
    meeting_type: str = "informal",
    owner: str = "42",
    agenda: list[dict] | None = None,
    series_id: str | None = None,
    mode: str = "lead",
) -> str:
    db = SessionLocal()
    try:
        meeting = Meeting(
            id=util.ulid(),
            room_name=f"room-{util.ulid().lower()}",
            display_title="Board meeting #9",
            owner_user_id=owner,
            owner_name="Chair Person",
            owner_email="chair@example.org",
        )
        db.add(meeting)
        if series_id is None:
            series = MeetppSeries(id=util.ulid(), owner_sub=owner, title="Riverside board", meeting_id=meeting.id, meeting_type=meeting_type)
            db.add(series)
            db.flush()
            series_id = series.id
        meeting.meetpp_series_id = series_id
        session = MeetppSession(
            id=util.ulid(),
            meeting_id=meeting.id,
            series_id=series_id,
            created_by_user_id=owner,
            status="setup",
            template=template,
            mode=mode,
            settings_json=util.dumps({"show_public": False, "in_recordings": True, "timebox_nudges": True, "speak": True}),
        )
        db.add(session)
        db.flush()
        outline.create_fixed_sections(db, session)
        if agenda:
            outline.replace_agenda(db, session, agenda)
        db.commit()
        sid = session.id
    finally:
        db.close()
    return sid


async def start(sid: str) -> None:
    await rt.start_session(sid)
    await drain()


def add_roster(series_id: str, key: str, name: str, voting: bool = True) -> None:
    db = SessionLocal()
    try:
        db.add(MeetppRoster(id=util.ulid(), series_id=series_id, person_key=key, display_name=name, voting=voting, active=True))
        db.commit()
    finally:
        db.close()


async def join(sid: str, sub: str, name: str, *, consent: str = "accept") -> None:
    identity = f"user-{sub}"
    await rt.presence(sid, [{"identity": identity, "name": name, "kind": "standard", "event": "connected"}])
    if consent:
        await rt.set_consent(sid, identity, consent, None, name)


async def say(sid: str, sub: str, name: str, text: str, *, seconds_ago: float = 0.0) -> int:
    """Ingest one tier-1 segment and return its seq."""
    uid = util.ulid()
    t0 = util.now() - timedelta(seconds=seconds_ago)
    res = await rt.ingest(
        sid,
        {"segments": [{"utterance_id": uid, "identity": f"user-{sub}", "name": name, "t_start": util.iso(t0),
                       "t_end": util.iso(t0 + timedelta(seconds=3)), "text": text, "lang": "en"}]},
    )
    return res["seqs"][uid]


def get(model, key):
    db = SessionLocal()
    try:
        return db.get(model, key)
    finally:
        db.close()


def query(fn):
    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


def sids(sid: str) -> dict[str, str]:
    """Prompt id (S1…) → section id, and section title → id."""
    db = SessionLocal()
    try:
        o = outline.load(db, sid)
        out = {f"S{i}": s.id for i, s in enumerate(o.flat, start=1)}
        out.update({s.title: s.id for s in o.flat})
        return out
    finally:
        db.close()


def segment(sid: str, seq: int) -> MeetppSegment:
    return query(lambda db: db.query(MeetppSegment).filter_by(session_id=sid, seq=seq).first())

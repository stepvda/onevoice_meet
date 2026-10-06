"""The Recording / Streaming flags of the room metadata follow what still runs
when an egress ends (reconcile_egress restarts the egress for several
transitions; the old egress's `egress_ended` arrives while the new one runs)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from livekit import api
from ulid import ULID

from app import webhooks
from app.config import settings
from app.db import SessionLocal, engine
from app.meetpp.models import ensure_schema
from app.models import Base, Meeting, Recording
from app.routes import streams
from app.services import egress_mgr


class FakeLK:
    def __init__(self, metadata: dict) -> None:
        self.metadata = json.dumps(metadata)
        self.writes = 0
        self.room = self

    async def list_rooms(self, req):
        await asyncio.sleep(0)
        return SimpleNamespace(rooms=[SimpleNamespace(metadata=self.metadata)])

    async def update_room_metadata(self, req):
        self.metadata = req.metadata
        self.writes += 1

    async def aclose(self):
        return None

    @property
    def flags(self) -> tuple[bool, bool]:
        md = json.loads(self.metadata)
        return md.get("recording_active", False), md.get("streaming_active", False)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    ensure_schema(engine)
    Base.metadata.create_all(bind=engine)


def _meeting(stream_egress: str | None, running: list[str]) -> str:
    """A meeting whose livestream egress is `stream_egress`, with a running
    Recording row per egress id in `running`."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        m = Meeting(
            id=str(ULID()), room_name=f"room-{ULID()}", display_title="Egress flags",
            owner_user_id="42", livestream_egress_id=stream_egress,
        )
        db.add(m)
        db.flush()
        for eg in running:
            db.add(Recording(
                id=str(ULID()), meeting_id=m.id, egress_id=eg, file_path=f"/tmp/{eg}.mp4",
                started_at=now, expires_at=now + timedelta(days=30), status="running",
            ))
        db.commit()
        return m.id


def _ended(egress_id: str, status=api.EgressStatus.EGRESS_COMPLETE) -> api.WebhookEvent:
    return api.WebhookEvent(
        event="egress_ended",
        egress_info=api.EgressInfo(egress_id=egress_id, status=status),
    )


@pytest.fixture
def hook(monkeypatch):
    """post(event, metadata) → the room's FakeLK after the webhook ran."""
    events: list[api.WebhookEvent] = []
    monkeypatch.setattr(webhooks, "_receiver", SimpleNamespace(receive=lambda body, auth: events.pop(0)))
    monkeypatch.setattr(settings, "whisper_url", "")
    app = FastAPI()
    app.include_router(webhooks.router)
    client = TestClient(app)

    def post(event: api.WebhookEvent, metadata: dict) -> FakeLK:
        lk = FakeLK(metadata)
        monkeypatch.setattr(egress_mgr, "livekit_api", lambda: lk)
        events.append(event)
        r = client.post("/v1/webhooks/livekit", content="{}")
        assert r.status_code == 200, r.text
        return lk

    return post


@pytest.mark.parametrize(
    "stream_egress, running, ended, before, after",
    [
        # Start streaming while recording: EG_1 (file) → EG_2 (file + stream).
        ("EG_2", ["EG_1", "EG_2"], "EG_1", (True, True), (True, True)),
        # Stop recording while streaming: EG_2 (file + stream) → EG_3 (stream).
        ("EG_3", ["EG_2"], "EG_2", (True, True), (False, True)),
        # Stop streaming while recording: EG_2 (file + stream) → EG_4 (file).
        (None, ["EG_2", "EG_4"], "EG_2", (True, True), (True, False)),
        # A recording-only egress ends.
        (None, ["EG_1"], "EG_1", (True, False), (False, False)),
        # A stream-only egress dies (no Recording row).
        ("EG_5", [], "EG_5", (False, True), (False, False)),
    ],
)
def test_egress_ended_keeps_the_flags_of_what_still_runs(hook, stream_egress, running, ended, before, after):
    tag = str(ULID())  # egress ids are unique across the test database
    stream_egress = stream_egress and f"{stream_egress}_{tag}"
    running = [f"{eg}_{tag}" for eg in running]
    ended = f"{ended}_{tag}"
    meeting_id = _meeting(stream_egress, running)
    lk = hook(_ended(ended), {"recording_active": before[0], "streaming_active": before[1], "room_layout": "grid"})
    assert lk.flags == after
    assert lk.writes == (0 if before == after else 1)
    assert json.loads(lk.metadata)["room_layout"] == "grid"
    with SessionLocal() as db:
        assert db.get(Meeting, meeting_id).livestream_egress_id == (None if stream_egress == ended else stream_egress)
        statuses = {r.egress_id: r.status for r in db.query(Recording).filter_by(meeting_id=meeting_id)}
        assert statuses == {eg: "completed" if eg == ended else "running" for eg in running}


def test_other_egress_events_leave_the_flags_alone(hook):
    egress_id = f"EG_{ULID()}"
    _meeting(egress_id, [egress_id])
    event = api.WebhookEvent(event="egress_updated", egress_info=api.EgressInfo(egress_id=egress_id))
    lk = hook(event, {"recording_active": False, "streaming_active": False})
    assert lk.writes == 0


async def test_stop_streaming_when_already_stopped_turns_a_stale_pill_off(monkeypatch):
    meeting_id = _meeting(None, [])
    lk = FakeLK({"recording_active": False, "streaming_active": True})
    monkeypatch.setattr(egress_mgr, "livekit_api", lambda: lk)
    with SessionLocal() as db:
        out = await streams.stop_stream(meeting_id, SimpleNamespace(sub="42"), db)
    assert out == {"ok": True, "already_stopped": True}
    assert lk.flags == (False, False)

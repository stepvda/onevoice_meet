"""No egress may run untracked: concurrent restarts of a meeting's egress start
one egress, the reaper stops TI-TV egresses the database doesn't track, and the
HLS watchdog starts no egress while the host is short of memory (2026-10-14:
four TI-TV composites ran at once and the host went OOM)."""
from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from livekit import api
from ulid import ULID

from app import livekit_client, scheduler
from app.config import settings
from app.db import SessionLocal, engine
from app.meetpp.models import ensure_schema
from app.models import Base, Meeting, Recording
from app.services import egress_mgr


class FakeEgressLK:
    """A LiveKit API whose egress service records starts and stops."""

    def __init__(self, active: list[api.EgressInfo] | None = None, fail_stop: tuple[str, ...] = ()) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.stop_attempts: list[str] = []
        self.active = active or []
        self.fail_stop = set(fail_stop)
        self.egress = self

    async def start_room_composite_egress(self, req):
        await asyncio.sleep(0.05)  # let a concurrent caller interleave
        egress_id = f"EG_{ULID()}"
        self.started.append(egress_id)
        return api.EgressInfo(egress_id=egress_id)

    async def stop_egress(self, req):
        await asyncio.sleep(0)
        self.stop_attempts.append(req.egress_id)
        if req.egress_id in self.fail_stop:
            raise RuntimeError("no response from servers")
        self.stopped.append(req.egress_id)
        return api.EgressInfo(egress_id=req.egress_id)

    async def list_egress(self, req):
        return SimpleNamespace(items=[e for e in self.active if e.room_name == req.room_name])

    async def aclose(self):
        return None


@pytest.fixture(scope="module", autouse=True)
def _schema():
    ensure_schema(engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture
def lk(monkeypatch):
    fake = FakeEgressLK()
    monkeypatch.setattr(egress_mgr, "livekit_api", lambda: fake)
    monkeypatch.setattr(livekit_client, "livekit_api", lambda: fake)  # the watchdog's stale stop

    async def no_metadata(*args, **kwargs):
        return None

    monkeypatch.setattr(egress_mgr, "_set_recording_metadata", no_metadata)
    monkeypatch.setattr(egress_mgr, "_orphan_stop_failed", {})
    return fake


@pytest.fixture
def titv(monkeypatch, tmp_path):
    """A streaming TI-TV channel of its own (the test database is shared)."""
    slug = f"titv-{ULID()}".lower()
    monkeypatch.setattr(settings, "titv_public_slug", slug)
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path))
    return _stream_meeting(public_slug=slug)


def _stream_meeting(*, public_slug: str | None = None, egress_id: str | None = None) -> str:
    with SessionLocal() as db:
        m = Meeting(
            id=str(ULID()), room_name=f"room-{ULID()}", display_title="Egress orphans",
            owner_user_id="42", public_slug=public_slug, livestream_egress_id=egress_id,
            livestream_enabled=True, livestream_rtmps_url="rtmps://live.example/app", livestream_stream_key="key",
        )
        db.add(m)
        db.commit()
        return m.id


def _restart(meeting_id: str):
    """What a caller does: load the meeting, then ask for a stream egress."""
    async def run():
        with SessionLocal() as db:
            m = db.get(Meeting, meeting_id)
            await asyncio.sleep(0)  # both callers read before either starts
            return await egress_mgr.reconcile_egress(
                m, want_file=False, want_stream=True, layout=None, user_sub="test", db=db,
            )
    return run()


def _pointer(meeting_id: str) -> str | None:
    with SessionLocal() as db:
        return db.get(Meeting, meeting_id).livestream_egress_id


async def test_concurrent_restarts_start_one_egress(lk):
    meeting_id = _stream_meeting()
    a, b = await asyncio.gather(_restart(meeting_id), _restart(meeting_id))
    assert len(lk.started) == 1 and lk.stopped == []
    assert a["egress_id"] == b["egress_id"] == lk.started[0] == _pointer(meeting_id)
    assert sorted([a["no_change"], b["no_change"]]) == [False, True]


def test_restarts_from_a_scheduler_thread_and_the_event_loop_start_one_egress(lk):
    """The watchdogs run in APScheduler threads, each with its own loop."""
    meeting_id = _stream_meeting()
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(asyncio.run(_restart(meeting_id)))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(results) == 2 and len(lk.started) == 1
    assert _pointer(meeting_id) == lk.started[0]


async def test_a_held_lock_times_out_with_409(monkeypatch):
    monkeypatch.setattr(egress_mgr, "EGRESS_LOCK_TIMEOUT_SECONDS", 0.1)
    async with egress_mgr.egress_lock("held"):
        with pytest.raises(egress_mgr.HTTPException) as exc:
            async with egress_mgr.egress_lock("held"):
                pass
    assert exc.value.status_code == 409
    async with egress_mgr.egress_lock("held"):  # released after the timeout
        pass


def _info(room: str, egress_id: str, age_s: float, status=api.EgressStatus.EGRESS_ACTIVE) -> api.EgressInfo:
    return api.EgressInfo(
        egress_id=egress_id, room_name=room, status=status,
        started_at=time.time_ns() - int(age_s * 1_000_000_000),
    )


async def test_reaper_stops_only_old_untracked_egresses(lk):
    tracked, recording = f"EG_{ULID()}", f"EG_{ULID()}"
    meeting_id = _stream_meeting(egress_id=tracked)
    with SessionLocal() as db:
        m = db.get(Meeting, meeting_id)
        room = m.room_name
        db.add(Recording(
            id=str(ULID()), meeting_id=m.id, egress_id=recording, file_path="/tmp/r.mp4",
            started_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc), status="running",
        ))
        db.commit()
    orphan, young, ending, other_room = (f"EG_{ULID()}" for _ in range(4))
    lk.active = [
        _info(room, tracked, 86400),
        _info(room, recording, 86400),
        _info(room, orphan, 86400),
        _info(room, young, 30),
        _info(room, ending, 86400, status=api.EgressStatus.EGRESS_ENDING),
        _info("another-room", other_room, 86400),
    ]
    with SessionLocal() as db:
        stopped = await egress_mgr.reap_orphan_egresses(db, db.get(Meeting, meeting_id))
    assert stopped == [orphan] and lk.stopped == [orphan]


async def test_reaper_retries_a_failed_stop_only_after_an_hour(lk):
    meeting_id = _stream_meeting(egress_id=f"EG_{ULID()}")
    dead = f"EG_{ULID()}"
    with SessionLocal() as db:
        room = db.get(Meeting, meeting_id).room_name
    lk.active = [_info(room, dead, 86400)]
    lk.fail_stop = {dead}
    for _ in range(3):
        with SessionLocal() as db:
            assert await egress_mgr.reap_orphan_egresses(db, db.get(Meeting, meeting_id)) == []
    assert lk.stop_attempts == [dead]
    egress_mgr._orphan_stop_failed[dead] -= egress_mgr.ORPHAN_RETRY_SECONDS + 1
    lk.fail_stop = set()
    with SessionLocal() as db:
        assert await egress_mgr.reap_orphan_egresses(db, db.get(Meeting, meeting_id)) == [dead]


def test_reaper_job_covers_the_titv_room(lk, titv):
    with SessionLocal() as db:
        room = db.get(Meeting, titv).room_name
    orphan = f"EG_{ULID()}"
    lk.active = [_info(room, orphan, 86400)]
    scheduler._egress_orphan_reaper_job()
    assert lk.stopped == [orphan]


def test_watchdog_starts_no_egress_while_memory_is_short(lk, titv, monkeypatch):
    monkeypatch.setattr(egress_mgr, "host_mem_available_mb", lambda: 300)
    scheduler._hls_egress_watchdog_job()
    assert lk.started == [] and _pointer(titv) is None

    monkeypatch.setattr(egress_mgr, "host_mem_available_mb", lambda: 4000)
    scheduler._hls_egress_watchdog_job()
    assert len(lk.started) == 1 and _pointer(titv) == lk.started[0]


def test_watchdog_stops_a_stale_egress_even_while_memory_is_short(lk, titv, monkeypatch, tmp_path):
    stale = f"EG_{ULID()}"
    with SessionLocal() as db:
        db.get(Meeting, titv).livestream_egress_id = stale
        db.commit()
    playlist = tmp_path / "hls" / settings.titv_public_slug / "live.m3u8"
    playlist.parent.mkdir(parents=True)
    playlist.write_text("#EXTM3U\n")
    old = time.time() - settings.hls_watchdog_stale_seconds - 60
    os.utime(playlist, (old, old))
    monkeypatch.setattr(egress_mgr, "host_mem_available_mb", lambda: 300)
    scheduler._hls_egress_watchdog_job()
    assert lk.stopped == [stale] and lk.started == [] and _pointer(titv) is None


def test_host_memory_reads_meminfo_or_none():
    available = egress_mgr.host_mem_available_mb()
    assert available is None if not os.path.exists("/proc/meminfo") else available > 0

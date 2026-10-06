"""The stage: presenter moves (app/stage.py), serialised room-metadata writes
(app/room_metadata.py) and the webhook hand-over to screen shares."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from livekit import api

from app import stage
from app.room_metadata import patch_room_metadata


class FakeLK:
    """Just enough of LiveKitAPI.room; yields between read and write so that
    unserialised read-modify-writes would lose updates."""

    def __init__(self, metadata: dict | None = None, exists: bool = True):
        self.metadata = json.dumps(metadata or {})
        self.exists = exists
        self.writes = 0
        self.room = self

    async def list_rooms(self, req):
        await asyncio.sleep(0)
        rooms = [SimpleNamespace(metadata=self.metadata)] if self.exists else []
        return SimpleNamespace(rooms=rooms)

    async def update_room_metadata(self, req):
        await asyncio.sleep(0)
        self.metadata = req.metadata
        self.writes += 1

    async def aclose(self):
        return None

    @property
    def md(self) -> dict:
        return json.loads(self.metadata)


def test_board_is_presenter_while_meetpp_runs_and_shares_hand_it_back():
    md: dict = {"presenter_identity": "user-alice"}
    assert stage.board_started(md) and md["presenter_identity"] == stage.BOARD_KEY
    # Someone shares: the share takes the stage, the board comes back after it.
    assert stage.stream_started(md, "user-bob#screen")
    assert md == {"presenter_identity": "user-bob#screen", "presenter_prev": stage.BOARD_KEY}
    # A second share takes over; the board is still what comes back.
    assert stage.stream_started(md, "user-carol#screen")
    assert md["presenter_prev"] == stage.BOARD_KEY
    assert stage.stream_stopped(md, "user-carol#screen")
    assert md == {"presenter_identity": stage.BOARD_KEY}
    # Meet++ ends: the board leaves the stage.
    assert stage.board_ended(md) and md["presenter_identity"] is None


def test_no_presenter_no_move_and_the_hosts_choice_stands():
    md: dict = {}
    assert not stage.stream_started(md, "user-bob#screen")
    assert not stage.stream_stopped(md, "user-bob#screen")
    md = {"presenter_identity": "user-alice"}
    stage.stream_started(md, stage.PLAYBACK_KEY)
    assert md == {"presenter_identity": "playback", "presenter_prev": "user-alice"}
    # The host presents the board during the playback: nothing comes back later.
    stage.chosen(md, stage.BOARD_KEY)
    assert md == {"presenter_identity": stage.BOARD_KEY}
    assert not stage.stream_stopped(md, stage.PLAYBACK_KEY)


def test_meetpp_starting_during_a_share_follows_the_share():
    md = {"presenter_identity": "user-bob#screen", "presenter_prev": "user-alice"}
    assert stage.board_started(md)
    assert md == {"presenter_identity": "user-bob#screen", "presenter_prev": stage.BOARD_KEY}
    assert stage.board_ended(md) and md == {"presenter_identity": "user-bob#screen"}


async def test_concurrent_metadata_patches_are_serialised():
    lk = FakeLK({"room_layout": "grid"})
    await asyncio.gather(
        patch_room_metadata(lk, "r1", lambda md: md.update(recording_active=True)),
        patch_room_metadata(lk, "r1", lambda md: stage.chosen(md, stage.BOARD_KEY)),
        patch_room_metadata(lk, "r1", lambda md: md.update(pip_enabled=False)),
    )
    assert lk.md == {
        "room_layout": "grid", "recording_active": True,
        "presenter_identity": stage.BOARD_KEY, "pip_enabled": False,
    }
    # A change returning False writes nothing; a missing room is left alone.
    assert await patch_room_metadata(lk, "r1", lambda md: False) == lk.md and lk.writes == 3
    gone = FakeLK(exists=False)
    assert await patch_room_metadata(gone, "r2", lambda md: md.update(x=1), require_room=True) is None
    assert gone.writes == 0


async def test_webhook_hands_the_stage_to_a_screen_share(monkeypatch):
    from app import webhooks

    assert webhooks._stage_key("user-bob", api.TrackSource.SCREEN_SHARE) == "user-bob#screen"
    assert webhooks._stage_key("playback", api.TrackSource.CAMERA) == "playback"
    assert webhooks._stage_key("user-bob", api.TrackSource.CAMERA) is None
    assert webhooks._stage_key("composite-room", api.TrackSource.SCREEN_SHARE) is None

    lk = FakeLK({"presenter_identity": stage.BOARD_KEY})
    monkeypatch.setattr(webhooks, "livekit_api", lambda: lk)
    await webhooks._stage_stream("room", "user-bob#screen", True)
    assert lk.md["presenter_identity"] == "user-bob#screen"
    await webhooks._stage_stream("room", "user-bob#screen", False)
    assert lk.md == {"presenter_identity": stage.BOARD_KEY}

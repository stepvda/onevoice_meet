"""Server-published LiveKit data messages (topic `meet-ai`, RELIABLE).

Every Meet++ real-time message goes through `send()`, so tests replace that
single function. Envelope: {"v": 1, "type": ..., "sid": ...}. State messages
larger than 8 KB are sent without their delta (clients then refetch).
Viewers of the public page (`viewer-` identities) only receive room-wide
messages while the session is shown publicly.
"""
from __future__ import annotations

import json
import logging
import time

from livekit import api as lk_api

from app.livekit_client import livekit_api

log = logging.getLogger("app.meetpp")

TOPIC = "meet-ai"
MAX_STATE_BYTES = 8 * 1024
MAX_ACTIVATIONS = 3
PARTICIPANTS_TTL_SECONDS = 3.0
VIEWER_PREFIX = "viewer-"

_client = None
# room → (monotonic time, participant identities), refreshed at most every 3 s.
_participants: dict[str, tuple[float, list[str]]] = {}
# room → the session is shown on the public page (room metadata `public`).
_public: dict[str, bool] = {}


def _lk():
    global _client
    if _client is None:
        _client = livekit_api()
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001
            pass
        _client = None


def encode(msg: dict) -> bytes:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _room_public(room: str) -> bool:
    if room not in _public:
        # Not set since this process started: read the session's setting.
        from app.db import SessionLocal
        from app.meetpp.models import MeetppSession
        from app.models import Meeting

        db = SessionLocal()
        try:
            session = (
                db.query(MeetppSession)
                .join(Meeting, Meeting.id == MeetppSession.meeting_id)
                .filter(Meeting.room_name == room, MeetppSession.status.in_(("running", "paused", "finalising")))
                .first()
            )
            st = json.loads(session.settings_json or "{}") if session is not None else {}
            _public[room] = bool(st.get("show_public", False))
        except Exception:  # noqa: BLE001
            return False
        finally:
            db.close()
    return _public[room]


async def _audience(room: str) -> list[str] | None:
    """Destinations of a room-wide message: None (everyone) while the session
    is public or no viewer is in the room, else every participant but the
    viewers. When the room cannot be listed and nothing is cached, everyone."""
    if _room_public(room):
        return None
    now = time.monotonic()
    cached = _participants.get(room)
    if cached is None or now - cached[0] >= PARTICIPANTS_TTL_SECONDS:
        try:
            res = await _lk().room.list_participants(lk_api.ListParticipantsRequest(room=room))
            cached = (now, [p.identity for p in res.participants])
            for r, (at, _ids) in list(_participants.items()):
                if now - at > 60:
                    del _participants[r]
            _participants[room] = cached
        except Exception as exc:  # noqa: BLE001
            if cached is None:
                log.warning("meetpp: cannot list the participants of %s, broadcasting to all: %s", room, exc)
                return None
    identities = cached[1]
    if not any(i.startswith(VIEWER_PREFIX) for i in identities):
        return None
    return [i for i in identities if not i.startswith(VIEWER_PREFIX)]


async def send(room: str | None, msg: dict, destination_identities: list[str] | None = None) -> bool:
    """Publish one message to the room. Never raises."""
    global _client
    if not room:
        return False
    try:
        if not destination_identities:
            destination_identities = await _audience(room)
            if destination_identities == []:
                return True  # only viewers in the room
        req = lk_api.SendDataRequest(
            room=room,
            data=encode(msg),
            kind=lk_api.DataPacket.Kind.RELIABLE,
            topic=TOPIC,
            destination_identities=list(destination_identities or []),
        )
        await _lk().room.send_data(req)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("meetpp: broadcast %s failed: %s", msg.get("type"), exc)
        # Drop the client so the next call reconnects.
        old, _client = _client, None
        if old is not None:
            try:
                await old.aclose()
            except Exception:  # noqa: BLE001
                pass
        return False


def message(mtype: str, sid: str, **payload) -> dict:
    return {"v": 1, "type": mtype, "sid": sid, **payload}


def state_message(sid: str, version: int, delta: dict | None, activations: list[dict]) -> dict:
    """`state` message with at most 3 activations (highest priority first) and
    the delta dropped when the payload would exceed 8 KB."""
    acts = sorted(activations or [], key=lambda a: -int(a.get("prio") or 0))[:MAX_ACTIVATIONS]
    msg = message("state", sid, version=version, activations=acts)
    if delta:
        with_delta = dict(msg, delta=delta)
        if len(encode(with_delta)) <= MAX_STATE_BYTES:
            return with_delta
    return msg


async def publish_changes(db, session, changes, *, room: str | None = None, lead=None) -> int | None:
    """Bump the state version atomically, commit, and broadcast the delta and
    activations. Returns the new version (None when nothing changed).

    `lead(version) -> [message]` are sent first with the same version (the
    `position` message precedes its `state` delta)."""
    from app.meetpp import ops
    from app.models import Meeting

    if not changes.any() and not changes.activations:
        db.commit()
        return None
    version = ops.bump_version(db, session)
    db.commit()
    delta = ops.build_delta(db, session, changes)
    if room is None:
        meeting = db.get(Meeting, session.meeting_id)
        room = meeting.room_name if meeting else None
    for msg in (lead(version) if lead else []):
        await send(room, msg)
    await send(room, state_message(session.id, version, delta, ops.sorted_activations(changes)))
    return version


async def set_room_meetpp(room: str | None, payload: dict, *, board: str | None = None) -> bool:
    """Mirror the session in the LiveKit room metadata (`meetpp` key). With
    `board="start"` the board becomes the presenter (a session starting), with
    `board="end"` it leaves the stage (app/stage.py). Never raises."""
    if not room:
        return False
    if "public" in payload:
        _public[room] = bool(payload["public"])
    from app import stage
    from app.room_metadata import patch_room_metadata

    def change(current: dict) -> None:
        current["meetpp"] = {**(current.get("meetpp") or {}), **payload}
        if board == "start":
            stage.board_started(current)
        elif board == "end":
            stage.board_ended(current)

    try:
        return await patch_room_metadata(_lk(), room, change, require_room=True) is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("meetpp: room metadata update failed: %s", exc)
        return False

"""
Waiting room workflow.

When `meeting.waiting_room_enabled` is on, non-owner joiners hitting
`POST /v1/rooms/{room_name}/anon-token` (in `tokens.py`) get a 202 with a
`wait_token` instead of a LiveKit token. They then poll
`GET /v1/rooms/{room_name}/wait/{wait_token}` until the owner admits or
denies them via the endpoints in this module.

Storage: a single Redis hash per pending request,
    key  = meet:pending:{meeting_id}:{wait_token}
    TTL  = 15 minutes (enough for a slow approver)
    fields = display_name, email, requested_at, status,
             livekit_token (only set once admitted), ice_servers (JSON)
"""
import json
import time
from typing import Literal

import redis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ulid import ULID

from app.auth import RequireUser
from app.config import settings
from app.db import get_db
from app.livekit_client import mint_participant_token, short_lived_turn_credentials
from app.models import Meeting, MeetingParticipant
from app.routes.meetings import is_moderator

router = APIRouter(prefix="/v1")

_redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)

_PENDING_TTL_SECONDS = 15 * 60


def _pending_key(meeting_id: str, wait_token: str) -> str:
    return f"meet:pending:{meeting_id}:{wait_token}"


def _pending_pattern(meeting_id: str) -> str:
    return f"meet:pending:{meeting_id}:*"


def enqueue_pending(
    *,
    meeting_id: str,
    display_name: str,
    email: str | None,
) -> str:
    """Create a pending row and return the wait_token. Caller is responsible
    for telling the joiner what URL to poll."""
    wait_token = str(ULID())
    payload = {
        "display_name": display_name,
        "email": email or "",
        "requested_at": str(int(time.time())),
        "status": "pending",
    }
    key = _pending_key(meeting_id, wait_token)
    _redis.hset(key, mapping=payload)
    _redis.expire(key, _PENDING_TTL_SECONDS)
    return wait_token


def _read_pending(meeting_id: str, wait_token: str) -> dict | None:
    data = _redis.hgetall(_pending_key(meeting_id, wait_token))
    return data or None


# ─── Joiner-side polling ──────────────────────────────────────────────


class WaitOut(BaseModel):
    status: Literal["pending", "admitted", "denied", "unknown"]
    livekit_url: str | None = None
    token: str | None = None
    ice_servers: dict | None = None
    room_name: str | None = None


@router.get("/rooms/{room_name}/wait/{wait_token}")
def poll_wait(room_name: str, wait_token: str, db: Session = Depends(get_db)) -> WaitOut:
    m = db.query(Meeting).filter_by(room_name=room_name).first()
    if not m:
        raise HTTPException(status_code=404, detail="room not found")
    data = _read_pending(m.id, wait_token)
    if not data:
        return WaitOut(status="unknown")
    status_ = data.get("status", "pending")
    if status_ == "admitted":
        ice = data.get("ice_servers")
        return WaitOut(
            status="admitted",
            livekit_url=settings.livekit_ws_url,
            token=data.get("livekit_token"),
            ice_servers=json.loads(ice) if ice else None,
            room_name=m.room_name,
        )
    if status_ == "denied":
        return WaitOut(status="denied")
    return WaitOut(status="pending")


# ─── Owner-side approval ──────────────────────────────────────────────


class PendingJoinerOut(BaseModel):
    wait_token: str
    display_name: str
    email: str | None
    requested_at: int


def _require_owner(meeting_id: str, user_sub: str, db: Session) -> Meeting:
    """Owner or co-host."""
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not is_moderator(m, user_sub):
        raise HTTPException(status_code=404, detail="meeting not found")
    return m


@router.get("/meetings/{meeting_id}/pending")
def list_pending(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> list[PendingJoinerOut]:
    _require_owner(meeting_id, user.sub, db)
    out: list[PendingJoinerOut] = []
    for key in _redis.scan_iter(match=_pending_pattern(meeting_id), count=200):
        data = _redis.hgetall(key)
        if not data or data.get("status") != "pending":
            continue
        wait_token = key.rsplit(":", 1)[-1]
        try:
            req_at = int(data.get("requested_at", "0"))
        except ValueError:
            req_at = 0
        out.append(
            PendingJoinerOut(
                wait_token=wait_token,
                display_name=data.get("display_name", ""),
                email=data.get("email") or None,
                requested_at=req_at,
            )
        )
    out.sort(key=lambda p: p.requested_at)
    return out


@router.post("/meetings/{meeting_id}/pending/{wait_token}/admit")
def admit_pending(
    meeting_id: str,
    wait_token: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    data = _read_pending(meeting_id, wait_token)
    if not data:
        raise HTTPException(status_code=404, detail="pending request not found")
    if data.get("status") != "pending":
        # Already resolved — let the caller see the current state without erroring.
        return {"ok": True, "status": data.get("status")}

    display_name = data.get("display_name") or "Guest"
    identity = f"anon-{ULID()}"
    # Honour the meeting's policy for the admitted joiner too (auto-mute,
    # auto-disable-camera, screenshare restriction). Mirrors anon_token's
    # behaviour so admitted joiners can't bypass moderation just by going
    # through the waiting room.
    join_meta: dict = {}
    if m.auto_mute_new_joiners:
        join_meta["auto_mute"] = True
    if m.auto_disable_camera_for_new:
        join_meta["auto_disable_camera"] = True
    token = mint_participant_token(
        room_name=m.room_name,
        identity=identity,
        display_name=display_name,
        is_owner=False,
        metadata=join_meta or None,
        allow_screenshare=bool(m.allow_participant_screenshare),
    )
    ice = short_lived_turn_credentials(identity)

    key = _pending_key(meeting_id, wait_token)
    _redis.hset(
        key,
        mapping={
            "status": "admitted",
            "livekit_token": token,
            "ice_servers": json.dumps(ice or {}),
        },
    )
    # Shorter TTL once admitted — joiner only needs a few seconds to poll
    # and pick the token up. Don't leave it lingering with the token.
    _redis.expire(key, 60)

    db.add(
        MeetingParticipant(
            meeting_id=m.id,
            livekit_identity=identity,
            display_name=display_name,
            email=data.get("email") or None,
            is_authenticated=False,
            is_owner=False,
        )
    )
    db.commit()
    return {"ok": True, "status": "admitted"}


@router.post("/meetings/{meeting_id}/pending/{wait_token}/deny")
def deny_pending(
    meeting_id: str,
    wait_token: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    _require_owner(meeting_id, user.sub, db)
    data = _read_pending(meeting_id, wait_token)
    if not data:
        raise HTTPException(status_code=404, detail="pending request not found")
    key = _pending_key(meeting_id, wait_token)
    _redis.hset(key, mapping={"status": "denied"})
    _redis.expire(key, 60)
    return {"ok": True, "status": "denied"}

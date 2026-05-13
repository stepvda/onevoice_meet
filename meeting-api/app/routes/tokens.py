import time

import redis
from fastapi import APIRouter, Depends, HTTPException, Request
from passlib.hash import argon2
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ulid import ULID

from app.auth import OptionalUser
from app.config import settings
from app.db import get_db
from app.livekit_client import mint_participant_token, short_lived_turn_credentials
from app.models import Meeting, MeetingFeedback, MeetingParticipant
from app.routes.waiting_room import enqueue_pending

router = APIRouter(prefix="/v1")

_redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)


class AnonTokenBody(BaseModel):
    display_name: str = Field(default="", max_length=80)
    email: EmailStr | None = None
    password: str | None = None


def _rate_limit(ip: str) -> None:
    key = f"anon_token:{ip}"
    now = int(time.time())
    window = 3600
    # Simple sliding window: ZSET of timestamps.
    try:
        pipe = _redis.pipeline()
        pipe.zremrangebyscore(key, 0, now - window)
        pipe.zcard(key)
        pipe.zadd(key, {str(now) + ":" + str(ULID()): now})
        pipe.expire(key, window)
        _, count, _, _ = pipe.execute()
    except redis.RedisError:
        # If Redis is down, fail open rather than blocking all joins.
        return
    if count >= settings.anon_token_rate_per_hour:
        raise HTTPException(status_code=429, detail="too many anonymous join requests")


@router.post("/rooms/{room_name}/anon-token")
def anon_token(
    room_name: str,
    body: AnonTokenBody,
    request: Request,
    auth_user: OptionalUser = None,
    db: Session = Depends(get_db),
) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    _rate_limit(client_ip)

    m = db.query(Meeting).filter_by(room_name=room_name).first()
    if not m:
        raise HTTPException(status_code=404, detail="room not found")
    if not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")

    if m.require_password:
        if not body.password or not m.password_hash or not argon2.verify(body.password, m.password_hash):
            raise HTTPException(status_code=401, detail="incorrect password")

    # Moderation: name required when policy is set.
    if m.require_name_on_join and not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display name is required for this meeting")

    # Moderation: lock room once the owner has joined for the first time.
    # `MeetingParticipant.is_owner=True` is written when the owner mints
    # their token; presence of any such row means the meeting has started.
    if m.lock_room_after_start:
        owner_joined = (
            db.query(MeetingParticipant)
            .filter_by(meeting_id=m.id, is_owner=True)
            .first()
            is not None
        )
        if owner_joined:
            raise HTTPException(status_code=403, detail="meeting is locked")

    # Moderation: waiting room — short-circuit without minting a LiveKit
    # token. The joiner gets a wait_token they poll until the owner admits
    # them (see app/routes/waiting_room.py).
    if m.waiting_room_enabled:
        wait_token = enqueue_pending(
            meeting_id=m.id,
            display_name=body.display_name,
            email=str(body.email) if body.email else None,
        )
        return {
            "status": "waiting",
            "wait_token": wait_token,
            "room_name": m.room_name,
        }

    # Signed-in joiners get `user-<sub>` as their LiveKit identity so the
    # owner's participants panel can match them against the cohost set
    # (which is keyed on user.sub). Anonymous joiners still get a fresh
    # ULID-suffixed `anon-` identity — they have no stable id to elevate.
    is_authenticated = auth_user is not None
    identity = f"user-{auth_user.sub}" if is_authenticated else f"anon-{ULID()}"
    # Auto-mute / auto-disable-camera: stamp the participant's join metadata
    # so the SPA reads it on connect and starts with mic/camera off.
    join_meta: dict = {}
    if m.auto_mute_new_joiners:
        join_meta["auto_mute"] = True
    if m.auto_disable_camera_for_new:
        join_meta["auto_disable_camera"] = True
    token = mint_participant_token(
        room_name=m.room_name,
        identity=identity,
        display_name=body.display_name,
        is_owner=False,
        metadata=join_meta or None,
        allow_screenshare=m.allow_participant_screenshare,
    )
    # Authenticated joiners (identity `user-<sub>`) can hit anon-token a
    # second time on refresh / reconnect — the partial unique index on
    # active rows would then 500. For anonymous joiners (`anon-<ULID>`)
    # the identity is fresh per call so a collision can't happen, but
    # the same defensive pattern is harmless. See the mirror fix in
    # meetings.mint_owner_token.
    existing = (
        db.query(MeetingParticipant)
        .filter_by(meeting_id=m.id, livekit_identity=identity, left_at=None)
        .first()
    )
    if existing is None:
        db.add(
            MeetingParticipant(
                meeting_id=m.id,
                livekit_identity=identity,
                display_name=body.display_name,
                email=str(body.email) if body.email else None,
                is_authenticated=is_authenticated,
                is_owner=False,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    else:
        existing.display_name = body.display_name
        if body.email:
            existing.email = str(body.email)
        db.commit()
    return {
        "livekit_url": settings.livekit_ws_url,
        "token": token,
        "room_name": m.room_name,
        "ice_servers": short_lived_turn_credentials(identity),
        "policy": {
            "auto_mute": bool(m.auto_mute_new_joiners),
            "auto_disable_camera": bool(m.auto_disable_camera_for_new),
            "allow_screenshare": bool(m.allow_participant_screenshare),
            "allow_chat": bool(m.allow_participant_chat),
        },
    }


class FeedbackBody(BaseModel):
    rating: int = Field(ge=0, le=10)
    comment: str | None = Field(default=None, max_length=2000)
    participant_identity: str | None = Field(default=None, max_length=200)
    participant_name: str | None = Field(default=None, max_length=200)


@router.post("/rooms/{room_name}/feedback", status_code=201)
def post_feedback(
    room_name: str,
    body: FeedbackBody,
    db: Session = Depends(get_db),
) -> dict:
    """Anonymous post-meeting feedback. Accepts ratings even after a meeting
    is closed so late-arriving submissions aren't dropped."""
    m = db.query(Meeting).filter_by(room_name=room_name).first()
    if not m:
        raise HTTPException(status_code=404, detail="room not found")
    db.add(
        MeetingFeedback(
            meeting_id=m.id,
            participant_identity=body.participant_identity,
            participant_name=body.participant_name,
            rating=body.rating,
            comment=(body.comment or "").strip() or None,
        )
    )
    db.commit()
    return {"ok": True}

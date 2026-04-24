import time

import redis
from fastapi import APIRouter, Depends, HTTPException, Request
from passlib.hash import argon2
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from ulid import ULID

from app.config import settings
from app.db import get_db
from app.livekit_client import mint_participant_token, short_lived_turn_credentials
from app.models import Meeting, MeetingParticipant

router = APIRouter(prefix="/v1")

_redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)


class AnonTokenBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
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

    identity = f"anon-{ULID()}"
    token = mint_participant_token(
        room_name=m.room_name,
        identity=identity,
        display_name=body.display_name,
        is_owner=False,
    )
    db.add(
        MeetingParticipant(
            meeting_id=m.id,
            livekit_identity=identity,
            display_name=body.display_name,
            email=str(body.email) if body.email else None,
            is_authenticated=False,
            is_owner=False,
        )
    )
    db.commit()
    return {
        "livekit_url": settings.livekit_ws_url,
        "token": token,
        "room_name": m.room_name,
        "ice_servers": short_lived_turn_credentials(identity),
    }

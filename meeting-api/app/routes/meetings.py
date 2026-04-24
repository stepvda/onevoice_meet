import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from passlib.hash import argon2
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ulid import ULID

from app.auth import AuthUser, RequireUser
from app.config import settings
from app.db import get_db
from app.livekit_client import livekit_api, mint_participant_token, short_lived_turn_credentials
from app.models import Meeting, MeetingParticipant

router = APIRouter(prefix="/v1")


class CreateMeetingBody(BaseModel):
    display_title: str = Field(min_length=1, max_length=200)
    scheduled_at: datetime | None = None
    ends_at: datetime | None = None
    max_participants: int = Field(default=50, ge=2, le=50)
    password: str | None = None
    recording_mode: str = Field(default="manual", pattern="^(manual|auto_on_start|off)$")


class MeetingOut(BaseModel):
    id: str
    room_name: str
    display_title: str
    owner_user_id: str
    created_at: datetime
    scheduled_at: datetime | None
    ends_at: datetime | None
    max_participants: int
    require_password: bool
    recording_mode: str
    is_active: bool


def _to_out(m: Meeting) -> MeetingOut:
    return MeetingOut(
        id=m.id,
        room_name=m.room_name,
        display_title=m.display_title,
        owner_user_id=m.owner_user_id,
        created_at=m.created_at,
        scheduled_at=m.scheduled_at,
        ends_at=m.ends_at,
        max_participants=m.max_participants,
        require_password=m.require_password,
        recording_mode=m.recording_mode,
        is_active=m.is_active,
    )


def _fresh_room_name(db: Session) -> str:
    # 12-char URL-safe slug, collision-checked.
    for _ in range(8):
        candidate = secrets.token_urlsafe(9)[:12]
        if not db.query(Meeting).filter_by(room_name=candidate).first():
            return candidate
    raise HTTPException(status_code=500, detail="could not generate unique room name")


@router.post("/meetings", status_code=201)
def create_meeting(body: CreateMeetingBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    meeting = Meeting(
        id=str(ULID()),
        room_name=_fresh_room_name(db),
        display_title=body.display_title,
        owner_user_id=user.user_id,
        owner_email=user.email,
        scheduled_at=body.scheduled_at,
        ends_at=body.ends_at,
        max_participants=body.max_participants,
        require_password=bool(body.password),
        password_hash=argon2.hash(body.password) if body.password else None,
        recording_mode=body.recording_mode,
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)

    # Pre-create the room in LiveKit so auto_create=false doesn't reject joins.
    # Fire-and-forget; the room is also recreated on demand if missing.
    try:
        api = livekit_api()
        # livekit-api is async; in sync route, we schedule via httpx call or skip.
        # For now, leave auto_create=true on LiveKit side as a pragmatic default
        # and revisit this once LiveKit async client is wired in.
    except Exception:  # noqa: BLE001
        pass

    return {
        "meeting": _to_out(meeting).model_dump(),
        "join_url": f"{settings.public_url}/j/{meeting.room_name}",
    }


@router.get("/meetings")
def list_my_meetings(user: RequireUser, db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(Meeting)
        .filter_by(owner_user_id=user.user_id)
        .order_by(Meeting.created_at.desc())
        .all()
    )
    return [_to_out(m).model_dump() for m in rows]


@router.get("/meetings/{meeting_id}")
def get_meeting(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    return _to_out(m).model_dump()


@router.delete("/meetings/{meeting_id}", status_code=204)
async def end_meeting(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> None:
    """End the meeting for everyone — closes the LiveKit room (kicks all participants),
    marks our DB row inactive."""
    from livekit import api as lkapi  # local to avoid import cost in the hot path

    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")

    lk = livekit_api()
    try:
        try:
            await lk.room.delete_room(lkapi.DeleteRoomRequest(room=m.room_name))
        except Exception:  # noqa: BLE001 — room may not exist (no one ever joined)
            pass
    finally:
        await lk.aclose()

    m.is_active = False
    m.closed_at = datetime.utcnow()
    db.commit()


@router.post("/meetings/{meeting_id}/token")
def mint_owner_token(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    if not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")
    identity = f"user-{user.user_id}"
    display_name = user.email or f"Owner {user.user_id}"
    token = mint_participant_token(
        room_name=m.room_name,
        identity=identity,
        display_name=display_name,
        is_owner=True,
    )
    db.add(
        MeetingParticipant(
            meeting_id=m.id,
            livekit_identity=identity,
            display_name=display_name,
            email=user.email,
            is_authenticated=True,
            is_owner=True,
        )
    )
    db.commit()
    return {
        "livekit_url": settings.livekit_ws_url,
        "token": token,
        "room_name": m.room_name,
        "ice_servers": short_lived_turn_credentials(identity),
    }

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from passlib.hash import argon2
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ulid import ULID

from app.auth import AuthUser, RequireUser
from app.config import settings
from app.db import get_db
from app.livekit_client import livekit_api, mint_participant_token, short_lived_turn_credentials
from app.models import Meeting, MeetingParticipant
from app.services.slug_words import generate_unique_slug

router = APIRouter(prefix="/v1")


class CreateMeetingBody(BaseModel):
    display_title: str = Field(min_length=1, max_length=200)
    scheduled_at: datetime | None = None
    ends_at: datetime | None = None
    max_participants: int = Field(default=50, ge=2, le=50)
    password: str | None = None
    recording_mode: str = Field(default="manual", pattern="^(manual|auto_on_start|off)$")
    list_for_authenticated: bool = False
    list_for_anonymous: bool = False


class UpdateMeetingBody(BaseModel):
    """Partial update — owner-only. Fields left unset are not changed."""
    display_title: str | None = Field(default=None, min_length=1, max_length=200)
    list_for_authenticated: bool | None = None
    list_for_anonymous: bool | None = None
    recording_mode: str | None = Field(default=None, pattern="^(manual|auto_on_start|off)$")


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
    branding_url: str | None = None
    list_for_authenticated: bool = False
    list_for_anonymous: bool = False


def _branding_url(m: Meeting) -> str | None:
    if not m.branding_image_path:
        return None
    # Public, served via the room-name path so the lobby can show it pre-auth.
    return f"{settings.public_url}/api/v1/rooms/{m.room_name}/branding"


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
        branding_url=_branding_url(m),
        list_for_authenticated=bool(m.list_for_authenticated),
        list_for_anonymous=bool(m.list_for_anonymous),
    )


def _to_public_out(m: Meeting) -> dict:
    """Subset of fields safe to expose without auth (the room_name is needed
    to build the join URL; owner_user_id and password hash are NEVER
    exposed)."""
    return {
        "room_name": m.room_name,
        "display_title": m.display_title,
        "max_participants": m.max_participants,
        "require_password": bool(m.require_password),
        "branding_url": _branding_url(m),
    }


def _fresh_room_name(db: Session) -> str:
    """Three-word dash-separated slug (e.g. `happy-blue-tiger`).

    The shareable URL renders as `https://meet.witysk.org/happy-blue-tiger`
    without a leading `/j/` — see the `/:roomName` route in the SPA. Old
    rooms created with 12-char random slugs still work via the same routes.
    """
    def exists(slug: str) -> bool:
        return db.query(Meeting).filter_by(room_name=slug).first() is not None

    try:
        return generate_unique_slug(exists)
    except RuntimeError as e:  # pragma: no cover — only if the word pool is exhausted
        raise HTTPException(status_code=500, detail=str(e)) from e


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
        list_for_authenticated=body.list_for_authenticated or body.list_for_anonymous,
        list_for_anonymous=body.list_for_anonymous,
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
        # Shareable URL format: https://meet.witysk.org/<3-word-slug>
        # (no `/j/` prefix). The SPA routes `/<slug>` to the lobby.
        "join_url": f"{settings.public_url}/{meeting.room_name}",
    }


@router.get("/meetings")
def list_my_meetings(user: RequireUser, db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(Meeting)
        .filter(Meeting.owner_user_id == user.user_id)
        .filter(Meeting.hidden.is_(False))
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
async def end_or_hide_meeting(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> None:
    """DELETE on a meeting has two semantics depending on state:

    - **Active meeting**: ends the meeting for everyone — closes the LiveKit room
      (kicks all participants) and marks `is_active=False`. Stays in the list as
      "Closed".
    - **Already-closed meeting**: soft-deletes the row by setting `hidden=True`,
      so it disappears from the user's MyMeetings list. Recordings belonging to
      it remain reachable in the recordings list (they keep their meeting_id).
    """
    from livekit import api as lkapi

    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")

    if m.is_active:
        lk = livekit_api()
        try:
            try:
                await lk.room.delete_room(lkapi.DeleteRoomRequest(room=m.room_name))
            except Exception:  # noqa: BLE001 — room may not exist
                pass
        finally:
            await lk.aclose()
        m.is_active = False
        m.closed_at = datetime.utcnow()
    else:
        m.hidden = True

    db.commit()


class InviteBody(BaseModel):
    emails: list[str] = Field(min_length=1, max_length=20)
    personal_message: str | None = Field(default=None, max_length=1000)


@router.post("/meetings/{meeting_id}/invite")
async def invite_by_email(
    meeting_id: str,
    body: InviteBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Send a Resend-backed invite email to one or more recipients."""
    from app.services.email import send_email
    from app.services.email_templates import meeting_invite

    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")

    join_url = f"{settings.public_url}/{m.room_name}"
    subject, html, text = meeting_invite(
        inviter_name=user.email or f"User {user.user_id}",
        inviter_email=user.email,
        meeting_title=m.display_title,
        join_url=join_url,
        personal_message=body.personal_message,
    )

    sent = 0
    failed: list[str] = []
    for addr in body.emails:
        ok = await send_email(
            to=addr,
            subject=subject,
            html=html,
            text=text,
            reply_to=settings.invite_reply_to or user.email or None,
        )
        if ok:
            sent += 1
        else:
            failed.append(addr)

    return {
        "ok": sent > 0,
        "sent": sent,
        "failed": failed,
        "join_url": join_url,
    }


_ALLOWED_BRANDING_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@router.post("/meetings/{meeting_id}/branding")
async def upload_branding(
    meeting_id: str,
    user: RequireUser,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    """Upload (or replace) a branding image for the meeting. The image is
    served publicly via /api/v1/rooms/{room_name}/branding so the lobby can
    show it before auth."""
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")

    ct = (file.content_type or "").lower()
    ext = _ALLOWED_BRANDING_TYPES.get(ct)
    if not ext:
        raise HTTPException(status_code=415, detail=f"unsupported content type {ct!r}; use jpeg/png/webp/gif")

    # Stream-read up to the cap, abort on overrun.
    Path(settings.branding_dir).mkdir(parents=True, exist_ok=True)
    target = Path(settings.branding_dir) / f"{m.id}{ext}"
    cap = settings.branding_max_bytes
    total = 0
    with target.open("wb") as out:
        while chunk := await file.read(64 * 1024):
            total += len(chunk)
            if total > cap:
                out.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"image exceeds {cap} bytes")
            out.write(chunk)

    # If a previous branding existed with a different extension, remove it.
    if m.branding_image_path and m.branding_image_path != str(target):
        try:
            Path(m.branding_image_path).unlink(missing_ok=True)
        except OSError:
            pass

    m.branding_image_path = str(target)
    db.commit()
    return {"ok": True, "branding_url": _branding_url(m), "size": total, "content_type": ct}


@router.delete("/meetings/{meeting_id}/branding", status_code=204)
def delete_branding(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> None:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    if m.branding_image_path:
        try:
            Path(m.branding_image_path).unlink(missing_ok=True)
        except OSError:
            pass
    m.branding_image_path = None
    db.commit()


@router.patch("/meetings/{meeting_id}")
def update_meeting(
    meeting_id: str,
    body: UpdateMeetingBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Owner updates editable fields on a meeting (visibility, title, recording mode)."""
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")

    if body.display_title is not None:
        m.display_title = body.display_title
    if body.recording_mode is not None:
        m.recording_mode = body.recording_mode

    # Visibility: anonymous implies authenticated.
    if body.list_for_anonymous is not None:
        m.list_for_anonymous = body.list_for_anonymous
        if body.list_for_anonymous:
            m.list_for_authenticated = True
    if body.list_for_authenticated is not None:
        # Don't let `list_for_authenticated=False` silently flip the anon
        # flag — but if anon is on, authenticated must remain on.
        if body.list_for_authenticated is False and m.list_for_anonymous:
            raise HTTPException(
                status_code=400,
                detail="cannot disable list_for_authenticated while list_for_anonymous is true",
            )
        m.list_for_authenticated = body.list_for_authenticated

    db.commit()
    return _to_out(m).model_dump()


@router.get("/discoverable")
def list_discoverable(user: RequireUser, db: Session = Depends(get_db)) -> list[dict]:
    """Active meetings owned by OTHER users that the current user is allowed
    to discover (either flag set; the user's own meetings appear in
    /meetings instead). Returns the public projection only."""
    rows = (
        db.query(Meeting)
        .filter(Meeting.is_active.is_(True))
        .filter(Meeting.hidden.is_(False))
        .filter(Meeting.owner_user_id != user.user_id)
        .filter(
            (Meeting.list_for_authenticated.is_(True))
            | (Meeting.list_for_anonymous.is_(True))
        )
        .order_by(Meeting.created_at.desc())
        .limit(50)
        .all()
    )
    return [_to_public_out(m) for m in rows]


@router.get("/public-meetings")
def list_public_meetings(db: Session = Depends(get_db)) -> list[dict]:
    """Active meetings that opted into anonymous discovery. No auth required —
    suitable for the unauthenticated landing page."""
    rows = (
        db.query(Meeting)
        .filter(Meeting.is_active.is_(True))
        .filter(Meeting.hidden.is_(False))
        .filter(Meeting.list_for_anonymous.is_(True))
        .order_by(Meeting.created_at.desc())
        .limit(50)
        .all()
    )
    return [_to_public_out(m) for m in rows]


@router.get("/rooms/{room_name}/info")
def public_room_info(room_name: str, db: Session = Depends(get_db)) -> dict:
    """Public endpoint used by the lobby to show meeting title + branding
    before the user has a token. No auth required, no sensitive fields."""
    m = db.query(Meeting).filter_by(room_name=room_name).first()
    if not m or not m.is_active:
        raise HTTPException(status_code=404, detail="room not found")
    return {
        "room_name": m.room_name,
        "display_title": m.display_title,
        "require_password": bool(m.require_password),
        "branding_url": _branding_url(m),
    }


@router.get("/rooms/{room_name}/branding")
def public_room_branding(room_name: str, db: Session = Depends(get_db)) -> FileResponse:
    """Public endpoint that serves the branding image bytes. Cache-friendly."""
    m = db.query(Meeting).filter_by(room_name=room_name).first()
    if not m or not m.branding_image_path:
        raise HTTPException(status_code=404, detail="no branding image")
    p = Path(m.branding_image_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="branding file missing")
    # Infer content-type from extension.
    ext_to_ct = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    return FileResponse(
        path=str(p),
        media_type=ext_to_ct.get(p.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": "public, max-age=300"},
    )


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

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from passlib.hash import argon2
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ulid import ULID

from app.auth import AuthUser, RequireAdmin, RequireUser
from app.config import settings
from app.db import get_db
from app.livekit_client import livekit_api, mint_participant_token, short_lived_turn_credentials
from app.models import Meeting, MeetingParticipant, UserPreferences
from app.services.slug_words import generate_unique_slug


def _cohost_set(m: Meeting) -> set[str]:
    """Parse the Meeting.cohost_user_ids JSON list into a set of strings.
    Defensive: returns an empty set on any parse error so a corrupt row
    never blocks moderation."""
    try:
        import json
        v = json.loads(m.cohost_user_ids or "[]")
        if isinstance(v, list):
            return {str(x) for x in v if isinstance(x, (str, int))}
    except ValueError:
        pass
    return set()


def is_moderator(m: Meeting, user_sub: str | None) -> bool:
    if not user_sub:
        return False
    if m.owner_user_id == user_sub:
        return True
    return user_sub in _cohost_set(m)


def _validated_rrule(value: str | None) -> str | None:
    """Accept only the recurrence presets the form offers. Anything else
    becomes None — we don't trust arbitrary RRULE strings into the .ics."""
    from app.services.ics import ALLOWED_RRULES
    if not value:
        return None
    v = value.strip()
    return v if v in ALLOWED_RRULES else None


def _anonymise_email(email: str | None) -> str | None:
    """Replace the local part of an email with the first letter + asterisks.
    `alice@example.com` → `a***@example.com`. Falls back to a single `***@…`
    when the local part is one character so we don't accidentally reveal it.
    """
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"

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
    # Owner's preferred display name. The SPA fetches it from
    # one.witysk.org's /api/auth/me (browser-to-server, IP-bound JWT
    # validates correctly) and passes it here. Stored on the meeting so
    # other viewers (Discover, lobby) see who's hosting.
    display_name: str | None = Field(default=None, max_length=120)
    # ── Moderation policy (defaults match Meeting model defaults) ─────
    auto_admit_authenticated: bool = True
    require_name_on_join: bool = True
    auto_mute_new_joiners: bool = False
    auto_disable_camera_for_new: bool = False
    waiting_room_enabled: bool = False
    lock_room_after_start: bool = False
    allow_participant_screenshare: bool = True
    allow_participant_chat: bool = True
    lobby_greeting: str | None = Field(default=None, max_length=2000)
    # RFC 5545 RRULE string (no leading "RRULE:"). Restricted to the
    # presets the form offers so we don't have to safely parse free input.
    recurrence_rule: str | None = Field(default=None, max_length=200)
    duration_minutes: int | None = Field(default=None, ge=5, le=8 * 60)
    # ── Live stream to X.com (or any RTMPS endpoint) ──────────────────
    # When enabled, the in-meeting toolbar offers a Start/Stop streaming
    # button. The URL + key are sent to LiveKit egress concatenated as
    # `<url>/<key>`, which is what every major RTMP endpoint expects.
    livestream_enabled: bool = False
    livestream_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_stream_key: str | None = Field(default=None, max_length=500)
    livestream_substack_enabled: bool = False
    livestream_substack_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_substack_stream_key: str | None = Field(default=None, max_length=500)
    livestream_youtube_enabled: bool = False
    livestream_youtube_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_youtube_stream_key: str | None = Field(default=None, max_length=500)
    livestream_facebook_enabled: bool = False
    livestream_facebook_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_facebook_stream_key: str | None = Field(default=None, max_length=500)
    livestream_rumble_enabled: bool = False
    livestream_rumble_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_rumble_stream_key: str | None = Field(default=None, max_length=500)


class UpdateMeetingBody(BaseModel):
    """Partial update — owner-only. Fields left unset are not changed."""
    display_title: str | None = Field(default=None, min_length=1, max_length=200)
    list_for_authenticated: bool | None = None
    list_for_anonymous: bool | None = None
    recording_mode: str | None = Field(default=None, pattern="^(manual|auto_on_start|off)$")
    livestream_enabled: bool | None = None
    livestream_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_stream_key: str | None = Field(default=None, max_length=500)
    livestream_substack_enabled: bool | None = None
    livestream_substack_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_substack_stream_key: str | None = Field(default=None, max_length=500)
    livestream_youtube_enabled: bool | None = None
    livestream_youtube_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_youtube_stream_key: str | None = Field(default=None, max_length=500)
    livestream_facebook_enabled: bool | None = None
    livestream_facebook_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_facebook_stream_key: str | None = Field(default=None, max_length=500)
    livestream_rumble_enabled: bool | None = None
    livestream_rumble_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_rumble_stream_key: str | None = Field(default=None, max_length=500)
    playback_enabled: bool | None = None
    playback_loop: bool | None = None


class MeetingOut(BaseModel):
    id: str
    room_name: str
    display_title: str
    owner_user_id: str
    owner_name: str | None = None
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
    auto_admit_authenticated: bool = True
    require_name_on_join: bool = True
    auto_mute_new_joiners: bool = False
    auto_disable_camera_for_new: bool = False
    waiting_room_enabled: bool = False
    lock_room_after_start: bool = False
    allow_participant_screenshare: bool = True
    allow_participant_chat: bool = True
    lobby_greeting: str | None = None
    recurrence_rule: str | None = None
    duration_minutes: int | None = None
    # Livestream config — surfaced so the SPA can render the right toolbar
    # state and pre-fill the edit modal. The stream key is intentionally
    # included; the endpoint is owner-only.
    livestream_enabled: bool = False
    livestream_rtmps_url: str | None = None
    livestream_stream_key: str | None = None
    livestream_substack_enabled: bool = False
    livestream_substack_rtmps_url: str | None = None
    livestream_substack_stream_key: str | None = None
    livestream_youtube_enabled: bool = False
    livestream_youtube_rtmps_url: str | None = None
    livestream_youtube_stream_key: str | None = None
    livestream_facebook_enabled: bool = False
    livestream_facebook_rtmps_url: str | None = None
    livestream_facebook_stream_key: str | None = None
    livestream_rumble_enabled: bool = False
    livestream_rumble_rtmps_url: str | None = None
    livestream_rumble_stream_key: str | None = None
    # True while a livestream egress is active (fanning out to whichever
    # destinations are configured).
    livestream_active: bool = False
    playback_enabled: bool = False
    playback_loop: bool = False
    # True while a LiveKit ingress is publishing the current playlist item.
    playback_active: bool = False
    playback_current_item_id: str | None = None


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
        owner_name=m.owner_name,
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
        auto_admit_authenticated=bool(m.auto_admit_authenticated),
        require_name_on_join=bool(m.require_name_on_join),
        auto_mute_new_joiners=bool(m.auto_mute_new_joiners),
        auto_disable_camera_for_new=bool(m.auto_disable_camera_for_new),
        waiting_room_enabled=bool(m.waiting_room_enabled),
        lock_room_after_start=bool(m.lock_room_after_start),
        allow_participant_screenshare=bool(m.allow_participant_screenshare),
        allow_participant_chat=bool(m.allow_participant_chat),
        lobby_greeting=m.lobby_greeting,
        recurrence_rule=m.recurrence_rule,
        duration_minutes=m.duration_minutes,
        livestream_enabled=bool(m.livestream_enabled),
        livestream_rtmps_url=m.livestream_rtmps_url,
        livestream_stream_key=m.livestream_stream_key,
        livestream_substack_enabled=bool(m.livestream_substack_enabled),
        livestream_substack_rtmps_url=m.livestream_substack_rtmps_url,
        livestream_substack_stream_key=m.livestream_substack_stream_key,
        livestream_youtube_enabled=bool(m.livestream_youtube_enabled),
        livestream_youtube_rtmps_url=m.livestream_youtube_rtmps_url,
        livestream_youtube_stream_key=m.livestream_youtube_stream_key,
        livestream_facebook_enabled=bool(m.livestream_facebook_enabled),
        livestream_facebook_rtmps_url=m.livestream_facebook_rtmps_url,
        livestream_facebook_stream_key=m.livestream_facebook_stream_key,
        livestream_rumble_enabled=bool(m.livestream_rumble_enabled),
        livestream_rumble_rtmps_url=m.livestream_rumble_rtmps_url,
        livestream_rumble_stream_key=m.livestream_rumble_stream_key,
        livestream_active=bool(m.livestream_egress_id),
        playback_enabled=bool(m.playback_enabled),
        playback_loop=bool(m.playback_loop),
        playback_active=bool(m.playback_ingress_id),
        playback_current_item_id=m.playback_current_item_id,
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
        "owner_name": m.owner_name,
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
def create_meeting(body: CreateMeetingBody, user: RequireAdmin, db: Session = Depends(get_db)) -> dict:
    """Create a new meeting. Gated on admin rights:
      - SSO (one.witysk.org) accounts always pass.
      - Native accounts pass while their 10-day trial, voucher-granted
        entitlement, or paid PayPal subscription is still active.
    Native users with no active entitlement get a 403 from RequireAdmin
    with a message telling them to renew.
    """
    meeting = Meeting(
        id=str(ULID()),
        room_name=_fresh_room_name(db),
        display_title=body.display_title,
        owner_user_id=user.sub,
        owner_email=user.email,
        owner_name=body.display_name,
        scheduled_at=body.scheduled_at,
        ends_at=body.ends_at,
        max_participants=body.max_participants,
        require_password=bool(body.password),
        password_hash=argon2.hash(body.password) if body.password else None,
        recording_mode=body.recording_mode,
        list_for_authenticated=body.list_for_authenticated or body.list_for_anonymous,
        list_for_anonymous=body.list_for_anonymous,
        auto_admit_authenticated=body.auto_admit_authenticated,
        require_name_on_join=body.require_name_on_join,
        auto_mute_new_joiners=body.auto_mute_new_joiners,
        auto_disable_camera_for_new=body.auto_disable_camera_for_new,
        waiting_room_enabled=body.waiting_room_enabled,
        lock_room_after_start=body.lock_room_after_start,
        allow_participant_screenshare=body.allow_participant_screenshare,
        allow_participant_chat=body.allow_participant_chat,
        lobby_greeting=(body.lobby_greeting or "").strip() or None,
        recurrence_rule=_validated_rrule(body.recurrence_rule),
        duration_minutes=body.duration_minutes,
        livestream_enabled=bool(body.livestream_enabled),
        livestream_rtmps_url=(body.livestream_rtmps_url or "").strip() or None,
        livestream_stream_key=(body.livestream_stream_key or "").strip() or None,
        livestream_substack_enabled=bool(body.livestream_substack_enabled),
        livestream_substack_rtmps_url=(body.livestream_substack_rtmps_url or "").strip() or None,
        livestream_substack_stream_key=(body.livestream_substack_stream_key or "").strip() or None,
        livestream_youtube_enabled=bool(body.livestream_youtube_enabled),
        livestream_youtube_rtmps_url=(body.livestream_youtube_rtmps_url or "").strip() or None,
        livestream_youtube_stream_key=(body.livestream_youtube_stream_key or "").strip() or None,
        livestream_facebook_enabled=bool(body.livestream_facebook_enabled),
        livestream_facebook_rtmps_url=(body.livestream_facebook_rtmps_url or "").strip() or None,
        livestream_facebook_stream_key=(body.livestream_facebook_stream_key or "").strip() or None,
        livestream_rumble_enabled=bool(body.livestream_rumble_enabled),
        livestream_rumble_rtmps_url=(body.livestream_rumble_rtmps_url or "").strip() or None,
        livestream_rumble_stream_key=(body.livestream_rumble_stream_key or "").strip() or None,
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)

    # The LiveKit room is auto-created on first participant join (auto_create
    # is on in livekit.yaml.tpl), so no pre-creation step is needed here.

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
        .filter(Meeting.owner_user_id == user.sub)
        .filter(Meeting.hidden.is_(False))
        .order_by(Meeting.created_at.desc())
        .all()
    )
    return [_to_out(m).model_dump() for m in rows]


@router.get("/meetings/{meeting_id}")
def get_meeting(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    return _to_out(m).model_dump()


@router.post("/meetings/{meeting_id}/reopen")
def reopen_meeting(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    """Reopen a closed meeting. Flips `is_active` back to True and clears
    `closed_at`. The room_name is unchanged so existing share links keep
    working. The LiveKit room is recreated on demand when the first
    participant joins (auto_create on the LiveKit server)."""
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    if not m.is_active:
        m.is_active = True
        m.closed_at = None
        m.hidden = False
        db.commit()
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

    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
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
        m.closed_at = datetime.now(timezone.utc)
    else:
        m.hidden = True

    db.commit()


class InviteBody(BaseModel):
    emails: list[str] = Field(min_length=1, max_length=20)
    personal_message: str | None = Field(default=None, max_length=1000)
    # SPA passes the inviter's freshly-fetched display name from
    # one.witysk.org's /api/auth/me so the email shows their current
    # preferred name (and refreshes the snapshot on the meeting row).
    display_name: str | None = Field(default=None, max_length=120)


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

    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")

    if body.display_name:
        m.owner_name = body.display_name
        db.commit()

    join_url = f"{settings.public_url}/{m.room_name}"
    subject, html, text = meeting_invite(
        inviter_name=m.owner_name or user.email or f"User {user.sub}",
        inviter_email=user.email,
        meeting_title=m.display_title,
        join_url=join_url,
        personal_message=body.personal_message,
        branding_url=_branding_url(m),
    )

    # Generate the .ics once and attach it to every invite so calendars
    # (Google, Outlook, Apple) recognise the meeting and let recipients add
    # it with one click.
    import base64 as _b64
    from app.services.ics import ics_for_meeting
    ics_text = ics_for_meeting(m)
    ics_attachment = {
        "filename": f"{m.room_name}.ics",
        "content": _b64.b64encode(ics_text.encode("utf-8")).decode("ascii"),
        "content_type": "text/calendar",
    }

    sent = 0
    failed: list[str] = []
    for addr in body.emails:
        ok = await send_email(
            to=addr,
            subject=subject,
            html=html,
            text=text,
            reply_to=settings.invite_reply_to or user.email or None,
            attachments=[ics_attachment],
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
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
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
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
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
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
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

    if body.livestream_enabled is not None:
        m.livestream_enabled = body.livestream_enabled
    if body.livestream_rtmps_url is not None:
        m.livestream_rtmps_url = body.livestream_rtmps_url.strip() or None
    if body.livestream_stream_key is not None:
        m.livestream_stream_key = body.livestream_stream_key.strip() or None
    if body.livestream_substack_enabled is not None:
        m.livestream_substack_enabled = body.livestream_substack_enabled
    if body.livestream_substack_rtmps_url is not None:
        m.livestream_substack_rtmps_url = body.livestream_substack_rtmps_url.strip() or None
    if body.livestream_substack_stream_key is not None:
        m.livestream_substack_stream_key = body.livestream_substack_stream_key.strip() or None
    if body.livestream_youtube_enabled is not None:
        m.livestream_youtube_enabled = body.livestream_youtube_enabled
    if body.livestream_youtube_rtmps_url is not None:
        m.livestream_youtube_rtmps_url = body.livestream_youtube_rtmps_url.strip() or None
    if body.livestream_youtube_stream_key is not None:
        m.livestream_youtube_stream_key = body.livestream_youtube_stream_key.strip() or None
    if body.livestream_facebook_enabled is not None:
        m.livestream_facebook_enabled = body.livestream_facebook_enabled
    if body.livestream_facebook_rtmps_url is not None:
        m.livestream_facebook_rtmps_url = body.livestream_facebook_rtmps_url.strip() or None
    if body.livestream_facebook_stream_key is not None:
        m.livestream_facebook_stream_key = body.livestream_facebook_stream_key.strip() or None
    if body.livestream_rumble_enabled is not None:
        m.livestream_rumble_enabled = body.livestream_rumble_enabled
    if body.livestream_rumble_rtmps_url is not None:
        m.livestream_rumble_rtmps_url = body.livestream_rumble_rtmps_url.strip() or None
    if body.livestream_rumble_stream_key is not None:
        m.livestream_rumble_stream_key = body.livestream_rumble_stream_key.strip() or None
    if body.playback_enabled is not None:
        m.playback_enabled = body.playback_enabled
    if body.playback_loop is not None:
        m.playback_loop = body.playback_loop

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
        .filter(Meeting.owner_user_id != user.sub)
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
        "owner_name": m.owner_name,
        "lobby_greeting": m.lobby_greeting,
        # Exposed so non-owner participants can address meeting-scoped
        # endpoints (polls, Q&A, shared notes). No sensitive data — the
        # meeting_id alone gives no extra access without a moderator JWT.
        "meeting_id": m.id,
    }


@router.get("/rooms/{room_name}/ics")
def public_room_ics(room_name: str, db: Session = Depends(get_db)):
    """Public .ics download for a room. Used by the lobby + Recordings page
    so participants can save the meeting to their calendar without an
    invite email."""
    from fastapi.responses import Response
    from app.services.ics import ics_for_meeting
    m = db.query(Meeting).filter_by(room_name=room_name).first()
    if not m or not m.is_active:
        raise HTTPException(status_code=404, detail="room not found")
    body = ics_for_meeting(m)
    return Response(
        content=body,
        media_type="text/calendar",
        headers={
            "Content-Disposition": f'attachment; filename="{m.room_name}.ics"',
            "Cache-Control": "no-cache",
        },
    )


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


class OwnerTokenBody(BaseModel):
    """Optional payload for the owner-token mint. The SPA passes the owner's
    current preferred display name (freshly fetched from one.witysk.org's
    /api/auth/me) so it can both (a) appear in the participant list as their
    real name and (b) refresh the snapshot stored on the meeting row."""
    display_name: str | None = Field(default=None, max_length=120)


@router.post("/meetings/{meeting_id}/token")
def mint_owner_token(
    meeting_id: str,
    user: RequireUser,
    body: OwnerTokenBody | None = None,
    db: Session = Depends(get_db),
) -> dict:
    # Accept the owner OR any user listed as a co-host. Both get a
    # `room_admin` LiveKit token so they can perform moderator actions.
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    if not is_moderator(m, user.sub):
        raise HTTPException(status_code=404, detail="meeting not found")
    if not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")
    is_real_owner = m.owner_user_id == user.sub
    if body and body.display_name and is_real_owner:
        m.owner_name = body.display_name
        db.commit()
    identity = f"user-{user.sub}"
    display_name = (
        (m.owner_name if is_real_owner else None)
        or user.email
        or f"User {user.sub}"
    )
    token = mint_participant_token(
        room_name=m.room_name,
        identity=identity,
        display_name=display_name,
        is_owner=True,
    )
    # Privacy: when this user has `anonymise_email_in_join_log` on, store a
    # masked copy of their email on the participant row rather than the raw
    # address. Owner sees the meeting normally; the join log carries the
    # anonymised form.
    prefs_row = db.get(UserPreferences, user.sub)
    stored_email = user.email
    if prefs_row and prefs_row.anonymise_email_in_join_log:
        stored_email = _anonymise_email(stored_email)
    db.add(
        MeetingParticipant(
            meeting_id=m.id,
            livekit_identity=identity,
            display_name=display_name,
            email=stored_email,
            is_authenticated=True,
            is_owner=is_real_owner,
        )
    )
    db.commit()
    return {
        "livekit_url": settings.livekit_ws_url,
        "token": token,
        "room_name": m.room_name,
        "ice_servers": short_lived_turn_credentials(identity),
        "role": "owner" if is_real_owner else "cohost",
    }


@router.get("/rooms/{room_name}/me-role")
def my_role(room_name: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    """Tell an authenticated user what role they hold in this room. Used by
    the Lobby to decide whether to mint a moderator token or fall through
    to the anon-token path."""
    m = db.query(Meeting).filter_by(room_name=room_name).first()
    if not m or not m.is_active:
        raise HTTPException(status_code=404, detail="room not found")
    if m.owner_user_id == user.sub:
        return {"role": "owner", "meeting_id": m.id}
    if user.sub in _cohost_set(m):
        return {"role": "cohost", "meeting_id": m.id}
    return {"role": "guest", "meeting_id": m.id}


class CohostBody(BaseModel):
    user_sub: str = Field(min_length=1, max_length=200)


def _save_cohosts(m: Meeting, cohosts: set[str]) -> None:
    import json
    m.cohost_user_ids = json.dumps(sorted(cohosts))


@router.get("/meetings/{meeting_id}/cohosts")
def list_cohosts(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> list[str]:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    return sorted(_cohost_set(m))


@router.post("/meetings/{meeting_id}/cohosts")
def add_cohost(
    meeting_id: str,
    body: CohostBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    if body.user_sub == m.owner_user_id:
        return {"ok": True, "cohosts": sorted(_cohost_set(m))}
    cur = _cohost_set(m)
    cur.add(body.user_sub)
    _save_cohosts(m, cur)
    db.commit()
    return {"ok": True, "cohosts": sorted(cur)}


@router.delete("/meetings/{meeting_id}/cohosts/{user_sub}")
def remove_cohost(
    meeting_id: str,
    user_sub: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    cur = _cohost_set(m)
    cur.discard(user_sub)
    _save_cohosts(m, cur)
    db.commit()
    return {"ok": True, "cohosts": sorted(cur)}

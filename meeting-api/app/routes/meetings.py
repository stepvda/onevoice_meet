from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from passlib.hash import argon2
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ulid import ULID

from app.auth import AuthUser, RequireAdmin, RequireUser
from app.config import settings
from app.db import get_db
from app.livekit_client import livekit_api, mint_participant_token, short_lived_turn_credentials
from app.models import Meeting, MeetingParticipant, User, UserPreferences
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


import re

# Slug for the /public/<slug> page. Lowercase letters, digits and dashes
# only, must start with a letter or digit, 3–60 chars. Keep the alphabet
# restrictive so it stays URL-safe without encoding and so we can compare
# case-insensitively to detect duplicates.
_PUBLIC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")


def _normalise_public_slug(raw: str | None) -> str | None:
    if raw is None:
        return None
    v = raw.strip().lower()
    if not v:
        return None
    if not _PUBLIC_SLUG_RE.match(v):
        raise HTTPException(
            status_code=400,
            detail=(
                "public_slug must be 3–60 characters, lowercase letters / "
                "digits / dashes, and start and end with a letter or digit"
            ),
        )
    return v


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
    # "rtmp" = legacy stream-key paste; "api" = OAuth + Data API managed
    # broadcasts. Switching to "api" requires the owner to have completed
    # the OAuth flow (refresh_token column populated).
    livestream_youtube_mode: str | None = Field(default=None, pattern="^(rtmp|api)$")
    livestream_facebook_enabled: bool | None = None
    livestream_facebook_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_facebook_stream_key: str | None = Field(default=None, max_length=500)
    livestream_rumble_enabled: bool | None = None
    livestream_rumble_rtmps_url: str | None = Field(default=None, max_length=500)
    livestream_rumble_stream_key: str | None = Field(default=None, max_length=500)
    playback_enabled: bool | None = None
    playback_loop: bool | None = None
    playback_whats_up_next: bool | None = None
    # Picture-in-Picture egress layout. When the toggle changes (or the
    # overlay identity changes) and an egress is currently running, the
    # handler restarts it so the new composition takes effect immediately.
    pip_enabled: bool | None = None
    pip_overlay_identity: str | None = Field(default=None, max_length=80)
    # Public view-only page. Enabling requires a slug to be set (either in
    # the same PATCH or previously). Slug is normalised to lowercase and
    # checked for uniqueness across all meetings.
    public_enabled: bool | None = None
    public_slug: str | None = Field(default=None, max_length=80)


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
    # YouTube managed-mode read state. The refresh token is NEVER exposed
    # — the SPA only needs to know whether a channel is connected and the
    # current public watch URL.
    livestream_youtube_mode: str = "rtmp"
    livestream_youtube_oauth_connected: bool = False
    livestream_youtube_channel_title: str | None = None
    livestream_youtube_watch_url: str | None = None
    livestream_facebook_enabled: bool = False
    livestream_facebook_rtmps_url: str | None = None
    livestream_facebook_stream_key: str | None = None
    livestream_rumble_enabled: bool = False
    livestream_rumble_rtmps_url: str | None = None
    livestream_rumble_stream_key: str | None = None
    # True while a livestream egress is active (fanning out to whichever
    # destinations are configured).
    livestream_active: bool = False
    # The layout the running (or last) egress was started with — the SPA
    # uses this to initialise the layout dropdown on rejoin so it
    # matches what's actually playing instead of falling back to
    # "speaker" and tempting the host to restart with a layout they
    # didn't intend.
    current_egress_layout: str | None = None
    playback_enabled: bool = False
    playback_loop: bool = False
    playback_whats_up_next: bool = False
    # True while a LiveKit ingress is publishing the current playlist item.
    playback_active: bool = False
    playback_current_item_id: str | None = None
    # Picture-in-Picture egress layout (recording + livestream output).
    pip_enabled: bool = False
    pip_overlay_identity: str | None = None
    # Public view-only stream. `public_url` is computed from the slug.
    public_enabled: bool = False
    public_slug: str | None = None
    public_url: str | None = None


def _branding_url(m: Meeting) -> str | None:
    if not m.branding_image_path:
        return None
    # Public, served via the room-name path so the lobby can show it pre-auth.
    return f"{settings.public_url}/api/v1/rooms/{m.room_name}/branding"


def _public_url(m: Meeting) -> str | None:
    if not m.public_slug:
        return None
    return f"{settings.public_url}/public/{m.public_slug}"


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
        livestream_youtube_mode=m.livestream_youtube_mode or "rtmp",
        livestream_youtube_oauth_connected=bool(m.livestream_youtube_refresh_token),
        livestream_youtube_channel_title=m.livestream_youtube_channel_title,
        livestream_youtube_watch_url=m.livestream_youtube_watch_url,
        livestream_facebook_enabled=bool(m.livestream_facebook_enabled),
        livestream_facebook_rtmps_url=m.livestream_facebook_rtmps_url,
        livestream_facebook_stream_key=m.livestream_facebook_stream_key,
        livestream_rumble_enabled=bool(m.livestream_rumble_enabled),
        livestream_rumble_rtmps_url=m.livestream_rumble_rtmps_url,
        livestream_rumble_stream_key=m.livestream_rumble_stream_key,
        livestream_active=bool(m.livestream_egress_id),
        current_egress_layout=m.current_egress_layout,
        playback_enabled=bool(m.playback_enabled),
        playback_loop=bool(m.playback_loop),
        playback_whats_up_next=bool(m.playback_whats_up_next),
        playback_active=bool(m.playback_ingress_id),
        playback_current_item_id=m.playback_current_item_id,
        pip_enabled=bool(m.pip_enabled),
        pip_overlay_identity=m.pip_overlay_identity,
        public_enabled=bool(m.public_enabled),
        public_slug=m.public_slug,
        public_url=_public_url(m),
    )


def _to_public_out(m: Meeting, *, viewer_is_authenticated: bool) -> dict:
    """Subset of fields safe to expose without auth (the room_name is needed
    to build the join URL; owner_user_id and password hash are NEVER
    exposed).

    `joinable` is computed per-caller: anonymous callers may join when
    `list_for_anonymous=True`, authenticated callers may join when either
    visibility flag is set (anon-listed implies auth-listed). The
    frontend uses this to decide whether to render a Join button —
    public-view-only meetings (`public_enabled=True` with no joining
    permission) get a View button only.
    """
    if viewer_is_authenticated:
        joinable = bool(m.list_for_authenticated or m.list_for_anonymous)
    else:
        joinable = bool(m.list_for_anonymous)
    return {
        "room_name": m.room_name,
        "display_title": m.display_title,
        "max_participants": m.max_participants,
        "require_password": bool(m.require_password),
        "branding_url": _branding_url(m),
        "owner_name": m.owner_name,
        "public_enabled": bool(m.public_enabled),
        "public_slug": m.public_slug,
        "public_url": _public_url(m),
        "joinable": joinable,
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
    """Owners and co-hosts can fetch the full meeting record (including
    streaming credentials, since they have parity with the owner for
    starting / stopping streams). Other users get 404."""
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not is_moderator(m, user.sub):
        raise HTTPException(status_code=404, detail="meeting not found")
    return _to_out(m).model_dump()


@router.post("/meetings/{meeting_id}/reopen")
def reopen_meeting(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    """Reopen a closed meeting. Flips `is_active` back to True and clears
    `closed_at`. The room_name is unchanged so existing share links keep
    working. The LiveKit room is recreated on demand when the first
    participant joins (auto_create on the LiveKit server). Co-hosts are
    allowed to reopen (parity with owner moderation surface)."""
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not is_moderator(m, user.sub):
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
      "Closed". **Co-hosts may end an active meeting** since it's a moderation
      action with parity to mute-all / kick.
    - **Already-closed meeting**: soft-deletes (hides) the row from MyMeetings.
      This stays **owner-only** because it's a destructive bookkeeping action
      on the owner's account, not a moderation primitive.
    Recordings belonging to the meeting stay reachable in either case.
    """
    from livekit import api as lkapi

    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    is_owner = m.owner_user_id == user.sub
    if m.is_active:
        # Ending an active meeting → any moderator may do it.
        if not is_moderator(m, user.sub):
            raise HTTPException(status_code=404, detail="meeting not found")
    else:
        # Hiding a closed meeting → owner only.
        if not is_owner:
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
    """Send a Resend-backed invite email to one or more recipients.
    Co-hosts can invite as well — the From: name shown to recipients is
    the meeting owner's name regardless of who sends, so attribution
    stays consistent."""
    from app.services.email import send_email
    from app.services.email_templates import meeting_invite

    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not is_moderator(m, user.sub):
        raise HTTPException(status_code=404, detail="meeting not found")

    if body.display_name and m.owner_user_id == user.sub:
        # Only the owner gets to refresh `owner_name` on the meeting from
        # the invite call; a co-host's display name shouldn't overwrite
        # the owner's snapshot.
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
async def update_meeting(
    meeting_id: str,
    body: UpdateMeetingBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Owner or co-host updates editable operational fields on a meeting:
    visibility, title, recording mode, livestream destinations, video
    playback config. The owner-only operations (delete, branding, co-host
    grants) remain gated separately. When the meeting has an active
    livestream egress and the destination set changes, we call LiveKit
    `UpdateStreamRequest` to add/remove URLs without restarting the
    egress — so a new destination starts receiving the stream the same
    tick the toggle flips."""
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not is_moderator(m, user.sub):
        raise HTTPException(status_code=404, detail="meeting not found")

    # Snapshot the URL set BEFORE applying changes so we can diff after.
    from app.services.egress_mgr import _enabled_stream_urls
    pre_urls = set(_enabled_stream_urls(m)) if m.livestream_egress_id else None

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
    if body.livestream_youtube_mode is not None:
        new_mode = body.livestream_youtube_mode
        if new_mode == "api" and not m.livestream_youtube_refresh_token:
            raise HTTPException(
                status_code=400,
                detail="connect a YouTube channel before switching to API mode",
            )
        m.livestream_youtube_mode = new_mode
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
    if body.playback_whats_up_next is not None:
        toggle_changed_on = (
            body.playback_whats_up_next and not bool(m.playback_whats_up_next)
        )
        m.playback_whats_up_next = body.playback_whats_up_next
        if toggle_changed_on:
            # Pre-encode the next eligible slide so the first eligible
            # advance / play doesn't pay the encode latency. Runs in the
            # background — best-effort.
            from app.services.whats_next_slide import schedule_pre_generation
            schedule_pre_generation(m.id)

    # Picture-in-Picture egress layout. Snapshot the before-values so we
    # can detect a meaningful change after applying the body — if a change
    # lands while a recording / livestream is running, we restart the
    # egress so the new layout takes effect immediately.
    pip_changed = False
    if body.pip_enabled is not None:
        if bool(m.pip_enabled) != bool(body.pip_enabled):
            pip_changed = True
        m.pip_enabled = body.pip_enabled
    if body.pip_overlay_identity is not None:
        new_overlay = body.pip_overlay_identity.strip() or None
        if (m.pip_overlay_identity or None) != new_overlay:
            pip_changed = True
        m.pip_overlay_identity = new_overlay

    # Public view-only page. Normalise the slug, reject duplicates, and
    # auto-flip the anonymous-listing flag so the meeting also surfaces
    # on the home page Discover list while public is on.
    new_slug_field_present = "public_slug" in body.model_fields_set
    new_enabled_field_present = "public_enabled" in body.model_fields_set
    if new_slug_field_present or new_enabled_field_present:
        # Compute the slug we'll end up with after applying the patch.
        target_slug = (
            _normalise_public_slug(body.public_slug)
            if new_slug_field_present
            else m.public_slug
        )
        target_enabled = (
            body.public_enabled if new_enabled_field_present else m.public_enabled
        )
        if target_enabled and not target_slug:
            raise HTTPException(
                status_code=400,
                detail="public_slug is required to enable public viewing",
            )
        if target_slug:
            # Uniqueness check — case-insensitively (slug is already lower).
            clash = (
                db.query(Meeting)
                .filter(Meeting.public_slug == target_slug)
                .filter(Meeting.id != m.id)
                .first()
            )
            if clash:
                raise HTTPException(
                    status_code=409,
                    detail="this public name is already in use; pick another",
                )
        m.public_slug = target_slug
        m.public_enabled = bool(target_enabled)
        # Note: `public_enabled` is decoupled from `list_for_anonymous` /
        # `list_for_authenticated`. Public viewing (View button → /public/<slug>)
        # is a separate capability from anonymous joining (Join button →
        # /<room_name> lobby). The owner controls each independently via
        # the visibility cycle (Globe icon) and the in-meeting Public group.

    db.commit()

    # If a livestream is running and the destination URLs changed, push
    # the diff to LiveKit so the existing egress fans out to the new set
    # immediately — no stop/restart needed.
    if pre_urls is not None and m.livestream_egress_id:
        post_urls = set(_enabled_stream_urls(m))
        to_add = sorted(post_urls - pre_urls)
        to_remove = sorted(pre_urls - post_urls)
        if to_add or to_remove:
            from livekit import api as lkapi
            lk = livekit_api()
            try:
                await lk.egress.update_stream(
                    lkapi.UpdateStreamRequest(
                        egress_id=m.livestream_egress_id,
                        add_output_urls=to_add,
                        remove_output_urls=to_remove,
                    )
                )
            except Exception:
                # If the live update fails (egress already stopped,
                # network glitch, key validation) we just return success
                # for the meeting update. Next stream restart will pick
                # up the new set anyway.
                import logging as _log
                _log.getLogger(__name__).exception(
                    "live destination update failed for meeting %s", m.id
                )
            finally:
                await lk.aclose()

    # PiP toggle / overlay identity changed — push the new values into
    # LiveKit room metadata so every connected client gets the change
    # live. Both the in-meeting `PresenterSpotlight` and the egress
    # custom layout at `/egress-layout/pip` subscribe to
    # `RoomEvent.RoomMetadataChanged` and re-render on the fly:
    #
    #   - Live viewers: `PresenterSpotlight` renders main + overlay
    #     from raw room tracks (client-side composition).
    #   - Recording / RTMP livestream: LiveKit Egress's headless Chrome
    #     loads `/egress-layout/pip` which renders the same layout and
    #     the egress encodes the page render — server-side composition
    #     for output, identical visual layout as the live view.
    #
    # The compositor Puppeteer service that was meant to publish a
    # `composite-*` track back to the SFU is intentionally NOT called
    # — it's been shelved (see lengthy history of headless-Chrome
    # `getDisplayMedia` / `canvas.captureStream` quirks). The two
    # paths above already deliver PiP everywhere it needs to appear.
    if pip_changed:
        from app.services.egress_mgr import push_pip_metadata
        try:
            await push_pip_metadata(m)
        except Exception:
            import logging as _log
            _log.getLogger(__name__).exception(
                "push_pip_metadata failed for meeting %s", m.id
            )

    return _to_out(m).model_dump()


@router.post("/meetings/{meeting_id}/composite-token")
def composite_token(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Mint a publish-capable token for the compositor session of this
    meeting. Returns the LiveKit URL, the room name, the chosen overlay
    identity, and the JWT. Owner / co-host only.

    Until the compositor Docker service is in place (Pass 2 of the PiP
    rebuild), this endpoint also lets the host paste the resulting
    `/egress-layout/composite?...` URL into a browser tab to verify
    end-to-end that the composite track lands in the room.
    """
    from app.livekit_client import mint_composite_token
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not is_moderator(m, user.sub):
        raise HTTPException(status_code=404, detail="meeting not found")
    token = mint_composite_token(room_name=m.room_name)
    base = f"{settings.public_url}/egress-layout/composite"
    # Pre-build the URL the way LiveKit Egress would (room, token, url)
    # plus our `overlay` hint. Quote everything so a stray + or = doesn't
    # silently mangle the WebSocket URL.
    from urllib.parse import urlencode
    qs = urlencode({
        "room": m.room_name,
        "token": token,
        "url": settings.livekit_ws_url,
        "overlay": m.pip_overlay_identity or "",
    })
    return {
        "livekit_url": settings.livekit_ws_url,
        "token": token,
        "room_name": m.room_name,
        "overlay_identity": m.pip_overlay_identity,
        "composite_url": f"{base}?{qs}",
    }


@router.get("/meetings/{meeting_id}/camera-publishers")
async def list_camera_publishers(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> list[dict]:
    """Live list of participants currently publishing a camera in the
    meeting's LiveKit room. Powers the Picture-in-Picture overlay
    dropdown in the in-meeting settings panel. Owner / co-host only —
    same gating as the rest of `update_meeting`. Returns identity +
    display name; the chosen identity is persisted on the meeting via
    `PATCH /meetings/{id}` `pip_overlay_identity`."""
    from livekit import api as lkapi
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not is_moderator(m, user.sub):
        raise HTTPException(status_code=404, detail="meeting not found")
    lk = livekit_api()
    try:
        pr = await lk.room.list_participants(
            lkapi.ListParticipantsRequest(room=m.room_name)
        )
    except Exception:
        # Room not in LiveKit yet (no one joined since startup): nothing
        # to list. Return empty rather than erroring so the UI can still
        # surface "no cameras live" instead of a red banner.
        return []
    finally:
        await lk.aclose()

    out: list[dict] = []
    for p in pr.participants:
        # Skip the reserved playback ingress identity — it publishes a
        # video track but it's never a "real" webcam pick.
        if p.identity == "playback":
            continue
        # Only standard human participants; bots / SIP / agent / egress
        # workers have their own Kind values.
        if p.kind != lkapi.ParticipantInfo.Kind.STANDARD:
            continue
        has_cam = any(
            t.type == lkapi.TrackType.VIDEO
            and getattr(t, "source", None) == lkapi.TrackSource.CAMERA
            for t in p.tracks
        )
        if not has_cam:
            continue
        out.append({
            "identity": p.identity,
            "name": p.name or p.identity,
        })
    return out


@router.get("/discoverable")
def list_discoverable(user: RequireUser, db: Session = Depends(get_db)) -> list[dict]:
    """Active meetings owned by OTHER users that the current user is allowed
    to discover. A meeting surfaces if any of the visibility flags is on,
    OR if it has a public view-only page enabled (those are always
    listed in Discover so viewers without the direct URL can still find
    them). Returns the public projection only."""
    rows = (
        db.query(Meeting)
        .filter(Meeting.is_active.is_(True))
        .filter(Meeting.hidden.is_(False))
        .filter(Meeting.owner_user_id != user.sub)
        .filter(
            (Meeting.list_for_authenticated.is_(True))
            | (Meeting.list_for_anonymous.is_(True))
            | (Meeting.public_enabled.is_(True))
        )
        .order_by(Meeting.created_at.desc())
        .limit(50)
        .all()
    )
    return [_to_public_out(m, viewer_is_authenticated=True) for m in rows]


@router.get("/public-meetings")
def list_public_meetings(db: Session = Depends(get_db)) -> list[dict]:
    """Active meetings visible to unauthenticated visitors.
    Listing is opt-in: the owner must explicitly set
    `list_for_anonymous=True`.
    Having a public view-only page (`public_enabled=True`) does NOT
    automatically list the meeting here — those two are different
    concerns. A host may want a shareable `/public/<slug>` URL for
    their audience without their meeting appearing in the anonymous
    Discover panel of the home page. Authenticated Discover
    (`/discoverable`) still surfaces public-enabled meetings."""
    rows = (
        db.query(Meeting)
        .filter(Meeting.is_active.is_(True))
        .filter(Meeting.hidden.is_(False))
        .filter(Meeting.list_for_anonymous.is_(True))
        .order_by(Meeting.created_at.desc())
        .limit(50)
        .all()
    )
    return [_to_public_out(m, viewer_is_authenticated=False) for m in rows]


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
    # **NEVER** fall back to `user.email` here. This value is passed
    # to LiveKit `participant.name`, which is rendered on every
    # viewer's tile + the participants panel + chat — leaking the
    # host's email address to everyone in the room (including
    # anonymous joiners and public viewers).
    #
    # Priority instead:
    #   1. The owner-specified display name stored on the meeting
    #      (owners only; cohosts don't write to `owner_name`).
    #   2. The User row's own `name` / `username` field (covers SSO
    #      users who set a name on one.witysk.org as well as native
    #      meet accounts).
    #   3. A generic `User <sub>` placeholder.
    db_user = db.get(User, user.user_id)
    fallback_name = (db_user.name or db_user.username) if db_user else None
    display_name = (
        (m.owner_name if is_real_owner else None)
        or fallback_name
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
    # If the moderator already has an active participant row (page refresh,
    # reconnect, or fresh token after cohost promotion), reuse it. The
    # partial-unique index `ux_meeting_participants_active` would otherwise
    # raise IntegrityError on the INSERT and 500 the entire token mint,
    # which caused the "joining the meeting fails / very long delay"
    # symptom in production logs.
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
                display_name=display_name,
                email=stored_email,
                is_authenticated=True,
                is_owner=is_real_owner,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            # Lost the race against the participant_joined webhook or a
            # concurrent token mint; the row is now there either way.
            db.rollback()
    else:
        # Keep the display name + role on the existing row fresh in case
        # cohost was just promoted to owner or the owner renamed.
        existing.display_name = display_name
        existing.email = stored_email
        existing.is_owner = is_real_owner
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
async def add_cohost(
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
    was_already_cohost = body.user_sub in cur
    cur.add(body.user_sub)
    _save_cohosts(m, cur)
    db.commit()

    # If this is a fresh promotion (not idempotent re-add) and the
    # promoted user is in the room right now, notify them so they can
    # rejoin and pick up a moderator token. The Lobby's existing
    # cohost-detection branch mints the right token on rejoin; without
    # this notice the promoted user has no idea they need to refresh.
    if not was_already_cohost:
        import json as _json
        from livekit import api as _lkapi
        target_identity = f"user-{body.user_sub}"
        lk = livekit_api()
        try:
            await lk.room.send_data(
                _lkapi.SendDataRequest(
                    room=m.room_name,
                    data=_json.dumps({"v": 1, "type": "promoted"}).encode("utf-8"),
                    kind=_lkapi.DataPacket.Kind.RELIABLE,
                    topic="meet-cohost",
                    destination_identities=[target_identity],
                )
            )
        except Exception:
            # Best-effort — they just won't see the toast. The crown badge
            # still appears in the participants panel on next refresh.
            pass
        finally:
            await lk.aclose()

    return {"ok": True, "cohosts": sorted(cur)}


@router.delete("/meetings/{meeting_id}/cohosts/{user_sub}")
async def remove_cohost(
    meeting_id: str,
    user_sub: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    cur = _cohost_set(m)
    was_cohost = user_sub in cur
    cur.discard(user_sub)
    _save_cohosts(m, cur)
    db.commit()

    # Mirror of add_cohost: when we actually remove someone (not an
    # idempotent re-discard) and they're in the room right now, send
    # them a data-channel signal so the SPA can show a rejoin banner.
    # Their existing LiveKit token still carries `room_admin` — the
    # moderator toolbar would otherwise stay active even though their
    # role is gone — so until they rejoin to pick up a guest-level
    # token, calls they make against owner-only endpoints would succeed
    # at LiveKit but be refused by our HTTP layer. Notification + rejoin
    # makes the state consistent end-to-end.
    if was_cohost:
        import json as _json
        from livekit import api as _lkapi
        target_identity = f"user-{user_sub}"
        lk = livekit_api()
        try:
            await lk.room.send_data(
                _lkapi.SendDataRequest(
                    room=m.room_name,
                    data=_json.dumps({"v": 1, "type": "demoted"}).encode("utf-8"),
                    kind=_lkapi.DataPacket.Kind.RELIABLE,
                    topic="meet-cohost",
                    destination_identities=[target_identity],
                )
            )
        except Exception:
            # Best-effort; on next refresh the Lobby will issue an
            # anon-token (no longer a cohost) and the UI corrects itself.
            pass
        finally:
            await lk.aclose()

    return {"ok": True, "cohosts": sorted(cur)}

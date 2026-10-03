"""Meet++ authentication.

Three principals:
  - chair: user JWT + owner/co-host (is_moderator). Like the rest of the app,
    non-moderators get 404 rather than 403.
  - room participant: the signed LiveKit room token (X-Meet-Room-Token),
    verified with LIVEKIT_API_SECRET. Guests have no app JWT but do have this.
  - internal: the agent, HMAC-signed with MEETPP_INTERNAL_SECRET.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from livekit import api as lk_api
from sqlalchemy.orm import Session

from app.auth import AuthUser, RequireUser
from app.config import settings
from app.db import get_db
from app.meetpp.models import MeetppSession
from app.models import Meeting

HMAC_WINDOW_SECONDS = 60

_verifier = lk_api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)


@dataclass
class RoomPrincipal:
    identity: str
    name: str
    room: str
    read_only: bool = False
    room_admin: bool = False
    claims: object | None = None


@dataclass
class ChairContext:
    meeting: Meeting
    session: MeetppSession
    user: AuthUser
    is_owner: bool
    extra: dict = field(default_factory=dict)


def is_moderator(m: Meeting, user_sub: str | None) -> bool:
    """Delegate to the canonical moderator check so Meet++ can never disagree
    with the rest of the app about who is a co-host."""
    from app.routes.meetings import is_moderator as _canonical

    return _canonical(m, user_sub)


def meetpp_allowed(meeting: Meeting) -> bool:
    """Global kill switch + pilot allow-list, re-checked on every Meet++
    request so disabling Meet++ stops existing sessions too."""
    if not settings.meetpp_enabled:
        return False
    pilots = settings.meetpp_pilot_owner_subs or []
    return not pilots or meeting.owner_user_id in pilots


def verify_room_token(token: str) -> lk_api.Claims:
    try:
        claims = _verifier.verify(token)
    except Exception as exc:  # noqa: BLE001 — jose raises several types
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid room token: {exc}",
        ) from exc
    return claims


def principal_from_claims(claims: lk_api.Claims, expected_room: str) -> RoomPrincipal:
    video = claims.video
    if video is None or not video.room_join:
        raise HTTPException(status_code=401, detail="room token lacks room_join")
    if video.room != expected_room:
        raise HTTPException(status_code=403, detail="room token is for a different room")
    identity = claims.identity or ""
    # Egress / public viewer tokens are read-only. The egress page's token has
    # recorder=True; viewer tokens use the `viewer-` identity prefix.
    recorder = bool(getattr(video, "recorder", False))
    read_only = recorder or identity.startswith("viewer-") or identity.startswith("egress-")
    return RoomPrincipal(
        identity=identity,
        name=claims.name or identity,
        room=video.room,
        read_only=read_only,
        room_admin=bool(video.room_admin),
        claims=claims,
    )


# ─── Dependencies ──────────────────────────────────────────────────────────


def require_room_token(
    x_meet_room_token: Annotated[str | None, Header()] = None,
) -> lk_api.Claims:
    if not x_meet_room_token:
        raise HTTPException(status_code=401, detail="missing X-Meet-Room-Token")
    return verify_room_token(x_meet_room_token)


def require_room_session(
    session_id: str,
    x_meet_room_token: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> tuple[MeetppSession, RoomPrincipal]:
    """Resolve a session by id and verify the caller's room token matches its
    meeting room. No Meet++ data is reachable by room name alone."""
    if not x_meet_room_token:
        raise HTTPException(status_code=401, detail="missing X-Meet-Room-Token")
    session = db.get(MeetppSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    meeting = db.get(Meeting, session.meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="session not found")
    if not meetpp_allowed(meeting):
        raise HTTPException(status_code=404, detail="Meet++ is not enabled")
    claims = verify_room_token(x_meet_room_token)
    principal = principal_from_claims(claims, meeting.room_name)
    return session, principal


async def require_chair_session(
    session_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> ChairContext:
    session = db.get(MeetppSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    meeting = db.get(Meeting, session.meeting_id)
    if meeting is None or not is_moderator(meeting, user.sub):
        raise HTTPException(status_code=404, detail="session not found")
    if not meetpp_allowed(meeting):
        raise HTTPException(status_code=404, detail="Meet++ is not enabled")
    if user.is_admin is False and user.kind == "native":
        # Native users without an active entitlement cannot operate Meet++.
        raise HTTPException(status_code=403, detail="admin rights required")
    return ChairContext(
        meeting=meeting,
        session=session,
        user=user,
        is_owner=meeting.owner_user_id == user.sub,
    )


def require_meeting_chair(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None or not is_moderator(meeting, user.sub):
        raise HTTPException(status_code=404, detail="meeting not found")
    return meeting


# ─── Editors ───────────────────────────────────────────────────────────────


def session_editors(session: MeetppSession) -> set[str]:
    try:
        v = json.loads(session.editors_json or "[]")
        if isinstance(v, list):
            return {str(x) for x in v}
    except ValueError:
        pass
    return set()


def principal_can_edit(session: MeetppSession, meeting: Meeting, principal: RoomPrincipal) -> bool:
    if principal.read_only:
        return False
    if principal.room_admin:
        return True
    if principal.identity in session_editors(session):
        return True
    if principal.identity.startswith("user-"):
        sub = principal.identity[len("user-"):]
        return is_moderator(meeting, sub)
    return False


# ─── Internal HMAC ─────────────────────────────────────────────────────────


def internal_signature(timestamp: str, body: bytes) -> str:
    msg = timestamp.encode("utf-8") + b"." + body
    return hmac.new(
        (settings.meetpp_internal_secret or "").encode("utf-8"),
        msg,
        hashlib.sha256,
    ).hexdigest()


def verify_internal_signature(timestamp: str | None, signature: str | None, body: bytes) -> None:
    if not settings.meetpp_internal_secret:
        raise HTTPException(status_code=503, detail="internal API disabled")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="missing HMAC headers")
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="bad timestamp") from None
    if abs(time.time() - ts) > HMAC_WINDOW_SECONDS:
        raise HTTPException(status_code=401, detail="stale timestamp")
    expected = internal_signature(timestamp, body)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="bad signature")


async def require_internal(
    request: Request,
    x_meetpp_timestamp: Annotated[str | None, Header()] = None,
    x_meetpp_signature: Annotated[str | None, Header()] = None,
) -> None:
    body = await request.body()
    verify_internal_signature(x_meetpp_timestamp, x_meetpp_signature, body)

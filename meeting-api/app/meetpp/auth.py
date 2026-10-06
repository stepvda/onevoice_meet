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
    if principal.identity in session_editors(session):
        return True
    if principal.identity.startswith("user-"):
        sub = principal.identity[len("user-"):]
        return is_moderator(meeting, sub)
    return False


# ─── Internal HMAC ─────────────────────────────────────────────────────────


# Signatures accepted within the window (signature → expiry): a captured
# request cannot be replayed.
_seen_signatures: dict[str, float] = {}


def internal_signature(timestamp: str, method: str, target: str, body: bytes) -> str:
    """v2: HMAC-SHA256 over the version, timestamp, method, raw path (with
    `?query` when there is one) and the body's SHA-256."""
    msg = ("v2\n" + timestamp + "\n" + method.upper() + "\n" + target + "\n").encode() + hashlib.sha256(body).hexdigest().encode()
    return hmac.new(
        (settings.meetpp_internal_secret or "").encode("utf-8"),
        msg,
        hashlib.sha256,
    ).hexdigest()


def verify_internal_signature(
    timestamp: str | None, signature: str | None, method: str, target: str, body: bytes
) -> None:
    if not settings.meetpp_internal_secret:
        raise HTTPException(status_code=503, detail="internal API disabled")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="missing HMAC headers")
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="bad timestamp") from None
    now = time.time()
    if abs(now - ts) > HMAC_WINDOW_SECONDS:
        raise HTTPException(status_code=401, detail="stale timestamp")
    expected = internal_signature(timestamp, method, target, body)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="bad signature")
    for sig, expiry in list(_seen_signatures.items()):
        if expiry < now:
            del _seen_signatures[sig]
    if expected in _seen_signatures:
        raise HTTPException(status_code=401, detail="replayed request")
    # Kept until the timestamp can no longer pass the window check.
    _seen_signatures[expected] = ts + HMAC_WINDOW_SECONDS + 1


def request_target(request: Request) -> str:
    """The raw request path, plus `?query` when there is one (as signed)."""
    raw = request.scope.get("raw_path")
    # ASGI servers give the path alone; some test clients append the query.
    path = raw.decode("latin-1").split("?", 1)[0] if raw else request.url.path
    query = request.scope.get("query_string") or b""
    return path + ("?" + query.decode("latin-1") if query else "")


async def require_internal(
    request: Request,
    x_meetpp_timestamp: Annotated[str | None, Header()] = None,
    x_meetpp_signature: Annotated[str | None, Header()] = None,
) -> None:
    body = await request.body()
    verify_internal_signature(x_meetpp_timestamp, x_meetpp_signature, request.method, request_target(request), body)


# ─── Room token or chair JWT ────────────────────────────────────────────────


@dataclass
class Access:
    """Caller of a live endpoint: a room participant (LiveKit room token) or
    a chair using the app JWT (review screen, outside the room)."""

    session: MeetppSession
    meeting: Meeting
    principal: RoomPrincipal | None
    user: AuthUser | None
    is_chair: bool
    can_edit: bool
    read_only: bool
    identity: str
    name: str
    # Anonymous viewer of the public page (`viewer-` identity).
    viewer: bool = False


def resolve_access(session_id: str, request: Request, db: Session) -> Access:
    from app.auth import require_user

    session = db.get(MeetppSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    meeting = db.get(Meeting, session.meeting_id)
    if meeting is None or not meetpp_allowed(meeting):
        raise HTTPException(status_code=404, detail="session not found")
    token = request.headers.get("x-meet-room-token")
    if token:
        principal = principal_from_claims(verify_room_token(token), meeting.room_name)
        viewer = principal.identity.startswith("viewer-")
        if viewer and not _show_public(session):
            raise HTTPException(status_code=404, detail="session not found")
        sub = principal.identity[5:] if principal.identity.startswith("user-") else None
        # Chair rights follow the meeting's current owner/co-hosts, not the
        # token's room_admin claim (a removed co-host keeps that for hours).
        is_chair = not principal.read_only and sub is not None and is_moderator(meeting, sub)
        can_edit = is_chair or (not principal.read_only and principal.identity in session_editors(session))
        return Access(
            session=session, meeting=meeting, principal=principal, user=None, is_chair=is_chair,
            can_edit=can_edit, read_only=principal.read_only, identity=principal.identity, name=principal.name,
            viewer=viewer,
        )
    authorization = request.headers.get("authorization")
    if authorization:
        user = require_user(authorization, db)
        if not is_moderator(meeting, user.sub):
            raise HTTPException(status_code=404, detail="session not found")
        return Access(
            session=session, meeting=meeting, principal=None, user=user, is_chair=True, can_edit=True,
            read_only=False, identity=f"user-{user.sub}", name=user.email or user.sub,
        )
    raise HTTPException(status_code=401, detail="missing X-Meet-Room-Token")


def _show_public(session: MeetppSession) -> bool:
    try:
        return bool(json.loads(session.settings_json or "{}").get("show_public", False))
    except (ValueError, AttributeError):
        return False


def room_access(session_id: str, request: Request, db: Session = Depends(get_db)) -> Access:
    return resolve_access(session_id, request, db)


def edit_access(session_id: str, request: Request, db: Session = Depends(get_db)) -> Access:
    acc = resolve_access(session_id, request, db)
    if not acc.can_edit:
        raise HTTPException(status_code=404, detail="session not found")
    return acc


def chair_access(session_id: str, request: Request, db: Session = Depends(get_db)) -> Access:
    acc = resolve_access(session_id, request, db)
    if not acc.is_chair:
        raise HTTPException(status_code=404, detail="session not found")
    return acc

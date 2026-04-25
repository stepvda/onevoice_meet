"""
TI Café — an always-on audio room.

Anyone authenticated via one.witysk.org can join. The LiveKit room name is
fixed (`ti-cafe`); LiveKit auto-creates it on first join and closes it after
its empty-timeout — functionally always-open from the user's perspective.

Live presence: the SPA wants to show a "LIVE" badge on every user who is
currently connected, including for viewers who haven't joined themselves.
We track that as an in-memory set of user_ids, mutated by LiveKit webhooks
(participant_joined / participant_left). The set is reconciled with LiveKit's
`list_participants` on first read after process start so a meeting-api
restart doesn't show stale data.
"""
from __future__ import annotations

import asyncio
from threading import Lock
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import RequireUser
from app.config import settings
from app.db import get_db
from app.livekit_client import livekit_api, mint_participant_token, short_lived_turn_credentials

router = APIRouter(prefix="/v1")

# Fixed room name. Anyone arriving with a token whose `room` claim is this
# joins the same audio call.
ROOM_NAME = "ti-cafe"

# user-<id>  →  user_id (int). One entry per live participant. We key on the
# LiveKit identity so participant_left can find the row without us tracking
# user_id separately on the join event.
_live: dict[str, int] = {}
_lock = Lock()
_seeded = False  # one-shot startup reconciliation


def _identity_for(user_id: str) -> str:
    return f"user-{user_id}"


def _user_id_from_identity(identity: str) -> Optional[int]:
    if not identity.startswith("user-"):
        return None
    try:
        return int(identity[len("user-"):])
    except ValueError:
        return None


def mark_joined(identity: str) -> None:
    uid = _user_id_from_identity(identity)
    if uid is None:
        return
    with _lock:
        _live[identity] = uid


def mark_left(identity: str) -> None:
    with _lock:
        _live.pop(identity, None)


def clear_room(room_name: str) -> None:
    """Wipe every entry for a room when LiveKit reports `room_finished`."""
    if room_name != ROOM_NAME:
        return
    with _lock:
        _live.clear()


async def _seed_from_livekit() -> None:
    """Populate the in-memory set from LiveKit's authoritative participant
    list. Called once, lazily, on the first /live read after startup."""
    global _seeded
    if _seeded:
        return
    lk = livekit_api()
    try:
        from livekit import api as lkapi  # local import — module-level keeps file decoupled

        try:
            res = await lk.room.list_participants(lkapi.ListParticipantsRequest(room=ROOM_NAME))
            with _lock:
                _live.clear()
                for p in res.participants:
                    uid = _user_id_from_identity(p.identity)
                    if uid is not None:
                        _live[p.identity] = uid
        except Exception:  # noqa: BLE001 — room may not exist yet; that's fine
            pass
    finally:
        await lk.aclose()
        _seeded = True


def is_ti_cafe_room(room_name: Optional[str]) -> bool:
    return room_name == ROOM_NAME


class TokenResponse(BaseModel):
    livekit_url: str
    token: str
    room_name: str
    ice_servers: dict | None


class LiveResponse(BaseModel):
    user_ids: list[int]


@router.post("/ti-cafe/token")
def mint_ti_cafe_token(user: RequireUser, db: Session = Depends(get_db)) -> TokenResponse:
    """Mint a LiveKit token for the always-on TI Café audio room."""
    _ = db  # unused for now; signature kept for symmetry with other routes
    identity = _identity_for(user.user_id)
    display_name = user.email or f"User {user.user_id}"
    token = mint_participant_token(
        room_name=ROOM_NAME,
        identity=identity,
        display_name=display_name,
        is_owner=False,
    )
    return TokenResponse(
        livekit_url=settings.livekit_ws_url,
        token=token,
        room_name=ROOM_NAME,
        ice_servers=short_lived_turn_credentials(identity),
    )


@router.get("/ti-cafe/live")
async def get_live(_: RequireUser) -> LiveResponse:
    """Return the user_ids currently connected to TI Café. Auth-only — TI
    Café is for signed-in users."""
    if not _seeded:
        await _seed_from_livekit()
    with _lock:
        ids = sorted(set(_live.values()))
    return LiveResponse(user_ids=ids)


@router.post("/ti-cafe/refresh")
async def refresh_live(_: RequireUser) -> LiveResponse:
    """Force a re-seed from LiveKit. Useful if the in-memory set looks stale
    (e.g. after a meeting-api restart, before any webhook event has fired)."""
    global _seeded
    _seeded = False
    await _seed_from_livekit()
    with _lock:
        ids = sorted(set(_live.values()))
    return LiveResponse(user_ids=ids)

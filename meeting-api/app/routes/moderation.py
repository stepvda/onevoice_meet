"""
Moderator endpoints — Phase 7: wired to the LiveKit server API.

All actions require ownership of the meeting (owner_user_id == JWT sub).
The LiveKit-side enforcement still relies on the owner's token carrying
`roomAdmin`; these REST routes are the channel the browser uses to trigger
server-side actions without holding the LiveKit API secret.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from livekit import api
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import RequireUser
from app.db import get_db
from app.livekit_client import livekit_api
from app.models import Meeting, ModerationAudit

router = APIRouter(prefix="/v1")


class MuteBody(BaseModel):
    participant_identity: str
    track_sid: str | None = None
    mute: bool = True


class KickBody(BaseModel):
    participant_identity: str


class PresenterBody(BaseModel):
    participant_identity: str | None = None


def _require_owner(meeting_id: str, user_id: str, db: Session) -> Meeting:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    if not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")
    return m


def _audit(db: Session, meeting_id: str, actor: str, action: str, target: str | None = None, details: str | None = None) -> None:
    db.add(ModerationAudit(meeting_id=meeting_id, actor_user_id=actor, action=action, target_identity=target, details=details))
    db.commit()


async def _mute_all_audio_tracks(lk: api.LiveKitAPI, room_name: str, identity: str, muted: bool) -> int:
    """Mute every audio track published by `identity`. Returns count muted."""
    pr = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
    count = 0
    for p in pr.participants:
        if p.identity != identity:
            continue
        for t in p.tracks:
            if t.type == api.TrackType.AUDIO:
                await lk.room.mute_published_track(
                    api.MuteRoomTrackRequest(room=room_name, identity=identity, track_sid=t.sid, muted=muted)
                )
                count += 1
    return count


@router.post("/meetings/{meeting_id}/mute")
async def mute(meeting_id: str, body: MuteBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    lk = livekit_api()
    try:
        if body.track_sid:
            await lk.room.mute_published_track(
                api.MuteRoomTrackRequest(
                    room=m.room_name,
                    identity=body.participant_identity,
                    track_sid=body.track_sid,
                    muted=body.mute,
                )
            )
            count = 1
        else:
            count = await _mute_all_audio_tracks(lk, m.room_name, body.participant_identity, body.mute)
    finally:
        await lk.aclose()
    _audit(db, m.id, user.sub, "mute", target=body.participant_identity, details=f"mute={body.mute},count={count}")
    return {"ok": True, "tracks_affected": count}


class LowerHandBody(BaseModel):
    participant_identity: str


@router.post("/meetings/{meeting_id}/lower-hand")
async def lower_hand(
    meeting_id: str,
    body: LowerHandBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Owner-only: clear `handRaised` from another participant's metadata via
    the LiveKit admin API. The participant client sees a
    ParticipantMetadataChanged event and the UI updates without needing a
    cooperative response from the target."""
    m = _require_owner(meeting_id, user.sub, db)
    lk = livekit_api()
    try:
        pr = await lk.room.list_participants(api.ListParticipantsRequest(room=m.room_name))
        target = next(
            (p for p in pr.participants if p.identity == body.participant_identity),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail="participant not in room")
        try:
            current = json.loads(target.metadata) if target.metadata else {}
            if not isinstance(current, dict):
                current = {}
        except ValueError:
            current = {}
        current.pop("handRaised", None)
        current.pop("handRaisedAt", None)
        await lk.room.update_participant(
            api.UpdateParticipantRequest(
                room=m.room_name,
                identity=body.participant_identity,
                metadata=json.dumps(current),
            )
        )
    finally:
        await lk.aclose()
    _audit(db, m.id, user.sub, "lower_hand", target=body.participant_identity)
    return {"ok": True}


@router.post("/meetings/{meeting_id}/kick")
async def kick(meeting_id: str, body: KickBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    lk = livekit_api()
    try:
        await lk.room.remove_participant(
            api.RoomParticipantIdentity(room=m.room_name, identity=body.participant_identity)
        )
    finally:
        await lk.aclose()
    _audit(db, m.id, user.sub, "kick", target=body.participant_identity)
    return {"ok": True}


async def _update_metadata(lk: api.LiveKitAPI, room_name: str, **patch) -> None:
    """Merge-update the room's metadata dict."""
    rooms = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
    current: dict = {}
    if rooms.rooms:
        try:
            current = json.loads(rooms.rooms[0].metadata or "{}")
        except ValueError:
            current = {}
    current.update(patch)
    await lk.room.update_room_metadata(
        api.UpdateRoomMetadataRequest(room=room_name, metadata=json.dumps(current))
    )


@router.post("/meetings/{meeting_id}/mute-all")
async def mute_all(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    """Mute every audio track of every non-owner participant in the room."""
    m = _require_owner(meeting_id, user.sub, db)
    owner_identity = f"user-{user.sub}"
    lk = livekit_api()
    affected = 0
    try:
        pr = await lk.room.list_participants(api.ListParticipantsRequest(room=m.room_name))
        for p in pr.participants:
            if p.identity == owner_identity:
                continue
            for t in p.tracks:
                if t.type == api.TrackType.AUDIO:
                    await lk.room.mute_published_track(
                        api.MuteRoomTrackRequest(
                            room=m.room_name, identity=p.identity, track_sid=t.sid, muted=True
                        )
                    )
                    affected += 1
    finally:
        await lk.aclose()
    _audit(db, m.id, user.sub, "mute_all", details=f"count={affected}")
    return {"ok": True, "tracks_muted": affected}


@router.post("/meetings/{meeting_id}/presenter")
async def set_presenter(meeting_id: str, body: PresenterBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    lk = livekit_api()
    try:
        await _update_metadata(lk, m.room_name, presenter_identity=body.participant_identity)
    finally:
        await lk.aclose()
    _audit(db, m.id, user.sub, "presenter", target=body.participant_identity)
    return {"ok": True, "presenter_identity": body.participant_identity}

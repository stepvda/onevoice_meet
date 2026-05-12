"""In-meeting video playback orchestration.

The host uploads MP4s into a per-meeting playlist (`PlaybackItem` rows).
When the host clicks Play, this module:
  1. Picks the first item.
  2. Calls LiveKit Ingress (URL_INPUT) with a signed local HTTP URL so
     the ingress container streams the file out as a "playback" participant
     track every viewer sees.
  3. Server-side mutes every other participant's mic + camera.
  4. Sends a `playback-start` data-channel signal so the SPA hides the
     grid and pins the playback participant for everyone.

When the current ingress ends (the LiveKit `ingress_ended` webhook calls
`advance_to_next`), we pick the next item by `position`; if there is none,
we end playback. If `Meeting.playback_loop` is True we wrap back to the
first item instead.

Stopping is the reverse: delete the ingress, broadcast `playback-end`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

from fastapi import HTTPException
from livekit import api
from sqlalchemy.orm import Session

from app.config import settings
from app.livekit_client import livekit_api
from app.models import Meeting, ModerationAudit, PlaybackItem

log = logging.getLogger(__name__)

# The participant identity / name the ingress publishes as. Reserved — we
# refuse to issue tokens with this identity to real users (see tokens.py
# guard if added) and the SPA can rely on this string to find the track.
PLAYBACK_IDENTITY = "playback"

# Internal URL the ingress container fetches the file from. Same docker
# network — caddy isn't involved. The ?token=… is an HMAC-signed expiry
# so the URL isn't a free-floating credential.
_INGRESS_INTERNAL_BASE = "http://meeting-api:8080"

# How long the signed download URL stays valid. Plenty of time for ingress
# to fetch the file plus some buffer for retries.
_PLAYBACK_URL_TTL_SECONDS = 30 * 60


def _sign_playback_url(item_id: str) -> str:
    """Build a token that only the ingress container needs to verify back —
    we sign with the existing JWT secret so we don't need a new env var.
    Format: `<expiry_unix>.<hex32>`."""
    exp = int(time.time()) + _PLAYBACK_URL_TTL_SECONDS
    payload = f"playback:{item_id}:{exp}".encode()
    mac = hmac.new(settings.jwt_secret_key.encode(), payload, hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{mac}"


def verify_playback_url(item_id: str, token: str) -> bool:
    """Used by the file-serving route to validate ?token=…"""
    try:
        exp_str, mac = token.split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    payload = f"playback:{item_id}:{exp}".encode()
    expected = hmac.new(settings.jwt_secret_key.encode(), payload, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(mac, expected)


def _build_signed_url(item: PlaybackItem) -> str:
    token = _sign_playback_url(item.id)
    return f"{_INGRESS_INTERNAL_BASE}/api/v1/internal/playback/{item.id}?token={token}"


async def _mute_all_other_participants(lk: "api.LiveKitAPI", room_name: str) -> None:
    """Server-side mute the published mic + camera tracks of every
    participant in the room EXCEPT the playback identity. We use the
    LiveKit MuteRoomTrackRequest which forces the SFU to stop forwarding
    the track; the SPA also receives the data-channel signal and toggles
    its own button state so the UI stays consistent."""
    try:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
    except Exception:
        log.exception("playback: failed to list participants for mute-all")
        return
    for p in res.participants:
        if p.identity == PLAYBACK_IDENTITY:
            continue
        # Only mute real human participants. Egress / Ingress / SIP / agent
        # workers also show up here and don't have user-controlled mics —
        # touching them risks suppressing the wrong track.
        if p.kind != api.ParticipantInfo.Kind.STANDARD:
            continue
        for t in p.tracks:
            if t.muted:
                continue
            try:
                await lk.room.mute_published_track(
                    api.MuteRoomTrackRequest(
                        room=room_name, identity=p.identity, track_sid=t.sid, muted=True
                    )
                )
            except Exception:
                # Best-effort — if one participant's mute fails (e.g. they
                # left between list_participants and mute) we keep going.
                log.exception("playback: mute_published_track failed for %s", p.identity)


async def _broadcast(room_name: str, signal: str, payload: dict) -> None:
    """Send a small JSON packet on the LiveKit data channel so the SPA can
    react to playback-start / playback-end without re-polling. The packet
    rides the room's reliable transport so it survives brief disconnects."""
    body = {"v": 1, "type": signal, **payload}
    lk = livekit_api()
    try:
        await lk.room.send_data(
            api.SendDataRequest(
                room=room_name,
                data=json.dumps(body).encode("utf-8"),
                kind=api.DataPacket.Kind.RELIABLE,
                topic="meet-playback",
            )
        )
    except Exception:
        log.exception("playback: broadcast %s failed", signal)
    finally:
        await lk.aclose()


async def _start_ingress_for_item(
    m: Meeting,
    item: PlaybackItem,
    db: Session,
    *,
    initial: bool,
) -> str:
    """Create a fresh LiveKit ingress for `item`, store its id on the
    meeting, and (only on the very first item of a play session) mute all
    other participants + broadcast the playback-start signal. Returns the
    new ingress_id."""
    url = _build_signed_url(item)
    lk = livekit_api()
    try:
        ingress = await lk.ingress.create_ingress(
            api.CreateIngressRequest(
                input_type=api.IngressInput.URL_INPUT,
                name=f"playback-{item.id}",
                room_name=m.room_name,
                participant_identity=PLAYBACK_IDENTITY,
                participant_name=item.filename,
                url=url,
            )
        )
        if initial:
            await _mute_all_other_participants(lk, m.room_name)
    finally:
        await lk.aclose()

    m.playback_ingress_id = ingress.ingress_id
    m.playback_current_item_id = item.id
    db.commit()

    await _broadcast(
        m.room_name,
        "playback-start" if initial else "playback-next",
        {"item_id": item.id, "filename": item.filename},
    )
    return ingress.ingress_id


async def start_playback(m: Meeting, user_sub: str, db: Session) -> dict:
    """Public entrypoint called by POST /playback:start."""
    if not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")
    if not m.playback_enabled:
        raise HTTPException(status_code=400, detail="video playback not enabled for this meeting")
    if m.playback_ingress_id:
        raise HTTPException(status_code=409, detail="playback already in progress")
    # Egress + ingress concurrency limit on the 2-vCPU host: recording
    # (RoomCompositeEgress with file output) + ingress (URL_INPUT
    # transcode) saturates CPU. Livestreaming is allowed (one egress slot
    # shared across destinations). The user-facing rule: stop recording
    # before starting video playback.
    from app.models import Recording
    running_rec = (
        db.query(Recording).filter_by(meeting_id=m.id, status="running").first()
    )
    if running_rec:
        raise HTTPException(
            status_code=409,
            detail="recording is active — stop recording before starting video playback",
        )
    first = (
        db.query(PlaybackItem)
        .filter_by(meeting_id=m.id)
        .order_by(PlaybackItem.position.asc())
        .first()
    )
    if not first:
        raise HTTPException(status_code=400, detail="playlist is empty")

    ingress_id = await _start_ingress_for_item(m, first, db, initial=True)
    db.add(
        ModerationAudit(
            meeting_id=m.id,
            actor_user_id=user_sub,
            action="playback_start",
            details=f"{first.id}:{ingress_id}",
        )
    )
    db.commit()
    return {"ok": True, "ingress_id": ingress_id, "item_id": first.id}


async def stop_playback(m: Meeting, user_sub: str, db: Session) -> dict:
    """Public entrypoint called by POST /playback:stop."""
    ingress_id = m.playback_ingress_id
    if not ingress_id:
        # Idempotent — see the stop_recording / stop_stream pattern.
        return {"ok": True, "already_stopped": True}

    lk = livekit_api()
    try:
        try:
            await lk.ingress.delete_ingress(api.DeleteIngressRequest(ingress_id=ingress_id))
        except Exception:
            # Even if delete fails (e.g. ingress already ended), clear our
            # state and broadcast end so the UI doesn't get stuck.
            log.exception("playback: delete_ingress failed for %s", ingress_id)
    finally:
        await lk.aclose()

    m.playback_ingress_id = None
    m.playback_current_item_id = None
    db.commit()

    await _broadcast(m.room_name, "playback-end", {})
    db.add(
        ModerationAudit(
            meeting_id=m.id,
            actor_user_id=user_sub,
            action="playback_stop",
            details=ingress_id,
        )
    )
    db.commit()
    return {"ok": True}


async def advance_after_ingress_ended(
    ingress_id: str,
    db: Session,
) -> Optional[str]:
    """Webhook hook: the ingress just ended on its own. Look up the meeting
    that owns this ingress, pick the next playlist item (or loop back),
    start a fresh ingress for it. Return the new ingress_id or None if we
    actually ended playback."""
    m = db.query(Meeting).filter_by(playback_ingress_id=ingress_id).first()
    if not m:
        return None  # not a playback ingress, or already cleaned up

    cur = (
        db.query(PlaybackItem).filter_by(id=m.playback_current_item_id).first()
        if m.playback_current_item_id
        else None
    )

    next_item: Optional[PlaybackItem] = None
    if cur:
        next_item = (
            db.query(PlaybackItem)
            .filter(PlaybackItem.meeting_id == m.id, PlaybackItem.position > cur.position)
            .order_by(PlaybackItem.position.asc())
            .first()
        )
    if not next_item and m.playback_loop:
        next_item = (
            db.query(PlaybackItem)
            .filter_by(meeting_id=m.id)
            .order_by(PlaybackItem.position.asc())
            .first()
        )

    if not next_item:
        # No next item and not looping — end the playback cleanly.
        m.playback_ingress_id = None
        m.playback_current_item_id = None
        db.commit()
        await _broadcast(m.room_name, "playback-end", {})
        return None

    return await _start_ingress_for_item(m, next_item, db, initial=False)

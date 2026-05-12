"""Live-stream-to-RTMP(S) endpoints.

The owner of a meeting configures an RTMPS URL + stream key on the meeting
(typically pointed at studio.x.com or any other RTMP ingest), then toggles
the in-meeting Start/Stop streaming button. Start asks LiveKit Egress to
composite the room and forward it to the configured RTMP URL; Stop calls
StopEgress. Final state is reconciled by the `egress_ended` webhook
(shared with the recording feature) which clears `livestream_egress_id`.

The URL+key pair is concatenated as `<url>/<key>` for the StreamOutput.urls
list — that's the form every major RTMP ingest expects, including X/Twitter
(via studio.x.com), Twitch, YouTube Live, and Facebook Live.
"""
from fastapi import APIRouter, Depends, HTTPException
from livekit import api
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import RequireUser
from app.db import get_db
from app.livekit_client import livekit_api
from app.models import Meeting, ModerationAudit
from app.routes.recordings import RecordingLayout, _encoding_options

router = APIRouter(prefix="/v1")


class StartStreamBody(BaseModel):
    # Same layout vocabulary as recording — no LiveKit-specific overlay
    # exists for the RTMP path, so we reuse the room-composite templates.
    layout: RecordingLayout = "speaker"


def _require_owner(meeting_id: str, user_id: str, db: Session) -> Meeting:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    return m


def _build_stream_url(rtmps_url: str, stream_key: str) -> str:
    """Concatenate ingest URL + key as `<url>/<key>`. Tolerates trailing
    slashes on the URL and leading slashes on the key so the owner can paste
    either form from studio.x.com."""
    return rtmps_url.rstrip("/") + "/" + stream_key.lstrip("/")


@router.post("/meetings/{meeting_id}/stream:start")
async def start_stream(
    meeting_id: str,
    user: RequireUser,
    body: StartStreamBody | None = None,
    db: Session = Depends(get_db),
) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    if not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")
    if not m.livestream_enabled:
        raise HTTPException(status_code=400, detail="livestream not enabled for this meeting")
    if not m.livestream_rtmps_url or not m.livestream_stream_key:
        raise HTTPException(status_code=400, detail="rtmps url and stream key required")
    if m.livestream_egress_id:
        raise HTTPException(status_code=409, detail="livestream already in progress")

    layout: RecordingLayout = body.layout if body else "speaker"
    url = _build_stream_url(m.livestream_rtmps_url, m.livestream_stream_key)
    lk = livekit_api()
    try:
        egress_info = await lk.egress.start_room_composite_egress(
            api.RoomCompositeEgressRequest(
                room_name=m.room_name,
                layout=layout,
                stream_outputs=[
                    api.StreamOutput(protocol=api.StreamProtocol.RTMP, urls=[url])
                ],
                advanced=_encoding_options(),
            )
        )
    finally:
        await lk.aclose()

    m.livestream_egress_id = egress_info.egress_id
    db.add(ModerationAudit(meeting_id=m.id, actor_user_id=user.sub, action="stream_start", details=egress_info.egress_id))
    db.commit()
    return {"ok": True, "egress_id": egress_info.egress_id}


@router.post("/meetings/{meeting_id}/stream:stop")
async def stop_stream(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    egress_id = m.livestream_egress_id
    if not egress_id:
        raise HTTPException(status_code=404, detail="no active livestream")

    lk = livekit_api()
    try:
        await lk.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
    finally:
        await lk.aclose()

    # Clear immediately; the webhook is just a safety net for crash recovery.
    m.livestream_egress_id = None
    db.add(ModerationAudit(meeting_id=m.id, actor_user_id=user.sub, action="stream_stop", details=egress_id))
    db.commit()
    return {"ok": True}


@router.get("/meetings/{meeting_id}/stream")
def get_stream_status(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    return {
        "enabled": bool(m.livestream_enabled),
        "active": bool(m.livestream_egress_id),
        "egress_id": m.livestream_egress_id,
    }

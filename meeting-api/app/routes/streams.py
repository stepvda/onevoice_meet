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
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import RequireUser
from app.db import get_db
from app.models import Meeting, Recording
from app.routes.recordings import RecordingLayout
from app.services.egress_mgr import reconcile_egress

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
    # Preserve any active recording — reconcile_egress restarts the egress
    # with both outputs so we stay within a single worker slot.
    keep_file = bool(
        db.query(Recording).filter_by(meeting_id=m.id, status="running").first()
    )
    result = await reconcile_egress(
        m,
        want_file=keep_file,
        want_stream=True,
        layout=layout,
        user_sub=user.sub,
        db=db,
    )
    return {"ok": True, "egress_id": result["egress_id"]}


@router.post("/meetings/{meeting_id}/stream:stop")
async def stop_stream(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    if not m.livestream_egress_id:
        raise HTTPException(status_code=404, detail="no active livestream")

    # If recording is also riding the same egress, restart with file-only so
    # the recording continues. Otherwise just stop.
    running_rec = (
        db.query(Recording)
        .filter_by(meeting_id=m.id, egress_id=m.livestream_egress_id, status="running")
        .first()
    )
    keep_file = bool(running_rec)
    await reconcile_egress(
        m,
        want_file=keep_file,
        want_stream=False,
        layout=None,
        user_sub=user.sub,
        db=db,
    )
    return {"ok": True}


@router.get("/meetings/{meeting_id}/stream")
def get_stream_status(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    return {
        "enabled": bool(m.livestream_enabled),
        "active": bool(m.livestream_egress_id),
        "egress_id": m.livestream_egress_id,
    }

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
    """Accepts owner OR co-host. Same broadening as recordings.py — the
    in-meeting co-host UX requires parity with the host for streaming,
    recording, and video playback controls."""
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    from app.routes.meetings import is_moderator
    if not is_moderator(m, user_id):
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
    # Meeting must have at least one enabled destination with creds. Each
    # destination has its own toggle; "streaming enabled" is the union.
    from app.services.egress_mgr import _enabled_stream_urls
    if not _enabled_stream_urls(m):
        raise HTTPException(
            status_code=400,
            detail="no livestream destination configured (enable X.com / Substack / YouTube / Facebook / Rumble with rtmps url + key)",
        )
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
        # Idempotent: see comment in stop_recording. Treating "already off" as
        # success means a stale SPA / double-click / mid-egress-ended-webhook
        # click doesn't surface as a confusing 404 toast.
        return {"ok": True, "already_stopped": True}

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


@router.get("/meetings/{meeting_id}/stream/destinations")
def get_stream_destinations(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> list[dict]:
    """Per-destination publish status. One row per platform that is
    currently *enabled* on the meeting. Status comes from the latest
    `egress_updated` webhook event LiveKit sent for the running (or
    most-recently-stopped) egress.

    Vocabulary (mirrors `LivestreamDestinationState.status`):
      - "idle"      : credentials present, no egress has reported yet
      - "streaming" : RTMP push to this destination is healthy
      - "failed"    : the destination rejected the publish; `error`
                      carries the egress's reason
      - "complete"  : the destination finished cleanly (last stream ended)
    """
    from app.models import LivestreamDestinationState
    from app.services.egress_mgr import LIVESTREAM_DESTINATIONS

    m = _require_owner(meeting_id, user.sub, db)
    states_by_platform = {
        s.platform_id: s
        for s in db.query(LivestreamDestinationState).filter_by(meeting_id=m.id).all()
    }
    out: list[dict] = []
    for platform, en_attr, _url_attr, _key_attr in LIVESTREAM_DESTINATIONS:
        if not bool(getattr(m, en_attr, False)):
            continue
        st = states_by_platform.get(platform)
        out.append({
            "platform_id": platform,
            "status": st.status if st else "idle",
            "error": st.error if st else None,
            "updated_at": st.updated_at.isoformat() if st and st.updated_at else None,
        })
    return out

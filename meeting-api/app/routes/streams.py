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
import logging

from fastapi import APIRouter, Depends, HTTPException
from livekit import api as lk_api
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import RequireUser
from app.db import get_db
from app.livekit_client import livekit_api
from app.models import Meeting, Recording
from app.routes.recordings import RecordingLayout
from app.services.egress_mgr import reconcile_egress

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)


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

    requested: RecordingLayout = body.layout if body else "speaker"
    # Playback override: while a video is playing the egress is locked
    # to single-speaker so the playback participant owns the frame. The
    # host's requested layout is stashed on the meeting and restored
    # when playback ends.
    layout: RecordingLayout = requested
    if m.playback_ingress_id:
        if m.layout_before_playback is None:
            m.layout_before_playback = requested
            db.commit()
        layout = "single-speaker"

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
    # Best-effort: politely complete the YouTube broadcast so the public
    # video page shows "stream ended" instead of "the broadcaster has
    # not pushed any data" for the next hour. We clear the meeting-row
    # pointers regardless of API success so the next Start call creates
    # a fresh broadcast.
    if (
        bool(m.livestream_youtube_enabled)
        and (m.livestream_youtube_mode or "rtmp") == "api"
        and m.livestream_youtube_broadcast_id
    ):
        from app.services import youtube_live
        try:
            await youtube_live.complete_broadcast(m)
        except Exception:  # noqa: BLE001
            log.exception("youtube complete broadcast on stop failed")
        m.livestream_youtube_broadcast_id = None
        m.livestream_youtube_broadcast_started_at = None
        m.livestream_youtube_watch_url = None
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


@router.get("/meetings/{meeting_id}/stream/destinations")
async def get_stream_destinations(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> list[dict]:
    """Per-destination publish status. One row per platform that is
    currently *enabled* on the meeting.

    While a livestream egress is active we live-read `stream_results`
    from LiveKit's egress API on every poll, so the SPA's coloured dot
    reflects the actual publish state within ~4s. Webhook-cached state
    is used as the fallback once the egress is gone (e.g. completed /
    failed final state for the most-recent stream).

    Live-reading bypasses two webhook timing issues:
      1. `egress_updated` events firing late or not at all in this
         egress build, leaving the cached state stuck on "idle".
      2. The meeting's `livestream_egress_id` getting cleared by Stop
         before `egress_ended` arrives, so the webhook's URL→platform
         match fails and the "complete" transition is lost.

    Vocabulary:
      - "idle"      : credentials present, RTMP push hasn't reported yet
      - "streaming" : RTMP push to this destination is healthy
      - "failed"    : the destination rejected the publish (`error` set)
      - "complete"  : last stream ended cleanly
    """
    from datetime import datetime, timezone
    from app.models import LivestreamDestinationState
    from app.services.egress_mgr import LIVESTREAM_DESTINATIONS

    m = _require_owner(meeting_id, user.sub, db)
    enabled_platforms: list[tuple[str, str]] = []
    prefixes: list[tuple[str, str]] = []
    for platform, en_attr, url_attr, _key_attr in LIVESTREAM_DESTINATIONS:
        if not bool(getattr(m, en_attr, False)):
            continue
        enabled_platforms.append((platform, en_attr))
        url = getattr(m, url_attr, None)
        if url:
            prefixes.append((url.rstrip("/"), platform))
    prefixes.sort(key=lambda p: len(p[0]), reverse=True)

    # Live-read pass when a stream egress is active. We map each
    # reported URL back to its platform using the same URL-prefix
    # strategy as the webhook handler (LiveKit redacts the
    # stream-key portion of the reported URL).
    live: dict[str, dict] = {}
    if m.livestream_egress_id:
        try:
            cli = livekit_api()
            try:
                resp = await cli.egress.list_egress(
                    lk_api.ListEgressRequest(egress_ids=[m.livestream_egress_id])
                )
            finally:
                await cli.aclose()
            for info in resp.items:
                for sr in (info.stream_results or []):
                    url = (getattr(sr, "url", "") or "")
                    platform = next(
                        (p for prefix, p in prefixes if url.startswith(prefix)),
                        None,
                    )
                    if not platform:
                        continue
                    raw = getattr(sr, "status", None)
                    try:
                        st_int = int(raw) if raw is not None else -1
                    except (TypeError, ValueError):
                        st_int = -1
                    if st_int == 1:
                        status = "streaming"
                    elif st_int == 2:
                        status = "failed"
                    elif st_int == 3:
                        status = "complete"
                    else:
                        status = "idle"
                    err = (getattr(sr, "error", "") or "").strip()[:500] or None
                    live[platform] = {
                        "status": status,
                        "error": err,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
        except Exception:  # noqa: BLE001
            log.exception("stream/destinations: live egress read failed eg=%s", m.livestream_egress_id)
            # Fall through to webhook-cached state below.

    states_by_platform = {
        s.platform_id: s
        for s in db.query(LivestreamDestinationState).filter_by(meeting_id=m.id).all()
    }
    out: list[dict] = []
    for platform, _en_attr in enabled_platforms:
        cached = states_by_platform.get(platform)
        # Viewer count is only ever populated for YouTube API-mode rows
        # (no other platform exposes a count via a stream-key API). It
        # comes from the supervisor's last poll, regardless of whether
        # we have live egress data this tick.
        viewer_count = cached.viewer_count if cached else None
        viewer_count_at = (
            cached.viewer_count_at.isoformat() if cached and cached.viewer_count_at else None
        )
        if platform in live:
            out.append({
                "platform_id": platform,
                **live[platform],
                "viewer_count": viewer_count,
                "viewer_count_at": viewer_count_at,
            })
            continue
        out.append({
            "platform_id": platform,
            "status": cached.status if cached else "idle",
            "error": cached.error if cached else None,
            "updated_at": cached.updated_at.isoformat() if cached and cached.updated_at else None,
            "viewer_count": viewer_count,
            "viewer_count_at": viewer_count_at,
        })
    return out

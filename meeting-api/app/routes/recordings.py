"""
Recording endpoints — Phase 10.

Recording start asks the LiveKit Egress service to composite the room to an
MP4 on the local /out volume. A Recording row is created with status='running';
the final size/duration/status are filled in by the LiveKit webhook handler
when the `egress_ended` event arrives.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from livekit import api
from pydantic import BaseModel
from sqlalchemy.orm import Session, contains_eager, joinedload
from ulid import ULID

from app.auth import RequireUser
from app.config import settings
from app.db import get_db
from app.livekit_client import livekit_api
from app.models import Meeting, ModerationAudit, Recording
from app.routes.meetings import _branding_url

router = APIRouter(prefix="/v1")


def _require_owner(meeting_id: str, user_id: str, db: Session) -> Meeting:
    """Accepts the owner OR any co-host of the meeting. The name is kept
    for backwards compatibility with existing call sites; "moderator"
    would be more accurate. Recording / livestream / playback parity
    with the host is part of the co-host contract."""
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    from app.routes.meetings import is_moderator
    if not is_moderator(m, user_id):
        raise HTTPException(status_code=404, detail="meeting not found")
    return m


async def _set_recording_metadata(lk: api.LiveKitAPI, room_name: str, active: bool) -> None:
    rooms = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
    current: dict = {}
    if rooms.rooms:
        try:
            current = json.loads(rooms.rooms[0].metadata or "{}")
        except ValueError:
            current = {}
    current["recording_active"] = active
    await lk.room.update_room_metadata(
        api.UpdateRoomMetadataRequest(room=room_name, metadata=json.dumps(current))
    )


# LiveKit's built-in room-composite templates. "speaker" is the historical
# default; "grid" mirrors the live grid view; "single-speaker" shows only the
# active speaker with no thumbnails. The encoding profile that goes with
# either recording or livestreaming lives in app/services/egress_mgr.py
# (single source of truth, shared by both code paths).
RecordingLayout = Literal["speaker", "grid", "single-speaker"]


class StartRecordingBody(BaseModel):
    layout: RecordingLayout = "speaker"


@router.post("/meetings/{meeting_id}/recordings:start")
async def start_recording(
    meeting_id: str,
    user: RequireUser,
    body: StartRecordingBody | None = None,
    db: Session = Depends(get_db),
) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    if not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")
    if m.playback_ingress_id:
        # See playback_mgr.start_playback comment — recording + ingress
        # together saturates the 2-vCPU box. Livestreaming is allowed to
        # coexist with playback (single egress slot, multiple destinations).
        raise HTTPException(
            status_code=409,
            detail="video playback is running — stop playback before starting a recording",
        )

    layout: RecordingLayout = body.layout if body else "speaker"
    existing = db.query(Recording).filter_by(meeting_id=m.id, status="running").first()
    if existing:
        raise HTTPException(status_code=409, detail="recording already in progress")

    # Preserve the stream output if one is active — reconcile_egress will
    # stop+restart with the combined output set so we never run two
    # concurrent egress jobs on a 2-vCPU box.
    keep_stream = bool(m.livestream_egress_id)
    from app.services.egress_mgr import reconcile_egress
    result = await reconcile_egress(
        m,
        want_file=True,
        want_stream=keep_stream,
        layout=layout,
        user_sub=user.sub,
        db=db,
    )
    return {"ok": True, "recording_id": result["recording_id"], "egress_id": result["egress_id"]}


@router.post("/meetings/{meeting_id}/recordings:stop")
async def stop_recording(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = _require_owner(meeting_id, user.sub, db)
    rec = db.query(Recording).filter_by(meeting_id=m.id, status="running").first()
    if not rec:
        # Idempotent: from the user's POV the state is already "stopped".
        # Returning 404 here makes the SPA appear stuck on a Stop button
        # that "doesn't work" — log shows e.g. four retries in 22 s.
        return {"ok": True, "recording_id": None, "already_stopped": True}

    # If a stream is also active on this egress, restart with stream-only so
    # the broadcast continues. Otherwise just stop everything.
    keep_stream = bool(m.livestream_egress_id) and m.livestream_egress_id == rec.egress_id
    from app.services.egress_mgr import reconcile_egress
    await reconcile_egress(
        m,
        want_file=False,
        want_stream=keep_stream,
        layout=None,  # reuse the layout the egress was started with
        user_sub=user.sub,
        db=db,
    )
    return {"ok": True, "recording_id": rec.id}


@router.get("/meetings/{meeting_id}/recordings")
def list_meeting_recordings(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> list[dict]:
    _require_owner(meeting_id, user.sub, db)
    rows = (
        db.query(Recording)
        .options(joinedload(Recording.meeting))
        .filter(Recording.meeting_id == meeting_id)
        .filter(Recording.status != "deleted")
        .order_by(Recording.started_at.desc())
        .all()
    )
    return [_recording_out(r) for r in rows]


@router.get("/recordings")
def list_all_my_recordings(user: RequireUser, db: Session = Depends(get_db)) -> list[dict]:
    """Lists the user's recordings. Hides rows with status='deleted' (the file
    is gone, the row only exists for audit). YouTube-published rows are kept
    visible because they still carry a useful URL."""
    # `contains_eager` instead of `joinedload` so the explicit join below is
    # the one that hydrates `Recording.meeting` — using both made SQLAlchemy
    # re-issue per-row meeting lookups in some versions.
    rows = (
        db.query(Recording)
        .join(Meeting, Meeting.id == Recording.meeting_id)
        .options(contains_eager(Recording.meeting))
        .filter(Meeting.owner_user_id == user.sub)
        .filter(Recording.status != "deleted")
        .order_by(Recording.started_at.desc())
        .all()
    )
    return [_recording_out(r) for r in rows]


@router.get("/recordings/{rec_id}/transcript")
def download_transcript(rec_id: str, user: RequireUser, db: Session = Depends(get_db)) -> FileResponse:
    r = (
        db.query(Recording)
        .join(Meeting, Meeting.id == Recording.meeting_id)
        .filter(Recording.id == rec_id, Meeting.owner_user_id == user.sub)
        .first()
    )
    if not r:
        raise HTTPException(status_code=404, detail="recording not found")
    if not r.transcript_path:
        raise HTTPException(status_code=404, detail="transcript not available")
    path = Path(r.transcript_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="transcript file missing")
    return FileResponse(
        path=str(path),
        media_type="text/plain; charset=utf-8",
        filename=path.name,
    )


@router.get("/recordings/{rec_id}/download")
def download_recording(rec_id: str, user: RequireUser, db: Session = Depends(get_db)) -> FileResponse:
    r = (
        db.query(Recording)
        .join(Meeting, Meeting.id == Recording.meeting_id)
        .filter(Recording.id == rec_id, Meeting.owner_user_id == user.sub)
        .first()
    )
    if not r:
        raise HTTPException(status_code=404, detail="recording not found")
    if r.status == "deleted" or not r.file_path:
        raise HTTPException(status_code=410, detail="recording has expired and been deleted")
    path = Path(r.file_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="recording file missing")
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        filename=path.name,
    )


class PublishYoutubeBody(BaseModel):
    title: str | None = None
    description: str | None = None
    privacy: str | None = None  # "public" | "unlisted" | "private"


@router.post("/recordings/{rec_id}/publish-youtube")
async def publish_to_youtube(
    rec_id: str,
    body: PublishYoutubeBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Manually upload a completed recording to YouTube. On success, the local
    file is deleted (the YouTube URL becomes the system of record) but the
    Recording row remains so it stays visible in the user's list."""
    from app.services.youtube import YouTubeError, upload_recording

    r = (
        db.query(Recording)
        .join(Meeting, Meeting.id == Recording.meeting_id)
        .filter(Recording.id == rec_id, Meeting.owner_user_id == user.sub)
        .first()
    )
    if not r:
        raise HTTPException(status_code=404, detail="recording not found")
    if r.status != "completed":
        raise HTTPException(status_code=409, detail=f"recording is {r.status}, not completed")
    if r.youtube_status == "published" and r.youtube_url:
        return {"ok": True, "already_published": True, "url": r.youtube_url}
    if not r.file_path:
        raise HTTPException(status_code=410, detail="local file is no longer available")
    path = Path(r.file_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="recording file missing on disk")

    # Default title/description from the meeting metadata. The Meeting was
    # already JOINed in the query above; use the eager-loaded relationship
    # instead of issuing a second SELECT.
    meeting = r.meeting
    default_title = body.title or (meeting.display_title if meeting else f"meet.witysk recording {r.id}")
    default_desc = body.description or (
        f"Recorded via meet.witysk.org\n"
        f"Meeting: {meeting.display_title if meeting else r.meeting_id}\n"
        f"Started: {r.started_at.isoformat() if r.started_at else 'unknown'}"
    )

    r.youtube_status = "uploading"
    r.youtube_error = None
    db.commit()

    try:
        result = await upload_recording(
            file_path=path,
            title=default_title,
            description=default_desc,
            privacy=body.privacy,
        )
    except YouTubeError as e:
        r.youtube_status = "failed"
        r.youtube_error = str(e)[:500]
        db.commit()
        raise HTTPException(status_code=502, detail=f"YouTube upload failed: {e}") from e
    except Exception as e:  # noqa: BLE001
        r.youtube_status = "failed"
        r.youtube_error = f"unexpected: {e}"[:500]
        db.commit()
        raise HTTPException(status_code=500, detail="unexpected upload error") from e

    # Success: store URL, delete local file, free disk.
    r.youtube_video_id = result.video_id
    r.youtube_url = result.url
    r.youtube_status = "published"
    try:
        path.unlink(missing_ok=True)
        r.file_path = None
    except OSError:
        # Don't fail the API call if the unlink itself fails — the upload
        # is the important part. The disk-cap job will clean up later.
        pass
    db.add(ModerationAudit(meeting_id=r.meeting_id, actor_user_id=user.sub, action="youtube_publish", details=result.video_id))
    db.commit()
    return {"ok": True, "url": result.url, "video_id": result.video_id}


@router.delete("/recordings/{rec_id}", status_code=204)
def delete_recording(rec_id: str, user: RequireUser, db: Session = Depends(get_db)) -> None:
    r = (
        db.query(Recording)
        .join(Meeting, Meeting.id == Recording.meeting_id)
        .filter(Recording.id == rec_id, Meeting.owner_user_id == user.sub)
        .first()
    )
    if not r:
        raise HTTPException(status_code=404, detail="recording not found")
    if r.file_path:
        Path(r.file_path).unlink(missing_ok=True)
    r.status = "deleted"
    r.file_path = None
    db.commit()


def _aware_utc(dt):
    """SQLite stores datetimes naive. Treat anything we read back as UTC so
    arithmetic with `datetime.now(timezone.utc)` is safe."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _recording_out(r: Recording) -> dict:
    now = datetime.now(timezone.utc)
    expires_at = _aware_utc(r.expires_at)
    expires_in = (expires_at - now).total_seconds() if expires_at else None
    # Friendly filename in the form "<room>-YYYYMMDD-HHMM.mp4". Prefer the
    # basename of `file_path` when the file still exists (it's authoritative);
    # otherwise reconstruct it from room_name + started_at so deleted /
    # YouTube-published rows still show a meaningful identifier.
    filename: str | None = None
    if r.file_path:
        filename = Path(r.file_path).name
    elif r.meeting and r.started_at:
        ts = _aware_utc(r.started_at)
        if ts:
            filename = f"{r.meeting.room_name}-{ts.strftime('%Y%m%d-%H%M%S')}.mp4"
    return {
        "id": r.id,
        "meeting_id": r.meeting_id,
        "meeting_title": r.meeting.display_title if r.meeting else None,
        "filename": filename,
        "branding_url": _branding_url(r.meeting) if r.meeting else None,
        "status": r.status,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "expires_at": r.expires_at,
        "expires_in_seconds": int(expires_in) if expires_in is not None else None,
        "file_size_bytes": r.file_size_bytes,
        "duration_seconds": r.duration_seconds,
        "has_local_file": bool(r.file_path),
        "youtube_url": r.youtube_url,
        "youtube_status": r.youtube_status,
        "youtube_error": r.youtube_error,
        "transcript_status": r.transcript_status,
        "has_transcript": bool(r.transcript_path),
    }


# Retention helper — called by scheduler.py
def cleanup_expired_recordings() -> int:
    from app.db import SessionLocal

    now = datetime.now(timezone.utc)
    deleted = 0
    with SessionLocal() as db:
        expired = (
            db.query(Recording)
            .filter(Recording.status == "completed", Recording.expires_at <= now)
            .all()
        )
        for r in expired:
            try:
                if r.file_path:
                    Path(r.file_path).unlink(missing_ok=True)
                r.status = "deleted"
                r.file_path = None
                deleted += 1
            except OSError:
                continue
        db.commit()
    return deleted


def disk_usage_ratio(path: str | Path) -> tuple[float, int, int]:
    """Returns (used_ratio, used_bytes, total_bytes) for the filesystem holding `path`."""
    import os

    st = os.statvfs(str(path))
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    return (used / total if total else 0.0, used, total)


def _recordings_dir_size(rec_dir: Path) -> int:
    """Sum of sizes of files directly in the recordings dir. Used to decide
    whether disk pressure is actually caused by recordings or by something
    else on the same filesystem (docker images, OS bloat, etc.)."""
    total = 0
    try:
        for p in rec_dir.iterdir():
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def enforce_disk_cap(target_ratio: float | None = None) -> dict:
    """If the filesystem holding recordings is at or above the configured cap
    (default 75%) AND the recordings dir is contributing meaningfully to that
    pressure, delete oldest completed recordings until below the cap.

    The "contributing meaningfully" guard is critical: when the disk is full
    because of docker build cache, OS logs, or other non-recording content,
    deleting every recording on the box won't get us under the cap. The old
    behaviour was to keep evicting until the list was empty (which is what
    wiped every old recording on 2026-05-11). Now we bail out instead and
    leave a warning so operators know to clean up the actual cause.

    Returns {"deleted": N, "freed_bytes": M, "before_ratio": ..., "after_ratio": ...}
    """
    from app.db import SessionLocal

    cap = target_ratio if target_ratio is not None else settings.recording_disk_cap_ratio
    rec_dir = Path(settings.recordings_dir)
    rec_dir.mkdir(parents=True, exist_ok=True)

    before_ratio, _, total = disk_usage_ratio(rec_dir)
    base_result = {
        "deleted": 0,
        "freed_bytes": 0,
        "before_ratio": before_ratio,
        "after_ratio": before_ratio,
        "cap": cap,
    }
    if before_ratio < cap:
        return base_result

    bytes_to_free = int((before_ratio - (cap - 0.05)) * total)
    bytes_to_free = max(bytes_to_free, 0)

    rec_dir_size = _recordings_dir_size(rec_dir)
    # If even wiping every recording would free less than half of what's
    # needed, the disk problem is somewhere else. Don't punish the user's
    # archive for the OS being fat. Operators get a logged warning instead.
    if rec_dir_size < bytes_to_free / 2:
        import logging
        logging.getLogger(__name__).warning(
            "disk-cap exceeded (%.0f%% >= %.0f%%) but recordings_dir is only "
            "%.1f MB of %.1f MB needed — refusing to evict (problem is elsewhere)",
            before_ratio * 100,
            cap * 100,
            rec_dir_size / 1e6,
            bytes_to_free / 1e6,
        )
        return {**base_result, "skipped": "recordings_too_small_to_help"}

    deleted = 0
    freed = 0
    with SessionLocal() as db:
        oldest = (
            db.query(Recording)
            .filter(
                Recording.status == "completed",
                Recording.file_path.isnot(None),
            )
            .order_by(Recording.started_at.asc())
            .all()
        )
        for r in oldest:
            if freed >= bytes_to_free:
                break
            try:
                p = Path(r.file_path)  # type: ignore[arg-type]
                if not p.exists():
                    r.status = "deleted"
                    r.file_path = None
                    continue
                size = p.stat().st_size
                p.unlink()
                r.status = "deleted"
                r.file_path = None
                freed += size
                deleted += 1
            except OSError:
                continue
        db.commit()

    after_ratio, _, _ = disk_usage_ratio(rec_dir)
    return {
        "deleted": deleted,
        "freed_bytes": freed,
        "before_ratio": before_ratio,
        "after_ratio": after_ratio,
        "cap": cap,
    }

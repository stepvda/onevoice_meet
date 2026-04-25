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

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from livekit import api
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from ulid import ULID

from app.auth import RequireUser
from app.config import settings
from app.db import get_db
from app.livekit_client import livekit_api
from app.models import Meeting, ModerationAudit, Recording
from app.routes.meetings import _branding_url

router = APIRouter(prefix="/v1")


def _require_owner(meeting_id: str, user_id: str, db: Session) -> Meeting:
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user_id).first()
    if not m:
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


# Encoding profile for live recordings.
#
# Goals: "decent" compression at low CPU. LiveKit Egress runs Chrome+ffmpeg
# under the hood and uses libx264 for H.264 encoding. We pick H.264 Main @
# 720p30, ~1500 kbps video + 128 kbps AAC. Main profile is broadly hardware-
# accelerated; 720p30 keeps encoder cost ~1 CPU core on the egress container.
#
# To bump to 1080p, set RECORDING_PRESET_1080P=1 in .env (still 30 fps; cost
# roughly doubles). Anything beyond is a cluster-sizing decision.
def _encoding_options() -> "api.EncodingOptions":
    if settings.recording_preset_1080p:
        return api.EncodingOptions(
            width=1920,
            height=1080,
            framerate=30,
            video_codec=api.VideoCodec.H264_MAIN,
            video_bitrate=3000,  # kbps
            audio_codec=api.AudioCodec.AAC,
            audio_bitrate=128,
            audio_frequency=48000,
            key_frame_interval=4.0,
        )
    return api.EncodingOptions(
        width=1280,
        height=720,
        framerate=30,
        video_codec=api.VideoCodec.H264_MAIN,
        video_bitrate=1500,
        audio_codec=api.AudioCodec.AAC,
        audio_bitrate=128,
        audio_frequency=48000,
        key_frame_interval=4.0,
    )


@router.post("/meetings/{meeting_id}/recordings:start")
async def start_recording(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = _require_owner(meeting_id, user.user_id, db)
    if not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")

    # One active recording per meeting at a time.
    existing = db.query(Recording).filter_by(meeting_id=m.id, status="running").first()
    if existing:
        raise HTTPException(status_code=409, detail="recording already in progress")

    # Disk cap: if the recordings volume is already at >= 75% (or the cap from
    # config), evict oldest completed recordings to make room before egress writes.
    enforce_disk_cap()

    rec_id = str(ULID())
    Path(settings.recordings_dir).mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    filepath = str(
        Path(settings.recordings_dir)
        / f"{m.room_name}-{started.strftime('%Y%m%d-%H%M')}.mp4"
    )

    lk = livekit_api()
    try:
        egress_info = await lk.egress.start_room_composite_egress(
            api.RoomCompositeEgressRequest(
                room_name=m.room_name,
                layout="speaker",
                file_outputs=[
                    api.EncodedFileOutput(
                        file_type=api.EncodedFileType.MP4,
                        filepath=filepath,
                        disable_manifest=True,
                    )
                ],
                advanced=_encoding_options(),
            )
        )
        try:
            await _set_recording_metadata(lk, m.room_name, True)
        except Exception:  # noqa: BLE001 — metadata update is best-effort
            pass
    finally:
        await lk.aclose()

    rec = Recording(
        id=rec_id,
        meeting_id=m.id,
        egress_id=egress_info.egress_id,
        file_path=filepath,
        started_at=started,
        expires_at=started + timedelta(days=settings.recording_retention_days),
        status="running",
    )
    db.add(rec)
    db.add(ModerationAudit(meeting_id=m.id, actor_user_id=user.user_id, action="recording_start", details=rec_id))
    db.commit()
    return {"ok": True, "recording_id": rec_id, "egress_id": egress_info.egress_id}


@router.post("/meetings/{meeting_id}/recordings:stop")
async def stop_recording(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    m = _require_owner(meeting_id, user.user_id, db)
    rec = db.query(Recording).filter_by(meeting_id=m.id, status="running").first()
    if not rec:
        raise HTTPException(status_code=404, detail="no active recording")

    lk = livekit_api()
    try:
        await lk.egress.stop_egress(api.StopEgressRequest(egress_id=rec.egress_id))
        try:
            await _set_recording_metadata(lk, m.room_name, False)
        except Exception:  # noqa: BLE001
            pass
    finally:
        await lk.aclose()

    db.add(ModerationAudit(meeting_id=m.id, actor_user_id=user.user_id, action="recording_stop", details=rec.id))
    db.commit()
    # status stays 'running' until the egress_ended webhook flips it to 'completed'.
    return {"ok": True, "recording_id": rec.id}


@router.get("/meetings/{meeting_id}/recordings")
def list_meeting_recordings(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> list[dict]:
    _require_owner(meeting_id, user.user_id, db)
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
    rows = (
        db.query(Recording)
        .options(joinedload(Recording.meeting))
        .join(Meeting, Meeting.id == Recording.meeting_id)
        .filter(Meeting.owner_user_id == user.user_id)
        .filter(Recording.status != "deleted")
        .order_by(Recording.started_at.desc())
        .all()
    )
    return [_recording_out(r) for r in rows]


@router.get("/recordings/{rec_id}/download")
def download_recording(rec_id: str, user: RequireUser, db: Session = Depends(get_db)) -> FileResponse:
    r = (
        db.query(Recording)
        .join(Meeting, Meeting.id == Recording.meeting_id)
        .filter(Recording.id == rec_id, Meeting.owner_user_id == user.user_id)
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
        .filter(Recording.id == rec_id, Meeting.owner_user_id == user.user_id)
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

    # Default title/description from the meeting metadata.
    meeting = db.query(Meeting).filter_by(id=r.meeting_id).first()
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
    db.add(ModerationAudit(meeting_id=r.meeting_id, actor_user_id=user.user_id, action="youtube_publish", details=result.video_id))
    db.commit()
    return {"ok": True, "url": result.url, "video_id": result.video_id}


@router.delete("/recordings/{rec_id}", status_code=204)
def delete_recording(rec_id: str, user: RequireUser, db: Session = Depends(get_db)) -> None:
    r = (
        db.query(Recording)
        .join(Meeting, Meeting.id == Recording.meeting_id)
        .filter(Recording.id == rec_id, Meeting.owner_user_id == user.user_id)
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
    return {
        "id": r.id,
        "meeting_id": r.meeting_id,
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


def enforce_disk_cap(target_ratio: float | None = None) -> dict:
    """If the recordings filesystem is at or above the configured cap (default 75%),
    delete oldest completed recordings until below the cap. Always called before
    starting a new recording AND from the nightly retention job.

    Returns {"deleted": N, "freed_bytes": M, "before_ratio": ..., "after_ratio": ...}
    """
    from app.db import SessionLocal

    cap = target_ratio if target_ratio is not None else settings.recording_disk_cap_ratio
    rec_dir = Path(settings.recordings_dir)
    rec_dir.mkdir(parents=True, exist_ok=True)

    before_ratio, _, total = disk_usage_ratio(rec_dir)
    if before_ratio < cap:
        return {"deleted": 0, "freed_bytes": 0, "before_ratio": before_ratio, "after_ratio": before_ratio}

    # How many bytes do we need to free to drop below cap, with a small safety margin
    # so back-to-back recordings don't immediately trigger the cap again.
    bytes_to_free = int((before_ratio - (cap - 0.05)) * total)
    bytes_to_free = max(bytes_to_free, 0)

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

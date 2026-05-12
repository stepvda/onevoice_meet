"""In-meeting video playback endpoints.

CRUD over the playlist (`/playback/items`) + control endpoints
(`:start` / `:stop`) + an internal file-serving route that LiveKit Ingress
calls with a signed URL.

Authorization model:
  - All write + control endpoints require the meeting owner (or a co-host
    via `is_moderator`).
  - The file-serving endpoint is open by design but gated on an
    HMAC-signed token (`playback_mgr.verify_playback_url`); the URL is
    constructed server-side at start time and never leaves the docker
    network in normal operation.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ulid import ULID

from app.auth import RequireUser
from app.config import settings
from app.db import get_db
from app.models import Meeting, PlaybackItem
from app.routes.meetings import is_moderator
from app.services.playback_mgr import (
    PLAYBACK_IDENTITY,
    start_playback,
    stop_playback,
    verify_playback_url,
)

router = APIRouter(prefix="/v1")

# Storage layout: /var/lib/meet/playback/<meeting_id>/<item_id>.mp4
_PLAYBACK_ROOT = Path(settings.recordings_dir).parent / "playback"

# Limits — keep modest to avoid bloating the host disk and the upload
# round-trip. A 15-minute 720p H.264 file at ~1.5 Mbps lands around 170 MB.
_MAX_FILE_BYTES = 500 * 1024 * 1024
_MAX_ITEMS_PER_MEETING = 20
_ALLOWED_CONTENT_TYPES = {"video/mp4", "video/quicktime", "video/x-m4v"}


def _require_moderator(meeting_id: str, user_sub: str, db: Session) -> Meeting:
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not is_moderator(m, user_sub):
        raise HTTPException(status_code=404, detail="meeting not found")
    return m


def _item_dir(meeting_id: str) -> Path:
    d = _PLAYBACK_ROOT / meeting_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _to_out(item: PlaybackItem) -> dict:
    return {
        "id": item.id,
        "position": item.position,
        "filename": item.filename,
        "file_size_bytes": item.file_size_bytes,
        "mime_type": item.mime_type,
        "uploaded_at": item.uploaded_at.isoformat() if item.uploaded_at else None,
    }


@router.get("/meetings/{meeting_id}/playback/items")
def list_playback_items(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> list[dict]:
    """Owner / co-host only. The playlist isn't surfaced to participants
    directly; they discover playback state via the data-channel signal."""
    _require_moderator(meeting_id, user.sub, db)
    rows = (
        db.query(PlaybackItem)
        .filter_by(meeting_id=meeting_id)
        .order_by(PlaybackItem.position.asc())
        .all()
    )
    return [_to_out(r) for r in rows]


@router.post("/meetings/{meeting_id}/playback/items", status_code=201)
async def upload_playback_item(
    meeting_id: str,
    user: RequireUser,
    file: Annotated[UploadFile, File(...)],
    filename: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> dict:
    """Multipart upload of one MP4. The host can override the displayed
    name via the optional `filename` form field — defaults to the
    uploaded file's own name. Streamed to disk so a 400 MB file doesn't
    sit in memory."""
    m = _require_moderator(meeting_id, user.sub, db)

    if file.content_type and file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail=f"unsupported type: {file.content_type}")

    existing_count = db.query(PlaybackItem).filter_by(meeting_id=m.id).count()
    if existing_count >= _MAX_ITEMS_PER_MEETING:
        raise HTTPException(
            status_code=413,
            detail=f"playlist is full ({_MAX_ITEMS_PER_MEETING} items max per meeting)",
        )

    new_id = str(ULID())
    dest = _item_dir(m.id) / f"{new_id}.mp4"
    total = 0
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_FILE_BYTES:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"file exceeds {_MAX_FILE_BYTES // (1024 * 1024)} MB cap",
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"upload failed: {e}") from e

    display_name = (filename or file.filename or new_id).strip() or new_id
    # Next available position = current item count (we just incremented).
    item = PlaybackItem(
        id=new_id,
        meeting_id=m.id,
        position=existing_count,
        filename=display_name[:200],
        file_path=str(dest),
        file_size_bytes=total,
        mime_type=file.content_type or "video/mp4",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _to_out(item)


@router.delete("/meetings/{meeting_id}/playback/items/{item_id}", status_code=204)
def delete_playback_item(
    meeting_id: str,
    item_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> None:
    m = _require_moderator(meeting_id, user.sub, db)
    item = db.query(PlaybackItem).filter_by(id=item_id, meeting_id=m.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    # Refuse to delete the item that's currently playing — caller should
    # stop playback first (or wait for it to finish).
    if m.playback_current_item_id == item.id:
        raise HTTPException(status_code=409, detail="item is currently playing")

    Path(item.file_path).unlink(missing_ok=True)
    deleted_position = item.position
    db.delete(item)
    db.commit()
    # Re-sequence positions so the list stays 0..N-1 with no gaps. Bulk
    # UPDATE keeps the trip count to one regardless of playlist size.
    from sqlalchemy import update
    db.execute(
        update(PlaybackItem)
        .where(PlaybackItem.meeting_id == m.id, PlaybackItem.position > deleted_position)
        .values(position=PlaybackItem.position - 1)
    )
    db.commit()


class ReorderBody(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=_MAX_ITEMS_PER_MEETING)


@router.put("/meetings/{meeting_id}/playback/items:reorder")
def reorder_playback_items(
    meeting_id: str,
    body: ReorderBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> list[dict]:
    """Replace the playlist order with the supplied id list. The list must
    contain every existing item for this meeting exactly once — partial
    reorderings are refused to keep the position invariant simple."""
    m = _require_moderator(meeting_id, user.sub, db)
    rows = db.query(PlaybackItem).filter_by(meeting_id=m.id).all()
    existing_ids = {r.id for r in rows}
    if set(body.item_ids) != existing_ids:
        raise HTTPException(
            status_code=400,
            detail="reorder list must contain every existing item exactly once",
        )
    by_id = {r.id: r for r in rows}
    for pos, iid in enumerate(body.item_ids):
        by_id[iid].position = pos
    db.commit()
    return [_to_out(by_id[iid]) for iid in body.item_ids]


@router.post("/meetings/{meeting_id}/playback:start")
async def playback_start(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    m = _require_moderator(meeting_id, user.sub, db)
    return await start_playback(m, user.sub, db)


@router.post("/meetings/{meeting_id}/playback:stop")
async def playback_stop(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    m = _require_moderator(meeting_id, user.sub, db)
    return await stop_playback(m, user.sub, db)


# ─── Internal file-fetch (called by livekit-ingress container) ────────


@router.get("/internal/playback/{item_id}")
def fetch_playback_file(
    item_id: str,
    token: Annotated[str, Query()],
    db: Session = Depends(get_db),
) -> FileResponse:
    """Open endpoint — gated on the HMAC token issued by playback_mgr at
    ingress-create time. Returns the MP4 with the right content-type so
    ffmpeg inside the ingress container streams it correctly."""
    if not verify_playback_url(item_id, token):
        raise HTTPException(status_code=403, detail="invalid or expired token")
    item = db.query(PlaybackItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    path = Path(item.file_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="file missing on disk")
    return FileResponse(
        path=str(path),
        media_type=item.mime_type or "video/mp4",
        filename=item.filename,
    )


# Re-exported so other modules (Room.tsx via meeting-out, etc.) can refer
# to the reserved playback identity without importing the service module.
__all__ = ["router", "PLAYBACK_IDENTITY"]

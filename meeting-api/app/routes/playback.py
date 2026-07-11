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

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ulid import ULID

from app.auth import RequireUser
from app.config import settings
from app.db import get_db
from app.models import Meeting, PlaybackItem
from app.routes.meetings import is_moderator, _branding_url
from app.services.playback_mgr import (
    PLAYBACK_IDENTITY,
    start_playback,
    stop_playback,
    verify_playback_url,
)

router = APIRouter(prefix="/v1")

# Storage layout: <playback_dir>/<meeting_id>/<item_id>.mp4
_PLAYBACK_ROOT = Path(settings.playback_dir)


def _ffprobe_duration(file_path: str) -> float | None:
    """Read the duration of an MP4 via ffprobe. ffmpeg/ffprobe are
    already in the meeting-api image (transcription uses ffmpeg). 5
    second timeout — a non-decodable container shouldn't hang the
    upload thread or the lazy-backfill background task."""
    import subprocess
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return None
        d = float(out.stdout.strip())
        return d if d > 0 else None
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, ValueError):
        return None


def _ffmpeg_seek_stream(file_path: str, t_seconds: float):
    """Generator that yields bytes from `ffmpeg -ss <t> -i <file> -c
    copy -f mpegts pipe:1`. We stream-copy H.264/AAC into MPEG-TS so
    there's no re-encode (CPU cheap). LiveKit Ingress's URL_INPUT
    uses GStreamer's `decodebin` which autodetects MPEG-TS.

    `-ss` placed BEFORE `-i` makes ffmpeg seek by demux-skipping
    (fast, slightly less accurate; lands at the nearest keyframe
    before T). For our use case (host scrubs the slider) that's the
    right trade-off — sub-second accuracy is fine and a re-encode
    accurate seek would peg one core."""
    import subprocess
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, float(t_seconds)):.3f}",
        "-i", file_path,
        "-c", "copy",
        "-f", "mpegts",
        "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _ffmpeg_freeze_stream(file_path: str, t_seconds: float):
    """Generator that yields an endless MPEG-TS stream holding the single
    frame at time T in `file_path`. Used by the server-side pause path:
    we replace the running ingress with one that points at this stream so
    every viewer keeps seeing the frame that was on screen when pause was
    clicked.

    Implementation: a two-stage ffmpeg pipeline in a single process.
       1. `-ss T -i src.mp4 -frames:v 1 ... ` extracts the frame near T.
       2. `loop` filter repeats it indefinitely; we encode the looped
          frames to H.264 + MPEG-TS at a low frame rate (5fps is fine —
          the frame doesn't change) and pipe to stdout.

    The encoder is `libx264 -tune stillimage -preset ultrafast` so it's
    cheap on CPU. We also generate a continuous silent AAC track so the
    receiving track has both audio and video — without audio LiveKit's
    pipeline can stutter when a no-audio stream is offered after an
    audio one. Ingress's GStreamer pipeline reads stdin / souphttpsrc the
    same way for both seek and freeze paths."""
    import subprocess
    # `-loop 1` doesn't work directly with MP4 input + seek; we use the
    # `loop` video filter instead. `loop=loop=-1:size=1` says "repeat the
    # first frame forever, in chunks of 1 frame". `trim` caps the output
    # duration; one hour is well beyond any realistic pause length.
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        # Decoder: seek and read one frame.
        "-ss", f"{max(0.0, float(t_seconds)):.3f}",
        "-i", file_path,
        # Silent audio source so the output stream has an audio track too.
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", "loop=loop=-1:size=1,trim=duration=14400,setpts=N/FRAME_RATE/TB",
        "-r", "5",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", "-ar", "48000", "-ac", "2",
        "-shortest",  # bounded by the trim'd video
        "-f", "mpegts",
        "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _backfill_duration(item_id: str) -> None:
    """Background task: probe and persist duration for a single item.
    Idempotent — re-runs are safe (file unchanged → same duration). On
    failure (no file, ffprobe error) we set the duration to NULL so the
    next list-call doesn't reschedule this item every poll.

    Also propagates the new duration onto any alias rows pointing at
    this source — without that, aliases born while the source's duration
    was still NULL stay NULL forever, which makes them invisible to the
    "What's up next" slide eligibility check."""
    from app.db import SessionLocal
    from sqlalchemy import update
    with SessionLocal() as db:
        item = db.query(PlaybackItem).filter_by(id=item_id).first()
        if not item or not item.file_path or item.source_item_id is not None:
            return
        # Aliases inherit duration from their source; we never probe an
        # alias because it has no file of its own.
        dur = _ffprobe_duration(item.file_path)
        if dur is not None:
            item.duration_seconds = dur
            db.execute(
                update(PlaybackItem)
                .where(
                    PlaybackItem.source_item_id == item.id,
                    PlaybackItem.duration_seconds.is_(None),
                )
                .values(duration_seconds=dur)
            )
            db.commit()

# Per-file size cap stays — protects the upload round-trip and the host
# disk. A 15-minute 720p H.264 file at ~1.5 Mbps lands around 170 MB.
# There is intentionally no per-playlist item count cap: the host
# decides; reasonable wall-clock total runtime constrains itself
# naturally via the disk-cap retention job.
_MAX_FILE_BYTES = 1024 * 1024 * 1024
# Reorder requests are size-bounded to a value comfortably above any
# realistic playlist length so a malformed request can't exhaust the
# server. Not user-visible.
_REORDER_MAX_ITEMS = 1000
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
        "duration_seconds": item.duration_seconds,
        "file_size_bytes": item.file_size_bytes,
        "mime_type": item.mime_type,
        "uploaded_at": item.uploaded_at.isoformat() if item.uploaded_at else None,
        "source_item_id": item.source_item_id,
    }


def _resolve_source(item: PlaybackItem, db: Session) -> PlaybackItem:
    """Return the file-owning row for a playlist item. For source rows
    that's the item itself; for aliases it's the row pointed to by
    `source_item_id`. Aliases never chain (we resolve to the root when
    one is created) so a single hop is always enough."""
    if item.source_item_id is None:
        return item
    src = db.query(PlaybackItem).filter_by(id=item.source_item_id).first()
    if src is None:
        # Source was deleted out from under the alias — caller surfaces
        # this as a 410 "file missing".
        return item
    return src


# Minimum length (seconds) for a playlist video to appear in On Demand.
_ON_DEMAND_MIN_SECONDS = 300  # 5 minutes


@router.get("/on-demand")
def list_on_demand(db: Session = Depends(get_db)) -> list[dict]:
    """Public, no-auth On Demand catalogue.

    One entry per ONGOING meeting (`is_active`) that has a public livestream
    (`public_enabled` + `public_slug`) AND a playlist containing videos
    longer than five minutes. Each entry lists those videos (deduplicated to
    unique source files, in playlist order) with a no-auth streaming URL.

    Powers the home-page "On Demand" section, which is reachable by
    anonymous visitors. The `public_enabled` gate is the meeting owner's
    opt-in to public viewing — only those meetings' files are exposed."""
    meetings = (
        db.query(Meeting)
        .filter(Meeting.is_active.is_(True))
        .filter(Meeting.hidden.is_(False))
        .filter(Meeting.public_enabled.is_(True))
        .filter(Meeting.public_slug.isnot(None))
        .order_by(Meeting.created_at.desc())
        .all()
    )
    out: list[dict] = []
    for m in meetings:
        items = (
            db.query(PlaybackItem)
            .filter_by(meeting_id=m.id)
            .order_by(PlaybackItem.position.asc())
            .all()
        )
        videos: list[dict] = []
        seen: set[str] = set()
        for it in items:
            src = _resolve_source(it, db)
            dur = it.duration_seconds if it.duration_seconds is not None else src.duration_seconds
            if dur is None or dur <= _ON_DEMAND_MIN_SECONDS:
                continue
            if not src.file_path or src.id in seen:
                continue
            seen.add(src.id)
            videos.append({
                "id": src.id,
                "filename": it.filename,
                "duration_seconds": dur,
                "stream_url": f"/api/v1/on-demand/items/{src.id}",
            })
        if videos:
            out.append({
                "room_name": m.room_name,
                "display_title": m.display_title,
                "public_slug": m.public_slug,
                "owner_name": m.owner_name,
                "branding_url": _branding_url(m),
                "videos": videos,
            })
    return out


@router.get("/on-demand/items/{item_id}")
def stream_on_demand_item(
    item_id: str,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    db: Session = Depends(get_db),
) -> Response:
    """Public, no-auth inline streaming of an On Demand playlist video.

    Gated identically to the `/on-demand` listing: the item must belong to
    an active, non-hidden, public-enabled meeting and be longer than five
    minutes. Honours HTTP Range requests (206) — same hand-rolled handling
    as the internal ingress route — so the browser <video> can seek;
    without it the player can only progressive-download from the start."""
    item = db.query(PlaybackItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    m = db.query(Meeting).filter_by(id=item.meeting_id).first()
    if not m or not m.is_active or m.hidden or not m.public_enabled:
        raise HTTPException(status_code=404, detail="item not found")
    source = _resolve_source(item, db)
    dur = source.duration_seconds if source.duration_seconds is not None else item.duration_seconds
    if dur is None or dur <= _ON_DEMAND_MIN_SECONDS:
        raise HTTPException(status_code=404, detail="item not found")
    if not source.file_path or not Path(source.file_path).exists():
        raise HTTPException(status_code=410, detail="file missing on disk")

    path = Path(source.file_path)
    file_size = path.stat().st_size
    media_type = source.mime_type or "video/mp4"
    common_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{item.filename}"',
    }
    if range_header is None:
        return FileResponse(
            path=str(path),
            media_type=media_type,
            filename=item.filename,
            headers=common_headers,
        )
    parsed = _parse_range_header(range_header, file_size)
    if parsed is None:
        return Response(
            status_code=416,
            headers={**common_headers, "Content-Range": f"bytes */{file_size}"},
        )
    start, end = parsed
    return StreamingResponse(
        _iter_file_range(path, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(end - start + 1),
        },
    )


@router.get("/meetings/{meeting_id}/playback/items")
def list_playback_items(
    meeting_id: str,
    user: RequireUser,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> list[dict]:
    """Owner / co-host only. The playlist isn't surfaced to participants
    directly; they discover playback state via the data-channel signal.

    Lazy duration backfill: any non-alias item with NULL duration gets a
    background ffprobe scheduled. The response returns whatever's
    currently in the DB (NULLs on first call); subsequent calls show
    durations as soon as the background tasks finish. Idempotent —
    once duration is set, the row isn't re-probed."""
    _require_moderator(meeting_id, user.sub, db)
    rows = (
        db.query(PlaybackItem)
        .filter_by(meeting_id=meeting_id)
        .order_by(PlaybackItem.position.asc())
        .all()
    )
    # Build a source-duration map so aliases whose source has been
    # backfilled-since-creation still show the right duration without
    # an extra query per row.
    src_duration: dict[str, float] = {
        r.id: r.duration_seconds
        for r in rows
        if r.source_item_id is None and r.duration_seconds is not None
    }
    for r in rows:
        if r.duration_seconds is None and r.source_item_id is None and r.file_path:
            background.add_task(_backfill_duration, r.id)

    out: list[dict] = []
    for r in rows:
        d = _to_out(r)
        if r.source_item_id and not d["duration_seconds"]:
            d["duration_seconds"] = src_duration.get(r.source_item_id)
        out.append(d)
    return out


@router.post("/meetings/{meeting_id}/playback/items", status_code=201)
async def upload_playback_item(
    meeting_id: str,
    user: RequireUser,
    file: Annotated[UploadFile, File(...)],
    filename: Annotated[str | None, Form()] = None,
    duration_seconds: Annotated[float | None, Form()] = None,
    db: Session = Depends(get_db),
) -> dict:
    """Multipart upload of one MP4. The host can override the displayed
    name via the optional `filename` form field — defaults to the
    uploaded file's own name. `duration_seconds` is set by the SPA
    from `HTMLVideoElement.duration` before upload so the panel can
    render a progress bar without ffprobe on the server. Streamed to
    disk so a 400 MB file doesn't sit in memory."""
    m = _require_moderator(meeting_id, user.sub, db)

    if file.content_type and file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail=f"unsupported type: {file.content_type}")

    existing_count = db.query(PlaybackItem).filter_by(meeting_id=m.id).count()
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
    # Duration: prefer the SPA-supplied value (cheap, no ffprobe roundtrip).
    # If absent or zero, fall back to server-side ffprobe so legacy
    # clients without the duration-probe code still get a populated bar.
    dur = duration_seconds if (duration_seconds and duration_seconds > 0) else None
    if dur is None:
        dur = _ffprobe_duration(str(dest))
    item = PlaybackItem(
        id=new_id,
        meeting_id=m.id,
        position=existing_count,
        filename=display_name[:200],
        file_path=str(dest),
        file_size_bytes=total,
        mime_type=file.content_type or "video/mp4",
        duration_seconds=dur,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _to_out(item)


@router.post("/meetings/{meeting_id}/playback/items/{item_id}:duplicate", status_code=201)
def duplicate_playback_item(
    meeting_id: str,
    item_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Create a playlist ALIAS that references an existing item's file —
    lets the host place the same video at multiple positions without
    duplicating the MP4 on disk. The alias goes to the end of the
    playlist; the host reorders it from there using the regular
    up/down or reorder calls."""
    m = _require_moderator(meeting_id, user.sub, db)
    src = db.query(PlaybackItem).filter_by(id=item_id, meeting_id=m.id).first()
    if not src:
        raise HTTPException(status_code=404, detail="item not found")
    existing_count = db.query(PlaybackItem).filter_by(meeting_id=m.id).count()
    # Resolve to the root source so aliases never chain — a single hop
    # is always enough at playback time and `delete` only has to walk
    # one set of dependants.
    root = _resolve_source(src, db)
    # If the source's duration hasn't been backfilled yet, probe now so
    # the alias is born with a real value. Aliases never get probed
    # (they have no file of their own) and the lazy-backfill task only
    # touches source rows — without this eager probe, a duplicate created
    # right after upload can be stuck with NULL duration indefinitely,
    # which would silently disqualify it from the "What's up next"
    # slide eligibility check.
    if root.duration_seconds is None and root.file_path:
        probed = _ffprobe_duration(root.file_path)
        if probed is not None:
            root.duration_seconds = probed
            db.commit()
    alias = PlaybackItem(
        id=str(ULID()),
        meeting_id=m.id,
        position=existing_count,
        filename=root.filename,
        source_item_id=root.id,
        # File data lives on the source row. Keep these NOT NULL columns
        # filled with empties so the schema invariant holds.
        file_path="",
        file_size_bytes=0,
        mime_type=root.mime_type,
        # Mirror the source's duration so the alias shows the same
        # time in the playlist UI.
        duration_seconds=root.duration_seconds,
    )
    db.add(alias)
    db.commit()
    db.refresh(alias)
    return _to_out(alias)


class RenameBody(BaseModel):
    # 200 mirrors the DB column cap on PlaybackItem.filename.
    filename: str = Field(min_length=1, max_length=200)


@router.patch("/meetings/{meeting_id}/playback/items/{item_id}")
def rename_playback_item(
    meeting_id: str,
    item_id: str,
    body: RenameBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Rename a playlist item's display label. The on-disk file is
    untouched — only the DB row's `filename` column changes, which is
    what every UI surface (playlist panel, "Now playing", "What's up
    next" slide) reads from."""
    m = _require_moderator(meeting_id, user.sub, db)
    item = db.query(PlaybackItem).filter_by(id=item_id, meeting_id=m.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    new_name = body.filename.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="filename must not be blank")
    item.filename = new_name[:200]
    db.commit()
    db.refresh(item)
    return _to_out(item)


@router.delete("/meetings/{meeting_id}/playback/items/{item_id}")
def delete_playback_item(
    meeting_id: str,
    item_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    m = _require_moderator(meeting_id, user.sub, db)
    item = db.query(PlaybackItem).filter_by(id=item_id, meeting_id=m.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    # Refuse to delete the item that's currently playing — caller should
    # stop playback first (or wait for it to finish).
    if m.playback_current_item_id == item.id:
        raise HTTPException(status_code=409, detail="item is currently playing")
    # Refuse to delete a source row that other (alias) rows depend on —
    # forces the host to remove aliases first so we don't strand them
    # with a broken file pointer.
    if item.source_item_id is None:
        alias_count = (
            db.query(PlaybackItem)
            .filter_by(meeting_id=m.id, source_item_id=item.id)
            .count()
        )
        if alias_count > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"this item is referenced by {alias_count} playlist link"
                    f"{'s' if alias_count != 1 else ''}; remove the link"
                    f"{'s' if alias_count != 1 else ''} first"
                ),
            )

    # Only delete the file on disk for self-contained rows; alias rows
    # have no file of their own.
    if item.source_item_id is None and item.file_path:
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
    return {"ok": True}


class ReorderBody(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=_REORDER_MAX_ITEMS)


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


@router.get("/meetings/{meeting_id}/playback/items/{item_id}/download")
def download_playback_item(
    meeting_id: str,
    item_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> FileResponse:
    """Owner/co-host download of an uploaded playlist item. Alias rows
    resolve to the source row's file so a Link can be downloaded too —
    same bytes either way. Served with `Content-Disposition: attachment`
    so the browser triggers a Save dialog instead of trying to play
    inline."""
    m = _require_moderator(meeting_id, user.sub, db)
    item = db.query(PlaybackItem).filter_by(id=item_id, meeting_id=m.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    source = _resolve_source(item, db)
    if not source.file_path:
        raise HTTPException(status_code=410, detail="file missing on disk")
    path = Path(source.file_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="file missing on disk")
    return FileResponse(
        path=str(path),
        media_type=source.mime_type or "video/mp4",
        filename=item.filename,
        headers={"Content-Disposition": f'attachment; filename="{item.filename}"'},
    )


@router.post("/meetings/{meeting_id}/playback/items/{item_id}:play")
async def play_specific_item_endpoint(
    meeting_id: str,
    item_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Click-to-play: jump to this playlist item now, skipping the
    play-in-order behaviour. If nothing is playing, this starts
    playback at the chosen item. If playback is already running, this
    switches to the chosen item without ending the spotlight (other
    participants stay muted, the playback participant stays pinned —
    only the source video swaps). Alias rows resolve to source at
    playback time via the existing fetch endpoint."""
    from app.services.playback_mgr import play_specific_item
    m = _require_moderator(meeting_id, user.sub, db)
    item = db.query(PlaybackItem).filter_by(id=item_id, meeting_id=m.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    return await play_specific_item(m, item, user.sub, db)


@router.get("/meetings/{meeting_id}/playback")
def get_playback_state(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Current playback state for the side-panel — polled while the
    panel is open. Includes the item that's actively playing (if any),
    when it started (so the SPA can compute elapsed time for the
    progress bar), and the item's duration so the bar can be
    proportional. Open to owner/co-host only."""
    m = _require_moderator(meeting_id, user.sub, db)
    cur_item: PlaybackItem | None = None
    if m.playback_current_item_id:
        cur_item = db.query(PlaybackItem).filter_by(id=m.playback_current_item_id).first()
    # SQLite drops tzinfo on round-trip even though the column is declared
    # `DateTime(timezone=True)`. The stored wall-clock IS UTC (every write
    # site uses `datetime.now(timezone.utc)`); reattach UTC tzinfo so the
    # ISO string carries `+00:00` and the SPA's `new Date(...)` doesn't
    # silently interpret it as local time (which would shift elapsed
    # by the browser's UTC offset — e.g. +2h in Brussels DST).
    started_at_iso: str | None = None
    if m.playback_started_at is not None:
        sa = m.playback_started_at
        if sa.tzinfo is None:
            sa = sa.replace(tzinfo=timezone.utc)
        started_at_iso = sa.isoformat()
    return {
        "enabled": bool(m.playback_enabled),
        "loop": bool(m.playback_loop),
        "active": bool(m.playback_ingress_id),
        "current_item_id": m.playback_current_item_id,
        "current_item_filename": cur_item.filename if cur_item else None,
        "current_item_duration_seconds": cur_item.duration_seconds if cur_item else None,
        "started_at": started_at_iso,
        # True while a freeze-frame ingress is holding on a single
        # frame; the SPA toggles Pause/Resume button state and freezes
        # its progress bar at `paused_offset_seconds`.
        "paused": m.playback_paused_offset_seconds is not None,
        "paused_offset_seconds": (
            float(m.playback_paused_offset_seconds)
            if m.playback_paused_offset_seconds is not None
            else None
        ),
    }


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


@router.post("/meetings/{meeting_id}/playback:pause")
async def playback_pause(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Pause without ending: swap the running ingress for a frozen-frame
    one at the current offset so every viewer sees the same still image.
    Resume picks up at the same offset."""
    from app.services.playback_mgr import pause_playback
    m = _require_moderator(meeting_id, user.sub, db)
    return await pause_playback(m, user.sub, db)


@router.post("/meetings/{meeting_id}/playback:resume")
async def playback_resume(
    meeting_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Inverse of `:pause` — tear down the freeze ingress, restart the
    real ingress at the saved offset."""
    from app.services.playback_mgr import resume_playback
    m = _require_moderator(meeting_id, user.sub, db)
    return await resume_playback(m, user.sub, db)


class SeekBody(BaseModel):
    position_seconds: float = Field(ge=0.0)


@router.post("/meetings/{meeting_id}/playback:seek")
async def playback_seek(
    meeting_id: str,
    body: SeekBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Click-on-progress-bar seek. Restarts the currently-playing
    ingress at the requested position; participants see a brief
    cut as the new ingress connects. 409 if nothing is playing."""
    from app.services.playback_mgr import seek_playback
    m = _require_moderator(meeting_id, user.sub, db)
    return await seek_playback(m, body.position_seconds, user.sub, db)


# ─── Internal file-fetch (called by livekit-ingress container) ────────


def _parse_range_header(value: str, file_size: int) -> tuple[int, int] | None:
    """Parse a single-range `Range: bytes=START-END` header. Returns
    (start, end_inclusive) or None if the header is malformed/unsupported.
    Multi-range and suffix-range (`bytes=-N`) are explicitly handled."""
    if not value or not value.lower().startswith("bytes="):
        return None
    spec = value[len("bytes="):].strip()
    if "," in spec:
        return None  # multi-range not supported
    if spec.startswith("-"):
        try:
            suffix_len = int(spec[1:])
        except ValueError:
            return None
        if suffix_len <= 0:
            return None
        start = max(0, file_size - suffix_len)
        return start, file_size - 1
    try:
        start_s, _, end_s = spec.partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= file_size:
        return None
    end = min(end, file_size - 1)
    return start, end


_RANGE_CHUNK = 1024 * 1024  # 1 MiB reads


def _iter_file_range(path: Path, start: int, end: int):
    """Yield bytes [start, end_inclusive] from `path` in 1 MiB chunks. Used
    as the StreamingResponse body so we don't load 1 GB into RAM."""
    remaining = end - start + 1
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(_RANGE_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/internal/playback/{item_id}")
def fetch_playback_file(
    item_id: str,
    token: Annotated[str, Query()],
    request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    t: Annotated[float | None, Query()] = None,
    freeze: Annotated[int, Query()] = 0,
    db: Session = Depends(get_db),
) -> Response:
    """Open endpoint — gated on the HMAC token issued by playback_mgr at
    ingress-create time. Supports HTTP Range requests so GStreamer's
    `souphttpsrc` (used by the LiveKit ingress URL_INPUT pipeline) can
    seek into the MP4 to read the `moov` atom; without Range support
    GStreamer aborts with "Server does not support seeking." even though
    the file is fully readable linearly.

    The optional `?t=<seconds>` parameter triggers a server-side seek
    path: ffmpeg stream-copies the file from time T as MPEG-TS, and we
    pipe its stdout straight to the response. Used by the playlist
    panel's slider for drag-to-seek and by the seek endpoint when the
    host clicks on a position. Range support is skipped on this path
    (the pipe is a one-shot stream, not a seekable file)."""
    if not verify_playback_url(item_id, token):
        raise HTTPException(status_code=403, detail="invalid or expired token")
    item = db.query(PlaybackItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    # Alias rows have no file of their own; serve the source's file.
    source = _resolve_source(item, db)
    if not source.file_path:
        raise HTTPException(status_code=410, detail="file missing on disk")
    path = Path(source.file_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="file missing on disk")

    # Pause path — endless single-frame loop at offset T. Takes precedence
    # over the regular seek path so the pause endpoint can hand out a URL
    # like `…?token=…&t=12.345&freeze=1` and the ingress will stream the
    # frozen frame instead of resuming playback at that offset.
    if freeze and t is not None and t >= 0:
        return StreamingResponse(
            _ffmpeg_freeze_stream(str(path), t),
            media_type="video/mp2t",
            headers={
                "Content-Disposition": f'inline; filename="{item.filename}"',
                "Cache-Control": "private, no-store",
            },
        )

    # Seek path — bypass Range/FileResponse and stream from ffmpeg.
    if t is not None and t > 0:
        return StreamingResponse(
            _ffmpeg_seek_stream(str(path), t),
            media_type="video/mp2t",
            headers={
                "Content-Disposition": f'inline; filename="{item.filename}"',
                "Cache-Control": "private, no-store",
            },
        )

    file_size = path.stat().st_size
    media_type = source.mime_type or "video/mp4"
    common_headers = {
        "Accept-Ranges": "bytes",
        # The alias's own filename is what we want shown to clients —
        # it usually mirrors the source's name but the host can rename
        # it independently in a future iteration.
        "Content-Disposition": f'inline; filename="{item.filename}"',
        "Cache-Control": "private, no-store",
    }

    if range_header is None:
        return FileResponse(
            path=str(path),
            media_type=media_type,
            filename=item.filename,
            headers=common_headers,
        )

    parsed = _parse_range_header(range_header, file_size)
    if parsed is None:
        # Malformed / unsatisfiable range → 416 with the actual file size
        # so the client can retry without Range or with a valid range.
        return Response(
            status_code=416,
            headers={**common_headers, "Content-Range": f"bytes */{file_size}"},
        )
    start, end = parsed
    length = end - start + 1
    return StreamingResponse(
        _iter_file_range(path, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        },
    )


@router.get("/internal/whats-next-slide/{slide_key}")
def fetch_whats_next_slide(
    slide_key: str,
    token: Annotated[str, Query()],
) -> Response:
    """Internal endpoint the LiveKit ingress fetches the "What's up next"
    slide from. Gated on the HMAC token issued at ingress-create time."""
    from app.services.whats_next_slide import verify_slide_url, slide_path_for_key

    if not verify_slide_url(slide_key, token):
        raise HTTPException(status_code=403, detail="invalid or expired token")
    path = slide_path_for_key(slide_key)
    if not path:
        raise HTTPException(status_code=404, detail="slide not found")
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="whats-next-{slide_key}.mp4"',
            "Cache-Control": "private, no-store",
        },
    )


# Re-exported so other modules (Room.tsx via meeting-out, etc.) can refer
# to the reserved playback identity without importing the service module.
__all__ = ["router", "PLAYBACK_IDENTITY"]

"""
Persistent meeting chat — modeled on one.witysk.org's DM (Messages) feature.

Supports markdown content, replies, image attachments, and per-message
reactions. Real-time fan-out is the writer's responsibility: after
mutating the API state, the writer sends a small "refetch" signal over
LiveKit's data channel so other clients re-pull state. The DB is the
single source of truth.

Auth model:
- In-meeting writes/reads use room-name as the gating key. We require the
  meeting to exist (and be active for writes). No JWT — anonymous
  participants need to chat.
- The post-meeting transcript view (`GET /v1/meetings/{id}/chat`) is
  owner-only and goes through the JWT path.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.auth import RequireUser
from app.config import settings
from app.db import get_db
from app.models import ChatMessage, ChatReaction, Meeting, WhiteboardShape, WhiteboardStroke
from app.routes.meetings import is_moderator

router = APIRouter(prefix="/v1")

_HISTORY_LIMIT = 1000

# Match onevoice's allowed image types for DM attachments.
_ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# The 8 reaction emojis the SPA can submit. Reject anything else so we
# don't get arbitrary unicode in the DB.
ALLOWED_REACTIONS = {"😊", "👍", "😂", "😢", "😠", "🤓", "❤️", "👎"}

# Lightweight identity sanity — keep this lenient; LiveKit identities are
# arbitrary strings ("user-123", anonymous "u-7af2…", etc.).
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,200}$")


def _meeting_for_room(room_name: str, db: Session, *, must_be_active: bool) -> Meeting:
    m = db.query(Meeting).filter_by(room_name=room_name).first()
    if not m:
        raise HTTPException(status_code=404, detail="room not found")
    if must_be_active and not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")
    return m


def _validate_identity(identity: str) -> None:
    if not _IDENTITY_RE.match(identity):
        raise HTTPException(status_code=400, detail="invalid identity")


def _serialize(
    msg: ChatMessage, reactions_by_msg: dict[int, list[ChatReaction]], room_name: str
) -> dict:
    return {
        "id": msg.id,
        "sender_identity": msg.sender_identity,
        "sender_name": msg.sender_name,
        "message": msg.message,
        "reply_to_id": msg.reply_to_id,
        "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
        "pinned_at": msg.pinned_at.isoformat() if msg.pinned_at else None,
        "pinned_by": msg.pinned_by,
        "attachment": (
            {
                # Scoped by room_name so integer message_ids aren't blindly
                # enumerable across the whole server. The endpoint verifies
                # the message actually belongs to the room.
                "url": f"/api/v1/rooms/{room_name}/chat/{msg.id}/attachment",
                "type": msg.attachment_type,
                "name": msg.attachment_name,
                "size": msg.attachment_size,
            }
            if msg.attachment_path
            else None
        ),
        "reactions": [
            {
                "emoji": r.emoji,
                "reactor_identity": r.reactor_identity,
                "reactor_name": r.reactor_name,
            }
            for r in reactions_by_msg.get(msg.id, [])
        ],
    }


def _history_for_meeting(meeting_id: str, room_name: str, db: Session) -> list[dict]:
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.meeting_id == meeting_id)
        .order_by(ChatMessage.sent_at.desc())
        .limit(_HISTORY_LIMIT)
        .all()
    )
    rows.reverse()
    if not rows:
        return []
    ids = [r.id for r in rows]
    reactions = db.query(ChatReaction).filter(ChatReaction.message_id.in_(ids)).all()
    by_msg: dict[int, list[ChatReaction]] = {}
    for r in reactions:
        by_msg.setdefault(r.message_id, []).append(r)
    return [_serialize(m, by_msg, room_name) for m in rows]


# ─── Read history ──────────────────────────────────────────────────────


@router.get("/rooms/{room_name}/chat")
def list_chat(room_name: str, db: Session = Depends(get_db)) -> list[dict]:
    m = _meeting_for_room(room_name, db, must_be_active=False)
    return _history_for_meeting(m.id, m.room_name, db)


@router.get("/meetings/{meeting_id}/chat")
def list_chat_for_meeting(
    meeting_id: str, user: RequireUser, db: Session = Depends(get_db)
) -> list[dict]:
    """Owner-only transcript access — used to view a closed meeting's chat
    history from MyMeetings without re-joining the room."""
    m = db.query(Meeting).filter_by(id=meeting_id, owner_user_id=user.sub).first()
    if not m:
        raise HTTPException(status_code=404, detail="meeting not found")
    return _history_for_meeting(m.id, m.room_name, db)


# ─── Post messages (text, with optional reply) ─────────────────────────


class ChatMessageIn(BaseModel):
    sender_identity: str = Field(min_length=1, max_length=200)
    sender_name: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8000)
    reply_to_id: int | None = None


@router.post("/rooms/{room_name}/chat", status_code=201)
def post_chat(
    room_name: str,
    body: ChatMessageIn,
    db: Session = Depends(get_db),
) -> dict:
    m = _meeting_for_room(room_name, db, must_be_active=True)
    _validate_identity(body.sender_identity)
    # Moderation: when participant chat is disabled, only the owner (identity
    # prefix `user-`) may post. Anonymous joiners (identity prefix `anon-`)
    # are rejected at the API layer.
    if not bool(m.allow_participant_chat) and body.sender_identity.startswith("anon-"):
        raise HTTPException(status_code=403, detail="participant chat is disabled for this meeting")
    if body.reply_to_id is not None:
        parent = db.query(ChatMessage).filter_by(id=body.reply_to_id, meeting_id=m.id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="reply target not found")
    row = ChatMessage(
        meeting_id=m.id,
        sender_identity=body.sender_identity,
        sender_name=body.sender_name,
        message=body.message,
        reply_to_id=body.reply_to_id,
        sent_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row, {}, m.room_name)


# ─── Image attachment upload (multipart) ───────────────────────────────


@router.post("/rooms/{room_name}/chat/attachment", status_code=201)
async def post_chat_attachment(
    room_name: str,
    sender_identity: str = Form(...),
    sender_name: str = Form(...),
    message: str = Form(""),
    reply_to_id: int | None = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    """Attach an image (only) to a chat message. Caption text is optional."""
    m = _meeting_for_room(room_name, db, must_be_active=True)
    _validate_identity(sender_identity)
    if not bool(m.allow_participant_chat) and sender_identity.startswith("anon-"):
        raise HTTPException(status_code=403, detail="participant chat is disabled for this meeting")
    if not sender_name or not sender_name.strip():
        raise HTTPException(status_code=400, detail="sender_name required")
    if reply_to_id is not None:
        parent = db.query(ChatMessage).filter_by(id=reply_to_id, meeting_id=m.id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="reply target not found")

    ct = (file.content_type or "").lower()
    ext = _ALLOWED_IMAGE_TYPES.get(ct)
    if not ext:
        raise HTTPException(
            status_code=415, detail=f"unsupported content type {ct!r}; use jpeg/png/webp/gif"
        )

    Path(settings.chat_attachments_dir).mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    target = Path(settings.chat_attachments_dir) / name
    cap = settings.chat_attachment_max_bytes
    total = 0
    with target.open("wb") as out:
        while chunk := await file.read(64 * 1024):
            total += len(chunk)
            if total > cap:
                out.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"image exceeds {cap} bytes")
            out.write(chunk)

    row = ChatMessage(
        meeting_id=m.id,
        sender_identity=sender_identity,
        sender_name=sender_name,
        message=(message or "").strip(),
        reply_to_id=reply_to_id,
        attachment_path=str(target),
        attachment_type=ct,
        attachment_name=file.filename or name,
        attachment_size=total,
        sent_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row, {}, m.room_name)


@router.get("/rooms/{room_name}/chat/{message_id}/attachment")
def get_chat_attachment(
    room_name: str, message_id: int, db: Session = Depends(get_db)
) -> FileResponse:
    """Read access to chat attachments, scoped by the parent room. The
    room-name gate matches the trust level for chat history (anyone with the
    3-word slug can read it). Without the scope, sequential message_ids
    would be enumerable across every meeting on the server."""
    m = _meeting_for_room(room_name, db, must_be_active=False)
    msg = (
        db.query(ChatMessage)
        .filter_by(id=message_id, meeting_id=m.id)
        .first()
    )
    if not msg or not msg.attachment_path:
        raise HTTPException(status_code=404, detail="attachment not found")
    p = Path(msg.attachment_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="attachment file missing")
    return FileResponse(
        path=str(p),
        media_type=msg.attachment_type or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=600"},
    )


# ─── Reactions ─────────────────────────────────────────────────────────


class ReactionIn(BaseModel):
    reactor_identity: str = Field(min_length=1, max_length=200)
    reactor_name: str = Field(min_length=1, max_length=200)
    emoji: str = Field(min_length=1, max_length=8)


@router.put("/rooms/{room_name}/chat/{message_id}/reaction")
def put_reaction(
    room_name: str,
    message_id: int,
    body: ReactionIn,
    db: Session = Depends(get_db),
) -> dict:
    """Add or replace a reactor's reaction on a message. One reaction per
    user per message — submitting a different emoji replaces the previous
    one (toggle/swap UX)."""
    m = _meeting_for_room(room_name, db, must_be_active=True)
    _validate_identity(body.reactor_identity)
    if body.emoji not in ALLOWED_REACTIONS:
        raise HTTPException(status_code=400, detail="emoji not allowed")
    msg = db.query(ChatMessage).filter_by(id=message_id, meeting_id=m.id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="message not found")
    existing = (
        db.query(ChatReaction)
        .filter_by(message_id=msg.id, reactor_identity=body.reactor_identity)
        .first()
    )
    if existing:
        existing.emoji = body.emoji
        existing.reactor_name = body.reactor_name
    else:
        db.add(
            ChatReaction(
                message_id=msg.id,
                reactor_identity=body.reactor_identity,
                reactor_name=body.reactor_name,
                emoji=body.emoji,
            )
        )
    db.commit()
    return {"ok": True}


@router.delete("/rooms/{room_name}/chat/{message_id}/reaction")
def delete_reaction(
    room_name: str,
    message_id: int,
    reactor_identity: str,
    db: Session = Depends(get_db),
) -> dict:
    m = _meeting_for_room(room_name, db, must_be_active=True)
    _validate_identity(reactor_identity)
    msg = db.query(ChatMessage).filter_by(id=message_id, meeting_id=m.id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="message not found")
    db.query(ChatReaction).filter_by(
        message_id=msg.id, reactor_identity=reactor_identity
    ).delete()
    db.commit()
    return {"ok": True}


# ─── Shared whiteboard (room-scoped) ──────────────────────────────────

_WHITEBOARD_MAX_STROKES = 5000  # safety cap per meeting


@router.get("/rooms/{room_name}/whiteboard/strokes")
def get_whiteboard_strokes(room_name: str, db: Session = Depends(get_db)) -> list[dict]:
    """Return every stroke / clear packet for this room in insert order.
    Used by clients opening the whiteboard tab to replay state from scratch."""
    m = _meeting_for_room(room_name, db, must_be_active=False)
    rows = (
        db.query(WhiteboardStroke)
        .filter(WhiteboardStroke.meeting_id == m.id)
        .order_by(WhiteboardStroke.id.asc())
        .all()
    )
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r.payload_json))
        except ValueError:
            continue
    return out


class StrokeBody(BaseModel):
    packet: dict


@router.post("/rooms/{room_name}/whiteboard/strokes", status_code=201)
def post_whiteboard_stroke(
    room_name: str,
    body: StrokeBody,
    db: Session = Depends(get_db),
) -> dict:
    m = _meeting_for_room(room_name, db, must_be_active=True)
    # Soft cap so a runaway script can't fill the table for one room.
    n = db.query(WhiteboardStroke).filter_by(meeting_id=m.id).count()
    if n >= _WHITEBOARD_MAX_STROKES:
        raise HTTPException(status_code=413, detail="whiteboard at capacity")
    db.add(
        WhiteboardStroke(
            meeting_id=m.id,
            payload_json=json.dumps(body.packet, separators=(",", ":")),
        )
    )
    db.commit()
    return {"ok": True}


@router.delete("/rooms/{room_name}/whiteboard/strokes")
def clear_whiteboard_strokes(room_name: str, db: Session = Depends(get_db)) -> dict:
    """Drop every stroke AND shape for this room. Called when any
    participant hits the Clear board button — the client also broadcasts
    a clear packet so live peers wipe their canvases instantly."""
    m = _meeting_for_room(room_name, db, must_be_active=True)
    db.query(WhiteboardStroke).filter_by(meeting_id=m.id).delete()
    db.query(WhiteboardShape).filter_by(meeting_id=m.id).delete()
    db.commit()
    return {"ok": True}


# ─── Shapes (rect / ellipse / text) — addressable by id so they can be
#     moved, resized and edited after creation. ────────────────────────


class ShapeIn(BaseModel):
    kind: str = Field(pattern="^(rect|ellipse|text)$")
    x: float
    y: float
    w: float
    h: float
    color: str = Field(default="#fbbf24", max_length=32)
    stroke_width: int = Field(default=3, ge=1, le=20)
    text: str | None = Field(default=None, max_length=2000)
    font_size: int | None = Field(default=None, ge=8, le=200)


@router.get("/rooms/{room_name}/whiteboard/shapes")
def list_whiteboard_shapes(room_name: str, db: Session = Depends(get_db)) -> list[dict]:
    m = _meeting_for_room(room_name, db, must_be_active=False)
    rows = (
        db.query(WhiteboardShape)
        .filter(WhiteboardShape.meeting_id == m.id)
        .order_by(WhiteboardShape.updated_at.asc())
        .all()
    )
    return [
        {
            "id": r.id,
            "kind": r.kind,
            "x": r.x, "y": r.y, "w": r.w, "h": r.h,
            "color": r.color,
            "stroke_width": r.stroke_width,
            "text": r.text,
            "font_size": r.font_size,
        }
        for r in rows
    ]


@router.put("/rooms/{room_name}/whiteboard/shapes/{shape_id}")
def upsert_whiteboard_shape(
    room_name: str,
    shape_id: str,
    body: ShapeIn,
    db: Session = Depends(get_db),
) -> dict:
    """Idempotent create-or-update by id. The client owns the id (ULID)."""
    m = _meeting_for_room(room_name, db, must_be_active=True)
    if not _IDENTITY_RE.match(shape_id):
        raise HTTPException(status_code=400, detail="invalid shape id")
    # Atomic upsert: clients can fire two writes for the same shape_id
    # almost simultaneously (e.g. text-tool click creates the shape, then a
    # fast text-commit fires before the first PUT has returned). A
    # check-then-act SELECT/INSERT loses that race with a PRIMARY KEY
    # conflict; INSERT ... ON CONFLICT DO UPDATE doesn't.
    payload = dict(
        id=shape_id, meeting_id=m.id, kind=body.kind,
        x=body.x, y=body.y, w=body.w, h=body.h,
        color=body.color, stroke_width=body.stroke_width,
        text=body.text, font_size=body.font_size,
        updated_at=datetime.now(timezone.utc),
    )
    stmt = sqlite_insert(WhiteboardShape).values(**payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[WhiteboardShape.id],
        set_={k: stmt.excluded[k] for k in payload if k != "id"},
    )
    db.execute(stmt)
    db.commit()
    return {"ok": True}


@router.delete("/rooms/{room_name}/whiteboard/shapes/{shape_id}")
def delete_whiteboard_shape(
    room_name: str,
    shape_id: str,
    db: Session = Depends(get_db),
) -> dict:
    m = _meeting_for_room(room_name, db, must_be_active=True)
    db.query(WhiteboardShape).filter_by(id=shape_id, meeting_id=m.id).delete()
    db.commit()
    return {"ok": True}


# ─── Shared notes (room-scoped, anyone in the room can read/write) ────


@router.get("/rooms/{room_name}/notes")
def get_notes(room_name: str, db: Session = Depends(get_db)) -> dict:
    m = _meeting_for_room(room_name, db, must_be_active=False)
    return {"notes": m.notes or "", "meeting_id": m.id}


class NotesBody(BaseModel):
    notes: str = Field(default="", max_length=100_000)


@router.put("/rooms/{room_name}/notes")
def put_notes(room_name: str, body: NotesBody, db: Session = Depends(get_db)) -> dict:
    m = _meeting_for_room(room_name, db, must_be_active=True)
    m.notes = body.notes
    db.commit()
    return {"ok": True, "length": len(m.notes)}


# ─── Pinned messages (owner-only) ──────────────────────────────────────

class PinBody(BaseModel):
    message_id: int
    pinned: bool


@router.post("/meetings/{meeting_id}/chat/pin")
def set_pin(
    meeting_id: str,
    body: PinBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    """Owner-only pin/unpin. The SPA polls / refetches chat history after the
    write, so no separate data-channel hint is required — the existing
    refetch signal that fires on every post is enough."""
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not is_moderator(m, user.sub):
        raise HTTPException(status_code=404, detail="meeting not found")
    msg = db.query(ChatMessage).filter_by(id=body.message_id, meeting_id=m.id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="message not found")
    if body.pinned:
        msg.pinned_at = datetime.now(timezone.utc)
        msg.pinned_by = user.sub
    else:
        msg.pinned_at = None
        msg.pinned_by = None
    db.commit()
    return {"ok": True, "pinned_at": msg.pinned_at.isoformat() if msg.pinned_at else None}

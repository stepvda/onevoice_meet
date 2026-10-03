"""Meet++ REST and internal endpoints."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from ulid import ULID

from livekit import api as lk_api

from app.auth import AuthUser, RequirePlatformAdmin, RequireUser
from app.config import settings
from app.db import SessionLocal, get_db
from app.livekit_client import livekit_api
from app.meetpp import ingest as ingest_mod
from app.meetpp import invites as invites_mod
from app.meetpp import llm, ops, phases, render, runtime as runtime_mod
from app.meetpp.auth import (
    ChairContext,
    RoomPrincipal,
    is_moderator,
    principal_can_edit,
    require_chair_session,
    require_internal,
    require_meeting_chair,
    require_room_session,
    session_editors,
)
from app.meetpp.locales import t
from app.meetpp.models import (
    MeetppAction,
    MeetppLlmCall,
    MeetppAgendaItem,
    MeetppAttachment,
    MeetppAttendee,
    MeetppDecision,
    MeetppDocument,
    MeetppMinute,
    MeetppOp,
    MeetppOutput,
    MeetppSegment,
    MeetppSession,
    MeetppSeries,
    utcnow,
)
from app.models import Meeting

log = logging.getLogger("app.meetpp")

router = APIRouter(prefix="/v1")

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


# ─── helpers ────────────────────────────────────────────────────────────────


def _ulid() -> str:
    return str(ULID())


def _pilot_allows(meeting: Meeting, user: AuthUser) -> bool:
    if not settings.meetpp_enabled:
        return False
    if settings.meetpp_pilot_owner_subs and meeting.owner_user_id not in settings.meetpp_pilot_owner_subs:
        return False
    return True


def _require_pilot(meeting: Meeting, user: AuthUser) -> None:
    if not settings.meetpp_enabled:
        raise HTTPException(status_code=404, detail="Meet++ is not enabled")
    if settings.meetpp_pilot_owner_subs and meeting.owner_user_id not in settings.meetpp_pilot_owner_subs:
        raise HTTPException(status_code=404, detail="Meet++ is not enabled for this owner")


def _active_session_count(db: Session, exclude_id: str | None = None) -> int:
    """CPU-consuming sessions for the capacity cap. `setup` does not count
    until started; the session being started is excluded explicitly so the
    check is `>=` against the other live sessions."""
    q = db.query(MeetppSession).filter(
        MeetppSession.status.in_(("running", "paused", "finalising"))
    )
    if exclude_id:
        q = q.filter(MeetppSession.id != exclude_id)
    return q.count()


def _series_for(meeting: Meeting, user: AuthUser, series_id: str | None, db: Session) -> MeetppSeries:
    if series_id:
        series = db.get(MeetppSeries, series_id)
        if series is None or series.owner_sub not in (user.sub, meeting.owner_user_id):
            raise HTTPException(status_code=404, detail="series not found")
        return series
    if meeting.meetpp_series_id:
        series = db.get(MeetppSeries, meeting.meetpp_series_id)
        if series is not None:
            return series
    series = MeetppSeries(id=_ulid(), owner_sub=meeting.owner_user_id, title=meeting.display_title, meeting_id=meeting.id)
    db.add(series)
    db.flush()
    meeting.meetpp_series_id = series.id
    return series


def _create_series_meeting(meeting: Meeting, user: AuthUser, db: Session) -> MeetppSeries:
    return _series_for(meeting, user, None, db)


# ─── setup / lifecycle ──────────────────────────────────────────────────────


class CreateSessionBody(BaseModel):
    template: str = "agenda"
    mode: str = "lead"
    language: str = "en"
    series_id: str | None = None
    goal: str | None = None


@router.post("/meetings/{meeting_id}/meetpp/sessions")
async def create_session(
    meeting_id: str,
    body: CreateSessionBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    meeting = require_meeting_chair(meeting_id, user, db)
    _require_pilot(meeting, user)
    # Only one active session per room.
    existing = (
        db.query(MeetppSession)
        .filter(
            MeetppSession.meeting_id == meeting.id,
            MeetppSession.status.in_(("setup", "running", "paused", "finalising")),
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="A Meet++ session is already active for this room")
    if _active_session_count(db) >= settings.meetpp_max_active_sessions:
        raise HTTPException(status_code=503, detail="AI capacity busy — try again later")

    language = body.language if body.language in settings.meetpp_languages else "en"
    mode = body.mode if body.mode in ("lead", "assist") else settings.meetpp_default_mode
    series = _series_for(meeting, user, body.series_id, db)
    session = MeetppSession(
        id=_ulid(),
        meeting_id=meeting.id,
        series_id=series.id,
        created_by_user_id=user.sub,
        status="setup",
        template=body.template if body.template in ("agenda", "goal") else "agenda",
        mode=mode,
        language=language,
        goal=body.goal,
        settings_json=json.dumps({"show_public": False, "in_recordings": True, "timebox_nudges": True, "speak": True}),
    )
    db.add(session)
    db.commit()
    log.info(
        "MEETPP_SESSION sid=%s meeting_id=%s event=create template=%s mode=%s language=%s",
        session.id, meeting.id, session.template, session.mode, session.language,
    )
    # Imported previous open actions.
    prev = (
        db.query(MeetppAction)
        .filter(
            MeetppAction.series_id == series.id,
            MeetppAction.session_id != session.id,
            MeetppAction.status.notin_(("done", "dropped")),
        )
        .all()
    )
    return {
        "session": ops.session_meta(session),
        "imported_actions": [ops.action_dict(a) for a in prev],
        "provider_label": settings.llm_provider_label,
    }


@router.get("/meetings/{meeting_id}/meetpp/sessions")
async def list_sessions(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    meeting = require_meeting_chair(meeting_id, user, db)
    rows = (
        db.query(MeetppSession)
        .filter_by(meeting_id=meeting.id)
        .order_by(MeetppSession.created_at.desc())
        .all()
    )
    return {"sessions": [{**ops.session_meta(s), "publish_version": s.publish_version} for s in rows]}


class PatchSessionBody(BaseModel):
    mode: str | None = None
    language: str | None = None
    editors: list[str] | None = None
    settings: dict | None = None


@router.patch("/meetpp/sessions/{session_id}")
async def patch_session(
    session_id: str,
    body: PatchSessionBody,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    s = ctx.session
    if body.mode in ("lead", "assist"):
        s.mode = body.mode
    if body.language in settings.meetpp_languages:
        s.language = body.language
    if body.editors is not None:
        s.editors_json = json.dumps(body.editors)
    if body.settings is not None:
        try:
            current = json.loads(s.settings_json or "{}")
        except ValueError:
            current = {}
        current.update(body.settings)
        s.settings_json = json.dumps(current)
    db.commit()
    return ops.session_meta(s)


@router.post("/meetpp/sessions/{session_id}/start")
async def start_session(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    s = ctx.session
    if s.status not in ("setup", "paused"):
        raise HTTPException(status_code=409, detail=f"cannot start from status {s.status}")
    # Count every other live session (including ones still in setup) against
    # the cap; this session is already counted by `sqlite` as setup, so exclude
    # it explicitly.
    if _active_session_count(db, exclude_id=s.id) >= settings.meetpp_max_active_sessions:
        raise HTTPException(status_code=503, detail="AI capacity busy — try again later")
    s.status = "running"
    s.started_at = s.started_at or utcnow()
    db.commit()
    log.info("MEETPP_SESSION sid=%s event=start mode=%s", s.id, s.mode)
    runtime_mod.runtime.start_session(s.id)
    return {"ok": True, "session": ops.session_meta(s)}


@router.post("/meetpp/sessions/{session_id}/pause")
async def pause_session(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    await runtime_mod.runtime.get(session_id).pause_session(session_id, True)
    return {"ok": True}


@router.post("/meetpp/sessions/{session_id}/resume")
async def resume_session(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    await runtime_mod.runtime.get(session_id).pause_session(session_id, False)
    return {"ok": True}


@router.post("/meetpp/sessions/{session_id}/end")
async def end_session(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    s = ctx.session
    if s.status not in ("running", "paused", "setup"):
        raise HTTPException(status_code=409, detail=f"cannot end from status {s.status}")
    final = await runtime_mod.runtime.end_session(session_id)
    db.expire_all()
    s = db.get(MeetppSession, session_id)
    meeting = db.get(Meeting, s.meeting_id)
    # Render the download outputs (minutes/agenda/.ics). For a recurring
    # series, also send the next-meeting invitations (and minutes) by e-mail
    # automatically once the outputs exist.
    outputs_info: dict = {}
    try:
        if meeting is not None and meeting.recurrence_rule:
            result = await invites_mod.publish(db, s)
            outputs_info = {"auto_sent": True, "publish": result}
        else:
            outputs_info = await invites_mod.prepare_outputs(db, s)
            outputs_info["auto_sent"] = False
    except Exception as exc:  # noqa: BLE001
        log.exception("meetpp: preparing outputs after end failed")
        outputs_info = {"error": str(exc)[:300]}
    listing = invites_mod.list_outputs(db, s)
    log.info("MEETPP_SESSION sid=%s event=end status=%s auto_sent=%s", s.id, s.status, outputs_info.get("auto_sent"))
    return {"ok": True, "session": ops.session_meta(s), "final": final, "outputs_info": outputs_info, "outputs": listing}


@router.get("/meetpp/sessions/{session_id}/outputs")
async def get_outputs(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    return invites_mod.list_outputs(db, ctx.session)


class PhaseBody(BaseModel):
    to: str
    item_id: str | None = None
    proposal_id: str | None = None
    accept: bool = True


@router.post("/meetpp/sessions/{session_id}/phase")
async def phase_action(
    session_id: str,
    body: PhaseBody,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    from app.meetpp import phases as ph

    runner = runtime_mod.runtime.get(session_id)
    if not body.accept:
        await runner.reject_proposal(db, ctx.session)
        return {"ok": True, "rejected": True}
    current = ph.current_proposal(ctx.session)
    target = body.to or (current or {}).get("to")
    if not target:
        raise HTTPException(status_code=400, detail="no phase target")
    item_id = body.item_id or (current or {}).get("item_id")
    ann = await runner.jump_phase(db, ctx.session, target, item_id, by=f"user:{ctx.user.sub}")
    return {"ok": True, "announcement": ann, "session": ops.session_meta(ctx.session)}


@router.post("/meetpp/sessions/{session_id}/board-to-main")
async def board_to_main(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    """Bring the board back to the main stage slot: clear any pinned person and
    mark the board main in the room metadata (takes effect on every client)."""
    lk = livekit_api()
    try:
        rooms = await lk.room.list_rooms(lk_api.ListRoomsRequest(names=[ctx.meeting.room_name]))
        current: dict = {}
        if rooms.rooms:
            try:
                current = json.loads(rooms.rooms[0].metadata or "{}")
            except ValueError:
                current = {}
        current["presenter_identity"] = None
        meetpp = current.get("meetpp") or {}
        meetpp.update({"sid": ctx.session.id, "active": True, "board_main": True})
        current["meetpp"] = meetpp
        await lk.room.update_room_metadata(
            lk_api.UpdateRoomMetadataRequest(room=ctx.meeting.room_name, metadata=json.dumps(current))
        )
    finally:
        await lk.aclose()
    return {"ok": True}


# ─── documents / agenda ─────────────────────────────────────────────────────


@router.post("/meetpp/sessions/{session_id}/documents")
async def upload_document(
    session_id: str,
    background: BackgroundTasks,
    kind: str = Form(...),
    file: UploadFile = File(...),
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> JSONResponse:
    if kind not in ("agenda", "previous_notes"):
        raise HTTPException(status_code=400, detail="kind must be agenda or previous_notes")
    data = await file.read()
    ok, err = ingest_mod.check_pdf_bytes(data)
    if not ok:
        raise HTTPException(status_code=400, detail=err or "invalid pdf")
    upload_dir = Path(settings.meetpp_data_dir) / session_id / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    doc_id = _ulid()
    path = upload_dir / f"{doc_id}.pdf"
    path.write_bytes(data)
    doc = MeetppDocument(
        id=doc_id,
        session_id=session_id,
        kind=kind,
        filename=file.filename or "document.pdf",
        path=str(path),
        status="uploaded",
    )
    db.add(doc)
    db.commit()
    background.add_task(_parse_document, doc_id)
    return JSONResponse(status_code=202, content={"document": _doc_dict(doc)})


def _parse_document(doc_id: str) -> None:
    import asyncio

    db = SessionLocal()
    try:
        doc = db.get(MeetppDocument, doc_id)
        if doc is None:
            return
        session = db.get(MeetppSession, doc.session_id)
        doc.status = "parsing"
        db.commit()
        result = ingest_mod.extract_pdf(Path(doc.path))
        valid, err = ingest_mod.validate_extracted(result, settings.meetpp_upload_max_pages)
        if not valid:
            doc.status = "failed"
            doc.error = err
            db.commit()
            return
        doc.page_count = int(result.get("page_count") or 0)
        doc.extracted_text = result.get("text") or ""
        # Surface the uploaded PDF in the Attachments tab so the chair can
        # always see/open what was provided.
        db.add(
            MeetppAttachment(
                id=_ulid(),
                session_id=doc.session_id,
                kind="upload",
                path=str(doc.path),
                filename=doc.filename,
                caption=("Agenda" if doc.kind == "agenda" else "Previous notes") + f": {doc.filename}",
                author=session.created_by_user_id,
            )
        )
        db.commit()
        structured = asyncio.run(ingest_mod.structure_document(db, doc, session))
        if structured:
            if doc.kind == "agenda":
                ingest_mod.import_agenda(db, session, structured)
            elif doc.kind == "previous_notes":
                ingest_mod.import_previous_notes(db, session, structured)
    except Exception as exc:  # noqa: BLE001
        log.exception("meetpp: document parse failed")
        try:
            doc = db.get(MeetppDocument, doc_id)
            if doc is not None:
                doc.status = "failed"
                doc.error = str(exc)[:500]
                db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


def _doc_dict(doc: MeetppDocument) -> dict:
    structured = None
    if doc.structured_json:
        try:
            structured = json.loads(doc.structured_json)
        except ValueError:
            structured = None
    return {
        "id": doc.id,
        "kind": doc.kind,
        "filename": doc.filename,
        "status": doc.status,
        "error": doc.error,
        "page_count": doc.page_count,
        "structured": structured,
    }


@router.get("/meetpp/sessions/{session_id}/documents/{doc_id}")
async def get_document(
    session_id: str,
    doc_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    doc = db.get(MeetppDocument, doc_id)
    if doc is None or doc.session_id != session_id:
        raise HTTPException(status_code=404, detail="document not found")
    return _doc_dict(doc)


class AgendaItemBody(BaseModel):
    id: str | None = None
    title: str
    presenter: str | None = None
    timebox_minutes: int | None = None
    desired_outcome: str | None = None
    status: str = "pending"


class AgendaBody(BaseModel):
    items: list[AgendaItemBody]


@router.put("/meetpp/sessions/{session_id}/agenda")
async def put_agenda(
    session_id: str,
    body: AgendaBody,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    s = ctx.session
    if s.status not in ("setup",):
        raise HTTPException(status_code=409, detail="agenda can only be replaced during setup")
    db.query(MeetppAgendaItem).filter_by(session_id=s.id).delete()
    for i, item in enumerate(body.items, start=1):
        db.add(
            MeetppAgendaItem(
                id=item.id or _ulid(),
                session_id=s.id,
                position=i,
                title=item.title[:400],
                presenter=item.presenter,
                timebox_minutes=item.timebox_minutes,
                desired_outcome=item.desired_outcome,
                status="pending",
                source="user",
            )
        )
    db.commit()
    ann = phases.announcement(db, s, "agenda")
    return {"ok": True, "announcement": ann}


# ─── board access ───────────────────────────────────────────────────────────


@router.get("/meetpp/rooms/{room}/active")
async def room_active(room: str, db: Session = Depends(get_db)) -> dict:
    """Unauthenticated lobby notice + provider/consent info for the room."""
    session = (
        db.query(MeetppSession)
        .join(Meeting, Meeting.id == MeetppSession.meeting_id)
        .filter(Meeting.room_name == room, MeetppSession.status.in_(("running", "paused")))
        .first()
    )
    if session is None:
        return {"active": False}
    return {
        "active": True,
        "sid": session.id,
        "provider_label": settings.llm_provider_label,
        "consent_text_version": "v1",
    }


@router.get("/meetpp/sessions/{session_id}/state")
async def get_state(
    session_id: str,
    since: int | None = None,
    resolved: tuple[MeetppSession, RoomPrincipal] = Depends(require_room_session),
    db: Session = Depends(get_db),
) -> dict:
    session, principal = resolved
    if since is not None and since >= session.state_version:
        # No new state.
        return {"v": 1, "type": "state", "sid": session.id, "version": session.state_version, "changes": [], "delta": {}}
    state = ops.build_state(db, session)
    state["v"] = 1
    state["type"] = "state"
    state["sid"] = session.id
    return state


@router.get("/meetpp/sessions/{session_id}/transcript")
async def get_transcript(
    session_id: str,
    after: int = 0,
    limit: int = 400,
    resolved: tuple[MeetppSession, RoomPrincipal] = Depends(require_room_session),
    db: Session = Depends(get_db),
) -> dict:
    session, _ = resolved
    rows = (
        db.query(MeetppSegment)
        .filter(MeetppSegment.session_id == session_id, MeetppSegment.seq > after)
        .order_by(MeetppSegment.seq)
        .limit(min(limit, 1000))
        .all()
    )
    return {
        "segments": [
            {
                "seq": r.seq,
                "identity": r.identity,
                "name": r.name,
                "text": r.text,
                "t_start": r.t_start.isoformat() if r.t_start else None,
                "t_end": r.t_end.isoformat() if r.t_end else None,
            }
            for r in rows
        ]
    }


class ConsentBody(BaseModel):
    decision: str


@router.post("/meetpp/sessions/{session_id}/consent")
async def set_consent(
    session_id: str,
    body: ConsentBody,
    resolved: tuple[MeetppSession, RoomPrincipal] = Depends(require_room_session),
    db: Session = Depends(get_db),
) -> dict:
    session, principal = resolved
    if body.decision not in ("accept", "opt_out"):
        raise HTTPException(status_code=400, detail="decision must be accept or opt_out")
    _ = session
    await runtime_mod.runtime.get(session_id).set_consent(session_id, principal.identity, body.decision)
    return {"ok": True, "decision": body.decision}


class OpsBody(BaseModel):
    ops: list[dict] = Field(default_factory=list)


@router.post("/meetpp/sessions/{session_id}/ops")
async def human_ops(
    session_id: str,
    body: OpsBody,
    resolved: tuple[MeetppSession, RoomPrincipal] = Depends(require_room_session),
    db: Session = Depends(get_db),
) -> dict:
    session, principal = resolved
    meeting = db.get(Meeting, session.meeting_id)
    if not principal_can_edit(session, meeting, principal):
        raise HTTPException(status_code=404, detail="session not found")
    actor = f"user:{principal.identity}"
    result = await runtime_mod.runtime.get(session_id).apply_human_ops(session_id, body.ops, actor)
    return result


@router.post("/meetpp/sessions/{session_id}/attachments")
async def upload_attachment(
    session_id: str,
    file: UploadFile = File(...),
    caption: str = Form(""),
    resolved: tuple[MeetppSession, RoomPrincipal] = Depends(require_room_session),
    db: Session = Depends(get_db),
) -> dict:
    session, principal = resolved
    meeting = db.get(Meeting, session.meeting_id)
    # Read-only principals (public viewers, egress/recorder tokens) must never
    # write, even when snapshots are open to participants.
    if principal.read_only:
        raise HTTPException(status_code=404, detail="session not found")
    if not principal_can_edit(session, meeting, principal):
        # Snapshot may be open to any (writable) participant when the setting
        # allows; it defaults to on.
        allow_any = True
        try:
            allow_any = bool((json.loads(session.settings_json or "{}") or {}).get("snapshot_any", True))
        except ValueError:
            allow_any = True
        if not allow_any:
            raise HTTPException(status_code=404, detail="session not found")
    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=400, detail="only PNG snapshots are accepted")
    att_dir = Path(settings.meetpp_data_dir) / session_id / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    att_id = _ulid()
    path = att_dir / f"{att_id}.png"
    path.write_bytes(data)
    item_id = session.current_item_id
    att = MeetppAttachment(
        id=att_id,
        session_id=session_id,
        agenda_item_id=item_id,
        kind="whiteboard",
        path=str(path),
        filename="whiteboard.png",
        caption=caption or "Whiteboard snapshot",
        author=principal.name,
    )
    db.add(att)
    # Synthetic transcript marker so minutes can reference it.
    max_seq = db.query(func.max(MeetppSegment.seq)).filter(MeetppSegment.session_id == session_id).scalar() or 0
    db.add(
        MeetppSegment(
            session_id=session_id,
            seq=max_seq + 1,
            identity="meetpp-board",
            name="Meet++",
            text=f"[whiteboard snapshot attached to item {item_id or '-'}]",
            is_gap=True,
        )
    )
    db.commit()
    await runtime_mod.runtime.get(session_id)._broadcast(
        "state",
        version=session.state_version,
        changes=[{"kind": "attachment", "id": att_id, "op": "add"}],
        delta={"attachment": [ops.attachment_dict(att)]},
    )
    return {"attachment": ops.attachment_dict(att)}


@router.get("/meetpp/sessions/{session_id}/attachments/{att_id}")
async def get_attachment(
    session_id: str,
    att_id: str,
    resolved: tuple[MeetppSession, RoomPrincipal] = Depends(require_room_session),
    db: Session = Depends(get_db),
) -> FileResponse:
    att = db.get(MeetppAttachment, att_id)
    if att is None or att.session_id != session_id:
        raise HTTPException(status_code=404, detail="attachment not found")
    suffix = (att.filename or "").lower().rsplit(".", 1)
    ext = suffix[1] if len(suffix) == 2 else ""
    media = {
        "png": "image/png",
        "pdf": "application/pdf",
        "txt": "text/plain",
    }.get(ext, "application/octet-stream")
    return FileResponse(att.path, media_type=media, filename=att.filename)


class AttachmentPatch(BaseModel):
    caption: str | None = None


@router.patch("/meetpp/sessions/{session_id}/attachments/{att_id}")
async def patch_attachment(
    session_id: str,
    att_id: str,
    body: AttachmentPatch,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    att = db.get(MeetppAttachment, att_id)
    if att is None or att.session_id != session_id:
        raise HTTPException(status_code=404, detail="attachment not found")
    if body.caption is not None:
        att.caption = body.caption[:400]
    db.commit()
    return {"attachment": ops.attachment_dict(att)}


@router.delete("/meetpp/sessions/{session_id}/attachments/{att_id}")
async def delete_attachment(
    session_id: str,
    att_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    att = db.get(MeetppAttachment, att_id)
    if att is None or att.session_id != session_id:
        raise HTTPException(status_code=404, detail="attachment not found")
    if att.kind == "whiteboard" and att.path:
        try:
            Path(att.path).unlink(missing_ok=True)
        except OSError:
            pass
    db.delete(att)
    ctx.session.state_version += 1
    db.commit()
    await runtime_mod.runtime.get(session_id)._broadcast(
        "state",
        version=ctx.session.state_version,
        changes=[{"kind": "attachment", "id": att_id, "op": "remove"}],
        delta={},
    )
    return {"ok": True}


class AttendeePatch(BaseModel):
    display_name: str | None = None
    email: str | None = None
    presence: str | None = None
    required_next: bool | None = None
    required_reason: str | None = None


@router.patch("/meetpp/sessions/{session_id}/attendees/{att_id}")
async def patch_attendee(
    session_id: str,
    att_id: str,
    body: AttendeePatch,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    a = db.get(MeetppAttendee, att_id)
    if a is None or a.session_id != session_id:
        raise HTTPException(status_code=404, detail="attendee not found")
    if body.display_name is not None and body.display_name.strip():
        a.display_name = body.display_name.strip()[:200]
    if body.email is not None:
        a.email = body.email.strip()[:300] or None
    if body.presence in ("present", "left", "absent", "apologies", "not_transcribed"):
        a.presence = body.presence
    if body.required_next is not None:
        a.required_next = body.required_next
    if body.required_reason is not None:
        a.required_reason = body.required_reason[:300] or None
    ctx.session.state_version += 1
    db.commit()
    await runtime_mod.runtime.get(session_id)._broadcast(
        "state",
        version=ctx.session.state_version,
        changes=[{"kind": "attendance", "id": a.id, "op": "update"}],
        delta={"attendance": [ops.attendee_dict(a)]},
    )
    return {"attendee": ops.attendee_dict(a)}


@router.post("/meetpp/sessions/{session_id}/minutes/{item_id}/regenerate")
async def regenerate_minutes(
    session_id: str,
    item_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    """Re-run the LLM for one agenda item's minutes (chair only)."""
    item = db.get(MeetppAgendaItem, item_id)
    if item is None or item.session_id != session_id:
        raise HTTPException(status_code=404, detail="item not found")
    segments = (
        db.query(MeetppSegment).filter_by(session_id=session_id).order_by(MeetppSegment.seq).all()
    )
    transcript = "\n".join(f"[{s.seq}] {s.name or s.identity}: {s.text}" for s in segments)[-60000:]
    messages = [
        {
            "role": "system",
            "content": (
                f"Write the minutes of one agenda item in {ctx.session.language}. Third person, factual, "
                "at most 1500 characters. Output json only: {\"body_md\": \"...\"}"
            ),
        },
        {"role": "user", "content": f"ITEM: {item.title}\nTRANSCRIPT:\n{transcript}"},
    ]
    try:
        result = await llm.complete_json(
            db=db, purpose="regenerate", messages=messages, max_tokens=900, temperature=0.2,
            session_id=session_id, enforce_budget=False,
        )
        parsed = llm.parse_json(result.text)
        body = str(parsed.get("body_md") or "").strip()
    except llm.LLMError as exc:
        raise HTTPException(status_code=502, detail=f"LLM unavailable: {exc}") from exc
    if not body:
        raise HTTPException(status_code=502, detail="LLM returned no minutes")
    minute = (
        db.query(MeetppMinute)
        .filter_by(session_id=session_id, agenda_item_id=item_id)
        .first()
    )
    if minute is None:
        minute = MeetppMinute(id=_ulid(), session_id=session_id, agenda_item_id=item_id, body_md=body[:1500], origin="ai")
        db.add(minute)
    else:
        minute.body_md = body[:1500]
        minute.version += 1
        minute.locked = False
    ctx.session.state_version += 1
    db.commit()
    await runtime_mod.runtime.get(session_id)._broadcast(
        "state",
        version=ctx.session.state_version,
        changes=[{"kind": "minutes", "id": minute.id, "op": "update"}],
        delta={"minutes": [ops.minute_dict(minute)]},
    )
    return {"minute": ops.minute_dict(minute)}


@router.get("/meetpp/tts/{hash_name}")
async def get_tts(hash_name: str) -> FileResponse:
    if not (hash_name.endswith(".ogg") or hash_name.endswith(".wav")):
        raise HTTPException(status_code=404, detail="not found")
    digest, ext = hash_name.rsplit(".", 1)
    if not _safe_hex(digest):
        raise HTTPException(status_code=404, detail="not found")
    path = Path(settings.meetpp_data_dir) / "tts" / f"{digest}.{ext}"
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(path), media_type="audio/ogg" if ext == "ogg" else "audio/wav")


def _safe_hex(value: str) -> bool:
    return len(value) <= 64 and all(c in "0123456789abcdef" for c in value.lower())


# ─── finalise / review / publish ────────────────────────────────────────────


@router.post("/meetpp/sessions/{session_id}/finalise")
async def finalise(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    final = await runtime_mod.runtime.get(session_id).finalise(session_id)
    return {"ok": True, "final": final}


@router.get("/meetpp/sessions/{session_id}/review")
async def get_review(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    s = ctx.session
    meeting = ctx.meeting
    final = None
    if s.final_json:
        try:
            final = json.loads(s.final_json)
        except ValueError:
            final = None
    booking = invites_mod.parse_booking(s, meeting)
    state = ops.build_state(db, s)
    return {
        "session": ops.session_meta(s),
        "final": final,
        "booking": booking,
        "agenda": state["agenda"],
        "attendance": state["attendance"],
        "minutes": state["minutes"],
        "decisions": state["decisions"],
        "actions": state["actions"],
        "attachments": state["attachments"],
        "error": s.error,
    }


class ReviewBody(BaseModel):
    booking: dict | None = None
    final: dict | None = None
    # Edited minutes from the review screen: [{id, body_md}] or [{item_id, body_md}].
    minutes: list[dict] | None = None


@router.put("/meetpp/sessions/{session_id}/review")
async def put_review(
    session_id: str,
    body: ReviewBody,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    s = ctx.session
    if body.booking is not None:
        s.review_json = json.dumps(body.booking)
    if body.final is not None:
        s.final_json = json.dumps(body.final)
    if body.minutes is not None:
        for entry in body.minutes:
            if not isinstance(entry, dict):
                continue
            body_md = entry.get("body_md")
            if body_md is None:
                continue
            minute = None
            if entry.get("id"):
                minute = db.get(MeetppMinute, str(entry["id"]))
                if minute is not None and minute.session_id != session_id:
                    minute = None
            if minute is None and entry.get("item_id"):
                minute = (
                    db.query(MeetppMinute)
                    .filter_by(session_id=session_id, agenda_item_id=str(entry["item_id"]))
                    .first()
                )
            if minute is None:
                minute = MeetppMinute(
                    id=_ulid(),
                    session_id=session_id,
                    agenda_item_id=(str(entry["item_id"]) if entry.get("item_id") else None),
                    body_md=str(body_md)[:1500],
                    origin="user",
                )
                db.add(minute)
            else:
                minute.body_md = str(body_md)[:1500]
                minute.version += 1
            # A human edit locks the item against AI overwrites.
            minute.locked = True
            minute.status = "edited"
    db.commit()
    return {"ok": True}


@router.post("/meetpp/sessions/{session_id}/publish")
async def publish(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = await invites_mod.publish(db, ctx.session)
    except Exception as exc:  # noqa: BLE001
        log.exception("meetpp: publish failed")
        raise HTTPException(status_code=500, detail=f"publish failed: {exc}") from exc
    return {"ok": True, **result}


@router.get("/meetpp/sessions/{session_id}/outputs/{output_id}")
async def get_output(
    session_id: str,
    output_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> FileResponse:
    out = db.get(MeetppOutput, output_id)
    if out is None or out.session_id != session_id or not out.path:
        raise HTTPException(status_code=404, detail="output not found")
    media = "application/pdf" if out.filename and out.filename.endswith(".pdf") else "text/calendar"
    return FileResponse(out.path, media_type=media, filename=out.filename or "output")


@router.get("/meetpp/sessions/{session_id}/export.json")
async def export_json_endpoint(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> dict:
    return render.export_json(db, ctx.session)


@router.get("/meetpp/sessions/{session_id}/export.md")
async def export_md_endpoint(
    session_id: str,
    ctx: ChairContext = Depends(require_chair_session),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return JSONResponse(
        content={"markdown": render.export_markdown(db, ctx.session)},
        media_type="application/json",
    )


@router.delete("/meetpp/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    session = db.get(MeetppSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    meeting = db.get(Meeting, session.meeting_id)
    if meeting is None or meeting.owner_user_id != user.sub:
        raise HTTPException(status_code=404, detail="session not found")
    await runtime_mod.runtime.get(session_id).stop_runner()
    runtime_mod.runtime.runners.pop(session_id, None)
    db.delete(session)
    db.commit()
    _purge_session_files(session_id)
    return {"ok": True}


def _purge_session_files(session_id: str) -> None:
    import shutil

    path = Path(settings.meetpp_data_dir) / session_id
    if path.exists() and session_id.replace("-", "").isalnum():
        shutil.rmtree(path, ignore_errors=True)


# ─── admin ──────────────────────────────────────────────────────────────────


@router.post("/admin/meetpp/sessions/{session_id}/end")
async def admin_end_session(
    session_id: str,
    user: RequirePlatformAdmin,
    db: Session = Depends(get_db),
) -> dict:
    """Operator kill switch: end a running session gracefully."""
    session = db.get(MeetppSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        await runtime_mod.runtime.end_session(session_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("meetpp: admin end failed")
        raise HTTPException(status_code=500, detail=str(exc)[:200]) from exc
    log.info("MEETPP_SESSION sid=%s event=admin_end actor=%s", session_id, user.sub)
    return {"ok": True}


@router.get("/admin/meetpp/status")
async def admin_status(user: RequirePlatformAdmin, db: Session = Depends(get_db)) -> dict:
    sessions = (
        db.query(MeetppSession)
        .filter(MeetppSession.status.in_(("running", "paused", "finalising")))
        .all()
    )
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    tokens = (
        db.query(func.sum(MeetppLlmCall.prompt_tokens))
        .filter(MeetppLlmCall.created_at >= today)
        .scalar()
        or 0
    )
    agent_health = await runtime_mod.runtime.get("__probe__").agent.health() if sessions else None
    rejected = (
        db.query(MeetppOp)
        .filter(MeetppOp.status == "rejected")
        .order_by(MeetppOp.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "enabled": settings.meetpp_enabled,
        "active_sessions": [
            {
                "sid": s.id,
                "meeting_id": s.meeting_id,
                "status": s.status,
                "phase": s.phase,
                "language": s.language,
            }
            for s in sessions
        ],
        "agent_health": agent_health,
        "breaker": llm.circuit_state(),
        "tokens_today": int(tokens),
        "rejected_ops": [
            {"op_type": r.op_type, "reason": r.reason, "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rejected
        ],
    }


# ─── internal (agent → meeting-api) ─────────────────────────────────────────


@router.post("/internal/meetpp/sessions/{session_id}/segments", dependencies=[Depends(require_internal)])
async def internal_segments(session_id: str, request: Request) -> dict:
    body = await request.json()
    segments = body if isinstance(body, list) else body.get("segments", [])
    n = await runtime_mod.runtime.get(session_id).ingest_segments(session_id, segments)
    return {"inserted": n}


@router.post("/internal/meetpp/sessions/{session_id}/presence", dependencies=[Depends(require_internal)])
async def internal_presence(session_id: str, request: Request) -> dict:
    body = await request.json()
    events = body if isinstance(body, list) else [body]
    await runtime_mod.runtime.get(session_id).apply_presence(session_id, events)
    return {"ok": True}


@router.post("/internal/meetpp/sessions/{session_id}/agent-status", dependencies=[Depends(require_internal)])
async def internal_agent_status(session_id: str, request: Request) -> dict:
    body = await request.json()
    await runtime_mod.runtime.get(session_id)._broadcast(
        "agent",
        status=body.get("status") or "listening",
        backlog_s=body.get("backlog_s"),
    )
    return {"ok": True}

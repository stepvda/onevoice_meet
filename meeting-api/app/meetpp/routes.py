"""Meet++ REST and internal endpoints (contract §2)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import AuthUser, RequirePlatformAdmin, RequireUser
from app.config import settings
from app.db import get_db
from app.livekit_client import mint_agent_token
from app.meetpp import (
    bus,
    compose,
    export as export_mod,
    governance,
    ingest as ingest_mod,
    invites as invites_mod,
    llm,
    ops,
    outline as outline_mod,
    runtime as rt,
    util,
)
from app.meetpp.auth import (
    Access,
    ChairContext,
    chair_access,
    edit_access,
    is_moderator,
    meetpp_allowed,
    require_chair_session,
    require_internal,
    require_meeting_chair,
    room_access,
)
from app.meetpp.models import (
    MeetppAction,
    MeetppActionReport,
    MeetppAttachment,
    MeetppAttendee,
    MeetppBallot,
    MeetppConsent,
    MeetppDecision,
    MeetppDocument,
    MeetppLlmCall,
    MeetppMinute,
    MeetppMinutesVersion,
    MeetppOp,
    MeetppOutput,
    MeetppRoster,
    MeetppSection,
    MeetppSegment,
    MeetppSeries,
    MeetppSession,
    MeetppVote,
)
from app.models import Meeting

log = logging.getLogger("app.meetpp")

router = APIRouter(prefix="/v1")

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
LIVE_STATUSES = ("setup", "running", "paused", "finalising")


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _require_pilot(meeting: Meeting) -> None:
    if not meetpp_allowed(meeting):
        raise HTTPException(status_code=404, detail="Meet++ is not enabled")


def _active_count(db: Session, exclude: str | None = None) -> int:
    q = db.query(MeetppSession).filter(MeetppSession.status.in_(("running", "paused", "finalising")))
    if exclude:
        q = q.filter(MeetppSession.id != exclude)
    return q.count()


def _series_for(meeting: Meeting, user: AuthUser, series_id: str | None, db: Session) -> MeetppSeries:
    if series_id:
        series = db.get(MeetppSeries, series_id)
        if series is None or series.owner_sub not in (user.sub, meeting.owner_user_id):
            raise HTTPException(status_code=404, detail="series not found")
        meeting.meetpp_series_id = series.id
        return series
    if meeting.meetpp_series_id:
        series = db.get(MeetppSeries, meeting.meetpp_series_id)
        if series is not None:
            return series
    series = MeetppSeries(id=util.ulid(), owner_sub=meeting.owner_user_id, title=meeting.display_title, meeting_id=meeting.id)
    db.add(series)
    db.flush()
    meeting.meetpp_series_id = series.id
    return series


def _require_series_chair(series_id: str, user: AuthUser, db: Session) -> MeetppSeries:
    series = db.get(MeetppSeries, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="series not found")
    if series.owner_sub == user.sub:
        return series
    meetings = db.query(Meeting).filter((Meeting.meetpp_series_id == series.id) | (Meeting.id == series.meeting_id)).all()
    if any(is_moderator(m, user.sub) for m in meetings):
        return series
    raise HTTPException(status_code=404, detail="series not found")


async def _publish(db: Session, session: MeetppSession, changes: ops.Changes) -> int | None:
    return await bus.publish_changes(db, session, changes)


# ─── 2.1 Setup and lifecycle ────────────────────────────────────────────────


class CreateSessionBody(_Body):
    template: str = "agenda"
    mode: str = "lead"
    series_id: str | None = None
    meeting_type: str | None = None
    goal: str | None = None


@router.post("/meetings/{meeting_id}/meetpp/sessions", status_code=201)
async def create_session(meeting_id: str, body: CreateSessionBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    meeting = require_meeting_chair(meeting_id, user, db)
    _require_pilot(meeting)
    existing = db.query(MeetppSession).filter(MeetppSession.meeting_id == meeting.id, MeetppSession.status.in_(LIVE_STATUSES)).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="A Meet++ session is already active for this room")
    series = _series_for(meeting, user, body.series_id, db)
    if body.meeting_type in governance.MEETING_TYPES:
        series.meeting_type = body.meeting_type
    session = MeetppSession(
        id=util.ulid(),
        meeting_id=meeting.id,
        series_id=series.id,
        created_by_user_id=user.sub,
        status="setup",
        template=body.template if body.template in ("agenda", "goal") else "agenda",
        mode=body.mode if body.mode in ("lead", "assist") else settings.meetpp_default_mode,
        language="en",
        goal=util.truncate(body.goal, 2000),
        settings_json=util.dumps({"show_public": False, "in_recordings": True, "timebox_nudges": True, "speak": True}),
    )
    db.add(session)
    db.flush()
    outline_mod.create_fixed_sections(db, session)
    rt.expected_attendees(db, session)
    db.commit()
    log.info("MEETPP_SESSION sid=%s meeting_id=%s event=create template=%s mode=%s", session.id, meeting.id, session.template, session.mode)
    state = ops.build_state(db, session, include_emails=True)
    imported = [a for a in state["actions"] if a["previous"]]
    return {"session": state["session"], "series": ops.series_dto(db, series), "imported_actions": imported}


@router.get("/meetings/{meeting_id}/meetpp/sessions")
async def list_sessions(meeting_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    meeting = require_meeting_chair(meeting_id, user, db)
    rows = db.query(MeetppSession).filter_by(meeting_id=meeting.id).order_by(MeetppSession.created_at.desc()).all()
    return {
        "sessions": [
            {"id": s.id, "status": s.status, "started_at": util.iso(s.started_at), "ended_at": util.iso(s.ended_at), "published_at": util.iso(s.published_at)}
            for s in rows
        ]
    }


class PatchSessionBody(_Body):
    mode: str | None = None
    goal: str | None = None
    editors: list[str] | None = None
    settings: dict | None = None


@router.patch("/meetpp/sessions/{session_id}")
async def patch_session(session_id: str, body: PatchSessionBody, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> dict:
    s = ctx.session
    if body.mode in ("lead", "assist"):
        s.mode = body.mode
    if body.goal is not None:
        s.goal = util.truncate(body.goal, 2000)
    if body.editors is not None:
        # Editors are signed-in participants (`user-<sub>` identities) only.
        s.editors_json = util.dumps(sorted({str(e)[:200] for e in body.editors if str(e).startswith("user-")})[:50])
    if body.settings is not None:
        current = util.loads(s.settings_json, {})
        for key in ("show_public", "in_recordings", "timebox_nudges", "speak"):
            if key in body.settings:
                current[key] = bool(body.settings[key])
        s.settings_json = util.dumps(current)
    await _publish(db, s, ops.Changes(session=True))
    if s.status in ("running", "paused"):
        asyncio.get_running_loop().create_task(rt.update_room_metadata(s.id, True))
    return ops.session_meta(db, s)


class SubpointIn(_Body):
    id: str | None = None
    title: str = ""
    body: str | None = None


class AgendaPointIn(_Body):
    id: str | None = None
    title: str = ""
    body: str | None = None
    presenter: str | None = None
    timebox_minutes: int | None = None
    subpoints: list[SubpointIn] = Field(default_factory=list)


class OutlineBody(_Body):
    agenda: list[AgendaPointIn] = Field(default_factory=list)


@router.put("/meetpp/sessions/{session_id}/outline")
async def put_outline(session_id: str, body: OutlineBody, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> dict:
    s = ctx.session
    if s.status not in ("setup", "running", "paused"):
        raise HTTPException(status_code=409, detail=f"the outline cannot change while {s.status}")
    before = {x.id for x in db.query(MeetppSection.id).filter_by(session_id=s.id).all()}
    try:
        changed = outline_mod.replace_agenda(db, s, [p.model_dump() for p in body.agenda[:60]])
    except outline_mod.OutlineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    after = {x.id for x in db.query(MeetppSection.id).filter_by(session_id=s.id).all()}
    changes = ops.Changes(sections=after | (changed & after))
    changes.removed.extend({"kind": "section", "id": sid} for sid in sorted(before - after))
    await _publish(db, s, changes)
    state = ops.build_state(db, s)
    return {"sections": state["sections"]}


@router.post("/meetpp/sessions/{session_id}/documents", status_code=202)
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
    doc_id = util.ulid()
    path = upload_dir / f"{doc_id}.pdf"
    path.write_bytes(data)
    import hashlib

    doc = MeetppDocument(
        id=doc_id,
        session_id=session_id,
        kind=kind,
        filename=(file.filename or "document.pdf")[:400],
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        status="uploaded",
    )
    db.add(doc)
    db.commit()
    background.add_task(ingest_mod.process_document, doc_id)
    return JSONResponse(status_code=202, content={"document": ops.document_dto(doc)})


@router.get("/meetpp/sessions/{session_id}/documents/{doc_id}")
async def get_document(session_id: str, doc_id: str, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> dict:
    doc = db.get(MeetppDocument, doc_id)
    if doc is None or doc.session_id != session_id:
        raise HTTPException(status_code=404, detail="document not found")
    dto = ops.document_dto(doc)
    out = {"document": dto, "summary": dto["summary"]}
    if doc.structured_json:
        out["structured"] = util.loads(doc.structured_json, {})
    return out


@router.post("/meetpp/sessions/{session_id}/start")
async def start_session(session_id: str, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> dict:
    s = ctx.session
    if s.status != "setup":
        raise HTTPException(status_code=409, detail=f"cannot start from status {s.status}")
    if _active_count(db, exclude=s.id) >= settings.meetpp_max_active_sessions:
        raise HTTPException(status_code=503, detail="AI capacity busy — try again later")
    db.commit()
    return await rt.start_session(s.id)


@router.post("/meetpp/sessions/{session_id}/pause")
async def pause_session(session_id: str, ctx: ChairContext = Depends(require_chair_session)) -> dict:
    try:
        return await rt.pause_session(session_id, True)
    except rt.PositionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


@router.post("/meetpp/sessions/{session_id}/resume")
async def resume_session(session_id: str, ctx: ChairContext = Depends(require_chair_session)) -> dict:
    try:
        return await rt.pause_session(session_id, False)
    except rt.PositionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


@router.post("/meetpp/sessions/{session_id}/end")
async def end_session(session_id: str, ctx: ChairContext = Depends(require_chair_session)) -> dict:
    if ctx.session.status not in ("running", "paused"):
        raise HTTPException(status_code=409, detail=f"cannot end from status {ctx.session.status}")
    await rt.end_session(session_id)
    return {"status": "finalising"}


@router.post("/meetpp/sessions/{session_id}/finalise")
async def finalise(session_id: str, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> dict:
    s = ctx.session
    if s.status not in ("finalising", "review", "published"):
        raise HTTPException(status_code=409, detail=f"cannot finalise from status {s.status}")
    jobs = rt._jobs(s)
    for name in rt.JOB_ORDER:
        if jobs[name].get("status") in ("failed", "running") or (s.status != "finalising" and jobs[name].get("status") == "pending"):
            jobs[name] = {"status": "pending"}
    s.jobs_json = util.dumps(jobs)
    await _publish(db, s, ops.Changes(session=True))
    if s.status == "finalising":
        rt.runtime.start_finalisation(session_id)
    else:
        asyncio.get_running_loop().create_task(rt.run_finalisation(session_id))
    return {"jobs": jobs}


@router.delete("/meetpp/sessions/{session_id}")
async def delete_session(session_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    session = db.get(MeetppSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    meeting = db.get(Meeting, session.meeting_id)
    if meeting is None or meeting.owner_user_id != user.sub:
        raise HTTPException(status_code=404, detail="session not found")
    await rt.runtime.drop(session_id)
    from app.meetpp import agent as agent_mod

    await agent_mod.client.stop(session_id)
    _delete_session_rows(db, session_id)
    db.commit()
    path = Path(settings.meetpp_data_dir) / session_id
    if path.exists() and re.fullmatch(r"[0-9A-Z]{26}", session_id):
        shutil.rmtree(path, ignore_errors=True)
    return {"ok": True}


def _delete_session_rows(db: Session, sid: str) -> None:
    decision_ids = [d.id for d in db.query(MeetppDecision.id).filter_by(session_id=sid).all()]
    if decision_ids:
        vote_ids = [v.id for v in db.query(MeetppVote.id).filter(MeetppVote.decision_id.in_(decision_ids)).all()]
        if vote_ids:
            db.query(MeetppBallot).filter(MeetppBallot.vote_id.in_(vote_ids)).delete(synchronize_session=False)
            db.query(MeetppVote).filter(MeetppVote.id.in_(vote_ids)).delete(synchronize_session=False)
    action_ids = [a.id for a in db.query(MeetppAction.id).filter_by(session_id=sid).all()]
    if action_ids:
        db.query(MeetppActionReport).filter(MeetppActionReport.action_id.in_(action_ids)).delete(synchronize_session=False)
    for model in (
        MeetppActionReport, MeetppAction, MeetppDecision, MeetppSection, MeetppMinute, MeetppMinutesVersion,
        MeetppAttendee, MeetppAttachment, MeetppDocument, MeetppSegment, MeetppOp, MeetppConsent, MeetppOutput,
    ):
        db.query(model).filter(model.session_id == sid).delete(synchronize_session=False)
    db.query(MeetppLlmCall).filter(MeetppLlmCall.session_id == sid).delete(synchronize_session=False)
    db.query(MeetppSession).filter(MeetppSession.id == sid).delete(synchronize_session=False)


@router.post("/meetpp/sessions/{session_id}/board-to-main")
async def board_to_main(session_id: str, ctx: ChairContext = Depends(require_chair_session)) -> dict:
    """Make the board the presenter (same as presenting it from its tile)."""
    from app import stage
    from app.room_metadata import patch_room_metadata

    await patch_room_metadata(bus._lk(), ctx.meeting.room_name, lambda md: stage.chosen(md, stage.BOARD_KEY))
    return {"ok": True}


# ─── 2.2 Live ───────────────────────────────────────────────────────────────


@router.get("/meetpp/rooms/{room}/active")
async def room_active(room: str, db: Session = Depends(get_db)) -> dict:
    session = (
        db.query(MeetppSession)
        .join(Meeting, Meeting.id == MeetppSession.meeting_id)
        .filter(Meeting.room_name == room, MeetppSession.status.in_(("running", "paused")))
        .first()
    )
    if session is None:
        return {"active": False, "consent_version": "v3"}
    return {"active": True, "sid": session.id, "provider_label": settings.llm_provider_label, "consent_version": "v3"}


@router.get("/meetpp/sessions/{session_id}/state")
async def get_state(session_id: str, acc: Access = Depends(room_access), db: Session = Depends(get_db)) -> dict:
    state = ops.build_state(db, acc.session, include_emails=acc.user is not None)
    return ops.without_person_keys(state) if acc.viewer else state


@router.get("/meetpp/sessions/{session_id}/transcript")
async def get_transcript(session_id: str, after: int = 0, limit: int = 500, acc: Access = Depends(room_access), db: Session = Depends(get_db)) -> dict:
    if acc.viewer:
        raise HTTPException(status_code=404, detail="session not found")
    limit = max(1, min(int(limit or 500), 1000))
    rows = (
        db.query(MeetppSegment)
        .filter(MeetppSegment.session_id == session_id, MeetppSegment.seq > after)
        .order_by(MeetppSegment.seq)
        .limit(limit + 1)
        .all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return {"segments": [ops.segment_dto(r) for r in rows], "next_after": rows[-1].seq if more and rows else None}


class ConsentBody(_Body):
    decision: str
    person_key: str | None = None
    name: str | None = None


@router.post("/meetpp/sessions/{session_id}/consent")
async def post_consent(session_id: str, body: ConsentBody, acc: Access = Depends(room_access)) -> dict:
    if body.decision not in ("accept", "opt_out"):
        raise HTTPException(status_code=400, detail="decision must be accept or opt_out")
    if acc.principal is None or acc.read_only:
        raise HTTPException(status_code=403, detail="consent is given from the room")
    try:
        await rt.set_consent(session_id, acc.identity, body.decision, body.person_key, body.name or acc.name)
    except rt.ConsentError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"ok": True}


class PositionBody(_Body):
    action: str
    section_id: str | None = None


@router.post("/meetpp/sessions/{session_id}/position")
async def post_position(session_id: str, body: PositionBody, acc: Access = Depends(chair_access)) -> dict:
    try:
        return await rt.chair_move(session_id, body.action, body.section_id)
    except rt.PositionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


@router.post("/meetpp/sessions/{session_id}/position/undo")
async def post_undo(session_id: str, acc: Access = Depends(chair_access)) -> dict:
    try:
        return await rt.undo_move(session_id)
    except rt.PositionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


class ProposalBody(_Body):
    pid: str
    accept: bool


@router.post("/meetpp/sessions/{session_id}/proposal")
async def post_proposal(session_id: str, body: ProposalBody, acc: Access = Depends(chair_access)) -> dict:
    try:
        await rt.answer_proposal(session_id, body.pid, body.accept)
    except rt.PositionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return {"ok": True}


class OpsBody(_Body):
    ops: list = Field(default_factory=list)


@router.post("/meetpp/sessions/{session_id}/ops")
async def post_ops(session_id: str, body: OpsBody, acc: Access = Depends(edit_access), db: Session = Depends(get_db)) -> dict:
    session = acc.session
    ctx = ops.ApplyContext(db=db, session=session, actor=f"user:{acc.identity}")
    ops.apply_ops(ctx, body.ops)
    version = await _publish(db, ctx.session, ctx.changes)
    for sec in ctx.compose:
        compose.schedule_section(session_id, sec)
    return {"applied": ctx.applied, "rejected": ctx.rejected, "version": version or ctx.session.state_version}


@router.post("/meetpp/sessions/{session_id}/sections/{section_id}/compose")
async def post_compose(session_id: str, section_id: str, acc: Access = Depends(edit_access), db: Session = Depends(get_db)) -> dict:
    o = outline_mod.load(db, session_id)
    section = o.by_id.get(section_id)
    if section is None:
        raise HTTPException(status_code=404, detail="section not found")
    compose.schedule_section(session_id, o.top(section).id, force=True)
    return {"status": "composing"}


def _section_for_upload(db: Session, session: MeetppSession, section_id: str | None) -> str | None:
    if section_id:
        s = db.get(MeetppSection, section_id)
        if s is not None and s.session_id == session.id:
            return s.id
    return session.live_section_id


@router.post("/meetpp/sessions/{session_id}/attachments")
async def upload_attachment(
    session_id: str,
    file: UploadFile = File(...),
    caption: str = Form(""),
    section_id: str | None = Form(None),
    acc: Access = Depends(room_access),
    db: Session = Depends(get_db),
) -> dict:
    session = acc.session
    if acc.read_only:
        raise HTTPException(status_code=404, detail="session not found")
    if not acc.can_edit and not util.loads(session.settings_json, {}).get("snapshot_any", True):
        raise HTTPException(status_code=404, detail="session not found")
    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=400, detail="only PNG snapshots are accepted")
    att_dir = Path(settings.meetpp_data_dir) / session_id / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    att_id = util.ulid()
    path = att_dir / f"{att_id}.png"
    path.write_bytes(data)
    att = MeetppAttachment(
        id=att_id,
        session_id=session_id,
        section_id=_section_for_upload(db, session, section_id),
        kind="whiteboard",
        path=str(path),
        filename="whiteboard.png",
        caption=util.truncate(caption, 400) or "Whiteboard snapshot",
        author=util.truncate(acc.name, 200),
    )
    db.add(att)
    db.flush()
    await _publish(db, session, ops.Changes(attachments={att.id}))
    return {"attachment": ops.attachment_dto(att)}


@router.get("/meetpp/sessions/{session_id}/attachments/{att_id}")
async def get_attachment(session_id: str, att_id: str, acc: Access = Depends(room_access), db: Session = Depends(get_db)) -> FileResponse:
    att = db.get(MeetppAttachment, att_id)
    if att is None or att.session_id != session_id or not Path(att.path).exists():
        raise HTTPException(status_code=404, detail="attachment not found")
    ext = (att.filename or "").lower().rsplit(".", 1)[-1]
    media = {"png": "image/png", "pdf": "application/pdf"}.get(ext, "application/octet-stream")
    return FileResponse(att.path, media_type=media, filename=att.filename)


class AttachmentPatch(_Body):
    caption: str | None = None


@router.patch("/meetpp/sessions/{session_id}/attachments/{att_id}")
async def patch_attachment(session_id: str, att_id: str, body: AttachmentPatch, acc: Access = Depends(edit_access), db: Session = Depends(get_db)) -> dict:
    att = db.get(MeetppAttachment, att_id)
    if att is None or att.session_id != session_id:
        raise HTTPException(status_code=404, detail="attachment not found")
    if body.caption is not None:
        att.caption = util.truncate(body.caption, 400)
    await _publish(db, acc.session, ops.Changes(attachments={att.id}))
    return {"attachment": ops.attachment_dto(att)}


@router.delete("/meetpp/sessions/{session_id}/attachments/{att_id}")
async def delete_attachment(session_id: str, att_id: str, acc: Access = Depends(edit_access), db: Session = Depends(get_db)) -> dict:
    att = db.get(MeetppAttachment, att_id)
    if att is None or att.session_id != session_id:
        raise HTTPException(status_code=404, detail="attachment not found")
    try:
        Path(att.path).unlink(missing_ok=True)
    except OSError:
        pass
    db.delete(att)
    changes = ops.Changes()
    changes.removed.append({"kind": "attachment", "id": att_id})
    await _publish(db, acc.session, changes)
    return {"ok": True}


class AttendeePatch(_Body):
    status: str | None = None
    voting: bool | None = None
    represented_by: str | None = None
    mandate_ref: str | None = None
    display_name: str | None = None
    email: str | None = None
    required_next: bool | None = None
    required_reason: str | None = None


@router.patch("/meetpp/sessions/{session_id}/attendees/{attendee_id}")
async def patch_attendee(session_id: str, attendee_id: str, body: AttendeePatch, acc: Access = Depends(edit_access), db: Session = Depends(get_db)) -> dict:
    a = db.get(MeetppAttendee, attendee_id)
    if a is None or a.session_id != session_id:
        raise HTTPException(status_code=404, detail="attendee not found")
    fields = body.model_dump(exclude_unset=True)
    if "status" in fields:
        if body.status not in ops.ATTENDANCE_STATUSES:
            raise HTTPException(status_code=400, detail="invalid status")
        a.status = body.status
    if "voting" in fields and body.voting is not None:
        a.voting = body.voting
    if "represented_by" in fields:
        a.represented_by = util.truncate(body.represented_by, 200)
    if "mandate_ref" in fields:
        a.mandate_ref = util.truncate(body.mandate_ref, 300)
    if body.display_name and body.display_name.strip():
        a.display_name = body.display_name.strip()[:200]
    if "email" in fields:
        a.email = (body.email or "").strip()[:300] or None
    if "required_next" in fields and body.required_next is not None:
        a.required_next = body.required_next
    if "required_reason" in fields:
        a.required_reason = util.truncate(body.required_reason, 300)
    roster = db.query(MeetppRoster).filter_by(series_id=acc.session.series_id, person_key=a.person_key).first()
    if roster is not None:
        if "voting" in fields and body.voting is not None:
            roster.voting = body.voting
        if "email" in fields and a.email:
            roster.email = a.email
        if body.display_name and body.display_name.strip():
            roster.display_name = a.display_name
    await _publish(db, acc.session, ops.Changes(attendees={a.id}, quorum=True))
    return ops.attendee_dto(a, include_email=True)


@router.get("/meetpp/tts/{name}")
async def get_tts(name: str) -> FileResponse:
    m = re.fullmatch(r"([0-9a-f]{16,64})\.(ogg|wav)", name or "")
    if not m:
        raise HTTPException(status_code=404, detail="not found")
    path = Path(settings.meetpp_data_dir) / "tts" / f"{m.group(1)}.{m.group(2)}"
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(path), media_type="audio/ogg" if m.group(2) == "ogg" else "audio/wav")


# ─── 2.3 Review and outputs ─────────────────────────────────────────────────


def _final_view(session: MeetppSession) -> dict:
    final = util.loads(session.final_json, {})
    return {
        "summary": final.get("summary") or [],
        "next_agenda": final.get("next_agenda") or [],
        "required_next": final.get("required_next") or [],
        "verify": final.get("verify") or [],
        "next_meeting_proposal": final.get("next_meeting_proposal"),
    }


@router.get("/meetpp/sessions/{session_id}/review")
async def get_review(session_id: str, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> dict:
    s = ctx.session
    state = ops.build_state(db, s, include_emails=True)
    return {
        "session": state["session"],
        "jobs": util.loads(s.jobs_json, {}),
        "review": invites_mod.review_draft(db, s),
        "final": _final_view(s),
        "state": state,
    }


@router.put("/meetpp/sessions/{session_id}/review")
async def put_review(session_id: str, body: dict, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> dict:
    s = ctx.session
    stored = util.loads(s.review_json, {})
    stored.update(invites_mod.clean_review(body if isinstance(body, dict) else {}))
    s.review_json = util.dumps(stored)
    db.commit()
    return {"review": invites_mod.review_draft(db, s)}


class VoteBody(_Body):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    method: str = "assent"
    for_: int | None = Field(default=None, alias="for")
    against: int | None = None
    abstain: int | None = None
    ballots: list[dict] | None = None
    question: str | None = None
    confirmed: bool | None = None


@router.put("/meetpp/decisions/{decision_id}/vote")
async def put_vote(decision_id: str, body: VoteBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    d = db.get(MeetppDecision, decision_id)
    if d is None:
        raise HTTPException(status_code=404, detail="decision not found")
    session = db.get(MeetppSession, d.session_id)
    meeting = db.get(Meeting, session.meeting_id) if session else None
    if meeting is None or not is_moderator(meeting, user.sub):
        raise HTTPException(status_code=404, detail="decision not found")
    data = {"method": body.method, "for": body.for_, "against": body.against, "abstain": body.abstain, "question": body.question}
    if body.ballots is not None:
        data["ballots"] = body.ballots
    vote = governance.apply_vote(db, session, d, data, confirmed=body.confirmed)
    if body.ballots is not None and not body.ballots:
        db.query(MeetppBallot).filter_by(vote_id=vote.id).delete(synchronize_session=False)
    d.locked = True
    if body.confirmed:
        d.confirmed = True
        if vote.result in ("adopted", "rejected"):
            d.status = vote.result
            d.decided_at = d.decided_at or util.now()
    await _publish(db, session, ops.Changes(decisions={d.id}))
    ballots = db.query(MeetppBallot).filter_by(vote_id=vote.id).order_by(MeetppBallot.id).all()
    return ops.decision_dto(d, vote, ballots)


class RulesBody(_Body):
    meeting_type: str | None = None
    majority_rule: str | None = None
    quorum_required: int | None = None


@router.put("/meetpp/series/{series_id}/rules")
async def put_rules(series_id: str, body: RulesBody, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    series = _require_series_chair(series_id, user, db)
    fields = body.model_dump(exclude_unset=True)
    if body.meeting_type in governance.MEETING_TYPES:
        series.meeting_type = body.meeting_type
    if body.majority_rule in governance.MAJORITY_RULES:
        series.majority_rule = body.majority_rule
    if "quorum_required" in fields:
        series.quorum_required = max(1, int(body.quorum_required)) if body.quorum_required else None
    db.flush()
    # Recompute the vote records of sessions that are not published yet; a
    # decision taken on a count follows its result under the new rules.
    for s in db.query(MeetppSession).filter(MeetppSession.series_id == series.id, MeetppSession.status != "published").all():
        for d in db.query(MeetppDecision).filter_by(session_id=s.id).all():
            if db.query(MeetppVote).filter_by(decision_id=d.id).first() is not None:
                vote = governance.apply_vote(db, s, d, {})
                if d.status in ("adopted", "rejected") and vote.result in ("adopted", "rejected"):
                    d.status = vote.result
        await _publish(db, s, ops.Changes(session=True, quorum=True, decisions={d.id for d in db.query(MeetppDecision).filter_by(session_id=s.id).all()}))
    db.commit()
    return ops.series_dto(db, series)


@router.get("/meetpp/series/{series_id}/roster")
async def get_roster(series_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    series = _require_series_chair(series_id, user, db)
    rows = db.query(MeetppRoster).filter_by(series_id=series.id).order_by(MeetppRoster.display_name).all()
    return {"roster": [ops.roster_dto(r) for r in rows]}


class RosterPatch(_Body):
    voting: bool | None = None
    active: bool | None = None
    display_name: str | None = None


@router.patch("/meetpp/series/{series_id}/roster/{roster_id}")
async def patch_roster(series_id: str, roster_id: str, body: RosterPatch, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    series = _require_series_chair(series_id, user, db)
    r = db.get(MeetppRoster, roster_id)
    if r is None or r.series_id != series.id:
        raise HTTPException(status_code=404, detail="roster member not found")
    if body.voting is not None:
        r.voting = body.voting
    if body.active is not None:
        r.active = body.active
    if body.display_name and body.display_name.strip():
        r.display_name = body.display_name.strip()[:200]
    for s in db.query(MeetppSession).filter(MeetppSession.series_id == series.id, MeetppSession.status != "published").all():
        a = db.query(MeetppAttendee).filter_by(session_id=s.id, person_key=r.person_key).first()
        if a is None:
            continue
        if body.voting is not None:
            a.voting = body.voting
        if body.display_name and body.display_name.strip():
            a.display_name = r.display_name
        await _publish(db, s, ops.Changes(attendees={a.id}, quorum=True))
    db.commit()
    return ops.roster_dto(r)


@router.get("/meetpp/sessions/{session_id}/report.pdf")
async def report_pdf(session_id: str, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> Response:
    data = export_mod.build_export(db, ctx.session)
    try:
        pdf = await invites_mod.render_report(data)
    except invites_mod.ReportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": "inline; filename=report-preview.pdf"})


@router.post("/meetpp/sessions/{session_id}/publish")
async def publish(session_id: str, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> dict:
    s = ctx.session
    if s.status not in ("review", "published"):
        raise HTTPException(status_code=409, detail=f"cannot publish while {s.status}")
    try:
        result = await invites_mod.publish(db, s)
    except invites_mod.ReportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("meetpp: publish failed")
        raise HTTPException(status_code=500, detail=f"publish failed: {exc}"[:300]) from exc
    room = ctx.meeting.room_name
    await bus.send(room, bus.message("session", session_id, state="published"))
    return result


@router.get("/meetpp/sessions/{session_id}/outputs")
async def get_outputs(session_id: str, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> dict:
    return {"outputs": invites_mod.list_outputs(db, ctx.session)}


@router.get("/meetpp/sessions/{session_id}/outputs/{output_id}")
async def get_output(session_id: str, output_id: str, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> FileResponse:
    out = db.get(MeetppOutput, output_id)
    if out is None or out.session_id != session_id or not out.path or not Path(out.path).exists():
        raise HTTPException(status_code=404, detail="output not found")
    media = {"pdf": "application/pdf", "ics": "text/calendar"}.get((out.filename or "").rsplit(".", 1)[-1], "application/octet-stream")
    return FileResponse(out.path, media_type=media, filename=out.filename or "output")


@router.get("/meetpp/sessions/{session_id}/export.json")
async def export_json(session_id: str, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> dict:
    return export_mod.build_export(db, ctx.session)


@router.get("/meetpp/sessions/{session_id}/export.md")
async def export_md(session_id: str, ctx: ChairContext = Depends(require_chair_session), db: Session = Depends(get_db)) -> PlainTextResponse:
    return PlainTextResponse(compose.minutes_markdown(db, ctx.session), media_type="text/markdown; charset=utf-8")


# ─── Admin ──────────────────────────────────────────────────────────────────


@router.post("/admin/meetpp/sessions/{session_id}/end")
async def admin_end_session(session_id: str, user: RequirePlatformAdmin, db: Session = Depends(get_db)) -> dict:
    session = db.get(MeetppSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.status in ("running", "paused"):
        await rt.end_session(session_id)
    elif session.status == "setup":
        session.status = "aborted"
        db.commit()
    log.info("MEETPP_SESSION sid=%s event=admin_end actor=%s", session_id, user.sub)
    return {"ok": True}


@router.get("/admin/meetpp/status")
async def admin_status(user: RequirePlatformAdmin, db: Session = Depends(get_db)) -> dict:
    from app.meetpp import agent as agent_mod

    sessions = db.query(MeetppSession).filter(MeetppSession.status.in_(("setup", "running", "paused", "finalising"))).all()
    today = util.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tokens = db.query(func.coalesce(func.sum(MeetppLlmCall.prompt_tokens), 0)).filter(MeetppLlmCall.created_at >= today).scalar() or 0
    rejected = db.query(MeetppOp).filter(MeetppOp.status == "rejected").order_by(MeetppOp.created_at.desc()).limit(50).all()
    failed_compositions = (
        db.query(MeetppMinute).filter(MeetppMinute.status == "failed").order_by(MeetppMinute.updated_at.desc()).limit(20).all()
    )
    health = await agent_mod.client.health() if sessions else None
    out_sessions = []
    for s in sessions:
        last_tick = db.query(MeetppLlmCall).filter_by(session_id=s.id, purpose="tick").order_by(MeetppLlmCall.created_at.desc()).first()
        out_sessions.append(
            {
                "sid": s.id,
                "meeting_id": s.meeting_id,
                "status": s.status,
                "live_section_id": s.live_section_id,
                "agent": util.loads(s.agent_json, {}),
                "agent_health": agent_mod.session_health(health, s.id),
                "jobs": util.loads(s.jobs_json, {}),
                "tokens_last_hour": llm.tokens_last_hour(db, s.id),
                "last_tick_ms": last_tick.latency_ms if last_tick else None,
                "ai": llm.ai_status(s.id),
            }
        )
    return {
        "enabled": settings.meetpp_enabled,
        "active_sessions": out_sessions,
        "agent_health": health,
        "breakers": {"tick": llm.circuit_state("tick"), "compose": llm.circuit_state("compose")},
        "tokens_today": int(tokens),
        "rejected_ops": [
            {"sid": r.session_id, "op_type": r.op_type, "reason": r.reason, "created_at": util.iso(r.created_at)} for r in rejected
        ],
        "composition_errors": [
            {"sid": m.session_id, "section_id": m.section_id, "error": m.error, "at": util.iso(m.updated_at)} for m in failed_compositions
        ],
    }


# ─── 2.4 Internal (agent → meeting-api) ─────────────────────────────────────


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if isinstance(body, list):
        return {"segments": body}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid body")
    return body


@router.post("/internal/meetpp/sessions/{session_id}/segments", dependencies=[Depends(require_internal)])
async def internal_segments(session_id: str, request: Request) -> dict:
    return await rt.ingest(session_id, await _json(request))


@router.post("/internal/meetpp/sessions/{session_id}/presence", dependencies=[Depends(require_internal)])
async def internal_presence(session_id: str, request: Request) -> dict:
    body = await _json(request)
    events = body.get("events") if isinstance(body.get("events"), list) else ([body] if body.get("identity") else [])
    await rt.presence(session_id, events)
    return {"ok": True}


@router.post("/internal/meetpp/sessions/{session_id}/agent-status", dependencies=[Depends(require_internal)])
async def internal_agent_status(session_id: str, request: Request) -> dict:
    await rt.agent_status(session_id, await _json(request))
    return {"ok": True}


@router.post("/internal/meetpp/sessions/{session_id}/agent-token", dependencies=[Depends(require_internal)])
async def internal_agent_token(session_id: str, db: Session = Depends(get_db)) -> dict:
    session = db.get(MeetppSession, session_id)
    if session is None or session.status not in ("running", "paused", "finalising"):
        raise HTTPException(status_code=404, detail="session not active")
    meeting = db.get(Meeting, session.meeting_id)
    token = mint_agent_token(room_name=meeting.room_name, identity=f"meetpp-scribe-{session_id}", ttl_hours=12)
    return {"token": token, "ws_url": settings.meetpp_agent_ws_url}

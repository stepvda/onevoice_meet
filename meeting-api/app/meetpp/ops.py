"""Operation vocabulary, validator, applier and state serialisation.

The LLM only proposes operations; this module validates them against IDs,
provenance, human locks and limits, then applies them in a versioned,
audited transaction. Human edits use the same vocabulary.

Unknown fields are rejected (pydantic `extra="forbid"`).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.meetpp.models import (
    MeetppAction,
    MeetppAgendaItem,
    MeetppAttendee,
    MeetppAttachment,
    MeetppDecision,
    MeetppMinute,
    MeetppOp,
    MeetppSession,
    utcnow,
)

log = logging.getLogger("app.meetpp")

MAX_OPS_PER_TICK = 12
MAX_MINUTE_UPSERTS_PER_TICK = 3
MAX_AGENDA_ITEMS = 20
MAX_ATTENDEES = 30
DUPLICATE_JACCARD = 0.85


# ─── Schemas ────────────────────────────────────────────────────────────────


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgendaSetActive(_Base):
    op: Literal["agenda.set_active"]
    item_id: str


class AgendaSetStatus(_Base):
    op: Literal["agenda.set_status"]
    item_id: str
    status: Literal["done", "deferred"]


class AgendaAdd(_Base):
    op: Literal["agenda.add"]
    title: str = Field(max_length=300)
    presenter: str | None = Field(default=None, max_length=200)
    source: Literal["aob"] = "aob"


class AgendaUpdate(_Base):
    op: Literal["agenda.update"]
    item_id: str
    title: str | None = Field(default=None, max_length=300)
    presenter: str | None = Field(default=None, max_length=200)
    timebox_minutes: int | None = None
    outcome: str | None = Field(default=None, max_length=600)
    position: int | None = None


class DecisionAdd(_Base):
    op: Literal["decision.add"]
    item_id: str | None = None
    text: str = Field(max_length=600)
    rationale: str | None = Field(default=None, max_length=600)
    evidence: list[int] = Field(default_factory=list)


class DecisionUpdate(_Base):
    op: Literal["decision.update"]
    id: str
    text: str | None = Field(default=None, max_length=600)
    status: Literal["proposed", "confirmed", "rejected"] | None = None


class ActionAdd(_Base):
    op: Literal["action.add"]
    title: str = Field(max_length=300)
    owner_alias: str | None = Field(default=None, max_length=200)
    due: str | None = None
    item_id: str | None = None
    evidence: list[int] = Field(default_factory=list)


class ActionUpdate(_Base):
    op: Literal["action.update"]
    id: str
    status: Literal["open", "in_progress", "done", "dropped", "carried", "proposed"] | None = None
    owner_alias: str | None = Field(default=None, max_length=200)
    due: str | None = None
    note: str | None = Field(default=None, max_length=500)
    evidence: list[int] = Field(default_factory=list)


class MinutesUpsert(_Base):
    op: Literal["minutes.upsert"]
    item_id: str | None = None
    body_md: str = Field(max_length=1500)


class AttendanceApologies(_Base):
    op: Literal["attendance.apologies"]
    person: str = Field(max_length=200)
    evidence: list[int] = Field(default_factory=list)


class AttendanceRequireNext(_Base):
    op: Literal["attendance.require_next"]
    person: str = Field(max_length=200)
    reason: str = Field(default="", max_length=300)
    evidence: list[int] = Field(default_factory=list)


class PhaseSignal(_Base):
    op: Literal["phase.signal"]
    to: str
    confidence: float = 0.0
    reason: str = Field(default="", max_length=200)


class NextMeetingPropose(_Base):
    op: Literal["next_meeting.propose"]
    date_text: str = Field(default="", max_length=200)
    iso: str | None = None
    duration_min: int | None = None


AnyOp = (
    AgendaSetActive
    | AgendaSetStatus
    | AgendaAdd
    | AgendaUpdate
    | DecisionAdd
    | DecisionUpdate
    | ActionAdd
    | ActionUpdate
    | MinutesUpsert
    | AttendanceApologies
    | AttendanceRequireNext
    | PhaseSignal
    | NextMeetingPropose
)

_OP_TYPES: dict[str, type[BaseModel]] = {
    "agenda.set_active": AgendaSetActive,
    "agenda.set_status": AgendaSetStatus,
    "agenda.add": AgendaAdd,
    "agenda.update": AgendaUpdate,
    "decision.add": DecisionAdd,
    "decision.update": DecisionUpdate,
    "action.add": ActionAdd,
    "action.update": ActionUpdate,
    "minutes.upsert": MinutesUpsert,
    "attendance.apologies": AttendanceApologies,
    "attendance.require_next": AttendanceRequireNext,
    "phase.signal": PhaseSignal,
    "next_meeting.propose": NextMeetingPropose,
}


class TickOutput(_Base):
    ops: list[dict] = Field(default_factory=list, max_length=MAX_OPS_PER_TICK)
    phase_signal: dict | None = None
    notes: str | None = None


def parse_op(raw: dict) -> AnyOp | None:
    op_type = raw.get("op")
    model = _OP_TYPES.get(str(op_type))
    if model is None:
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None


# ─── Apply context ──────────────────────────────────────────────────────────


@dataclass
class AppliedOp:
    op_type: str
    kind: str
    id: str | None
    action: str  # add | update | remove
    status: str  # applied | rejected | suggested
    reason: str | None = None
    focus_prio: int = 0


@dataclass
class ApplyContext:
    db: Session
    session: MeetppSession
    actor: str
    now: datetime = field(default_factory=utcnow)
    # transcript window this tick covers, for evidence validation
    window_min: int = 0
    window_max: int = 10**9
    # alias -> (name, attendee person_key)
    aliases: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    applied: list[AppliedOp] = field(default_factory=list)
    rejected: list[AppliedOp] = field(default_factory=list)
    phase_signal: dict | None = None
    next_meeting: dict | None = None
    minute_upserts: int = 0
    has_agenda: bool = True
    # Target state_version for every op row written by this tick.
    version: int = 0

    def ver(self) -> int:
        return self.version or (self.session.state_version + 1)

    def agenda_items(self) -> list[MeetppAgendaItem]:
        return (
            self.db.query(MeetppAgendaItem)
            .filter_by(session_id=self.session.id)
            .order_by(MeetppAgendaItem.position)
            .all()
        )

    def find_item(self, item_id: str | None) -> MeetppAgendaItem | None:
        if not item_id:
            item = self.session.current_item_id
            if item:
                return self.db.get(MeetppAgendaItem, item)
            items = self.agenda_items()
            return items[0] if items else None
        return self.db.get(MeetppAgendaItem, item_id)


# ─── Validator / applier ────────────────────────────────────────────────────


def _norm_tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return {t for t in re.findall(r"\w+", text) if len(t) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_duplicate(text: str, existing: list[str]) -> bool:
    tokens = _norm_tokens(text)
    return any(_jaccard(tokens, _norm_tokens(e)) >= DUPLICATE_JACCARD for e in existing)


def _evidence_ok(ctx: ApplyContext, evidence: list[int]) -> bool:
    if not evidence:
        return False
    return all(ctx.window_min <= int(e) <= ctx.window_max for e in evidence)


def _resolve_owner(ctx: ApplyContext, alias: str | None) -> tuple[str | None, str | None]:
    if not alias:
        return None, None
    key = alias.strip()
    if key in ctx.aliases:
        return ctx.aliases[key]
    # quoted / free name — match a known attendee by normalised name
    needle = key.strip('"\u201c\u201d ')
    low = needle.lower()
    for a in ctx.db.query(MeetppAttendee).filter_by(session_id=ctx.session.id).all():
        if a.display_name.lower() == low or low in a.display_name.lower():
            return a.display_name, a.person_key
    return (needle or None), None


def _valid_due(value: str | None) -> str | None:
    if not value:
        return None
    try:
        d = date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
    if d < date.today():
        return None
    return d.isoformat()


def _reject(ctx: ApplyContext, op_type: str, reason: str) -> None:
    ctx.rejected.append(AppliedOp(op_type, _kind_for(op_type), None, "update", "rejected", reason))
    ctx.db.add(
        MeetppOp(
            session_id=ctx.session.id,
            version=ctx.ver(),
            op_type=op_type,
            payload_json="{}",
            actor=ctx.actor,
            status="rejected",
            reason=reason[:300],
        )
    )


def _kind_for(op_type: str) -> str:
    return {
        "agenda.set_active": "agenda",
        "agenda.set_status": "agenda",
        "agenda.add": "agenda",
        "agenda.update": "agenda",
        "decision.add": "decision",
        "decision.update": "decision",
        "action.add": "action",
        "action.update": "action",
        "minutes.upsert": "minutes",
        "attendance.apologies": "attendance",
        "attendance.require_next": "attendance",
        "phase.signal": "phase",
        "next_meeting.propose": "next_meeting",
    }.get(op_type, "other")


def _record(ctx: ApplyContext, op: AnyOp, kind: str, obj_id: str | None, action: str, prio: int) -> None:
    payload = op.model_dump() if hasattr(op, "model_dump") else {}
    ctx.applied.append(AppliedOp(op.op, kind, obj_id, action, "applied", None, prio))
    ctx.db.add(
        MeetppOp(
            session_id=ctx.session.id,
            version=ctx.ver(),
            op_type=op.op,
            payload_json=json.dumps(payload, ensure_ascii=False),
            actor=ctx.actor,
            status="applied",
            evidence_json=json.dumps(getattr(op, "evidence", []) or []),
        )
    )


def apply_ops(ctx: ApplyContext, raw_ops: list[dict]) -> list[AppliedOp]:
    """Validate and apply up to MAX_OPS_PER_TICK operations. Any op on a locked
    item becomes a suggestion (recorded, not applied)."""
    session = ctx.session
    for raw in raw_ops[:MAX_OPS_PER_TICK]:
        if not isinstance(raw, dict):
            continue
        op = parse_op(raw)
        if op is None:
            _reject(ctx, str(raw.get("op", "?")), "invalid op or schema")
            continue
        try:
            _apply_one(ctx, op)
        except Exception as exc:  # noqa: BLE001 — never let one op kill the tick
            log.exception("meetpp: op %s failed", getattr(op, "op", "?"))
            _reject(ctx, getattr(op, "op", "?"), f"error: {exc}")
    return ctx.applied


def _apply_one(ctx: ApplyContext, op: AnyOp) -> None:
    session = ctx.session
    kind = _kind_for(op.op)
    handler = {
        "agenda.set_active": _apply_agenda_active,
        "agenda.set_status": _apply_agenda_status,
        "agenda.add": _apply_agenda_add,
        "agenda.update": _apply_agenda_update,
        "decision.add": _apply_decision_add,
        "decision.update": _apply_decision_update,
        "action.add": _apply_action_add,
        "action.update": _apply_action_update,
        "minutes.upsert": _apply_minutes_upsert,
        "attendance.apologies": _apply_apologies,
        "attendance.require_next": _apply_require_next,
        "phase.signal": _apply_phase_signal,
        "next_meeting.propose": _apply_next_meeting,
    }[op.op]
    handler(ctx, op)


def _apply_agenda_active(ctx: ApplyContext, op: AgendaSetActive) -> None:
    item = ctx.db.get(MeetppAgendaItem, op.item_id)
    if item is None or item.session_id != ctx.session.id:
        _reject(ctx, op.op, "item not found")
        return
    for other in ctx.agenda_items():
        if other.status == "active" and other.id != item.id:
            other.status = "pending"
    if item.status == "pending":
        item.status = "active"
        item.started_at = ctx.now
    ctx.session.current_item_id = item.id
    _record(ctx, op, "agenda", item.id, "update", 5)


def _apply_agenda_status(ctx: ApplyContext, op: AgendaSetStatus) -> None:
    item = ctx.db.get(MeetppAgendaItem, op.item_id)
    if item is None or item.session_id != ctx.session.id:
        _reject(ctx, op.op, "item not found")
        return
    if item.locked:
        _suggest(ctx, op, "agenda", item.id)
        return
    item.status = op.status
    item.closed_at = ctx.now
    if ctx.session.current_item_id == item.id and op.status in ("done", "deferred"):
        ctx.session.current_item_id = None
    _record(ctx, op, "agenda", item.id, "update", 5)


def _apply_agenda_add(ctx: ApplyContext, op: AgendaAdd) -> None:
    phase = ctx.session.phase
    if phase not in ("discussion", "aob", "next_steps"):
        _reject(ctx, op.op, "agenda.add only allowed in discussion/aob")
        return
    items = ctx.agenda_items()
    if len(items) >= MAX_AGENDA_ITEMS:
        _reject(ctx, op.op, "agenda full")
        return
    item = MeetppAgendaItem(
        id=_ulid(),
        session_id=ctx.session.id,
        position=(items[-1].position + 1) if items else 1,
        title=op.title.strip()[:300],
        presenter=(op.presenter or None),
        source="aob",
        status="pending",
    )
    ctx.db.add(item)
    ctx.db.flush()
    _record(ctx, op, "agenda", item.id, "add", 5)


def _apply_agenda_update(ctx: ApplyContext, op: AgendaUpdate) -> None:
    item = ctx.db.get(MeetppAgendaItem, op.item_id)
    if item is None or item.session_id != ctx.session.id:
        _reject(ctx, op.op, "item not found")
        return
    if item.locked:
        _suggest(ctx, op, "agenda", item.id)
        return
    if op.title is not None:
        item.title = op.title.strip()[:300] or item.title
    if op.presenter is not None:
        item.presenter = op.presenter.strip()[:200] or None
    if op.timebox_minutes is not None:
        item.timebox_minutes = max(0, min(600, int(op.timebox_minutes)))
    if op.outcome is not None:
        item.desired_outcome = op.outcome.strip()[:600] or None
    if op.position is not None:
        items = [i for i in ctx.agenda_items() if i.id != item.id]
        target = max(1, min(len(items) + 1, int(op.position)))
        items.insert(target - 1, item)
        for idx, it in enumerate(items, start=1):
            it.position = idx
    _record(ctx, op, "agenda", item.id, "update", 5)


def _apply_decision_add(ctx: ApplyContext, op: DecisionAdd) -> None:
    if not _evidence_ok(ctx, op.evidence):
        _reject(ctx, op.op, "evidence missing or outside window")
        return
    item = ctx.find_item(op.item_id)
    existing = [d.text for d in ctx.db.query(MeetppDecision).filter_by(series_id=ctx.session.series_id).all()]
    if _is_duplicate(op.text, existing):
        _reject(ctx, op.op, "near-duplicate decision")
        return
    series = _series(ctx)
    series.decision_counter += 1
    decision = MeetppDecision(
        id=_ulid(),
        session_id=ctx.session.id,
        series_id=ctx.session.series_id,
        ref=f"D-{series.decision_counter:02d}",
        agenda_item_id=item.id if item else None,
        text=op.text.strip()[:600],
        rationale=(op.rationale or None),
        status="proposed",
        origin="ai" if ctx.actor == "ai" else "user",
        evidence_json=json.dumps(op.evidence),
    )
    ctx.db.add(decision)
    ctx.db.flush()
    _record(ctx, op, "decision", decision.id, "add", 4)


def _apply_decision_update(ctx: ApplyContext, op: DecisionUpdate) -> None:
    d = ctx.db.get(MeetppDecision, op.id)
    if d is None or d.session_id != ctx.session.id:
        _reject(ctx, op.op, "decision not found")
        return
    if d.locked:
        _suggest(ctx, op, "decision", d.id)
        return
    if op.text is not None:
        d.text = op.text.strip()[:600]
    if op.status is not None:
        d.status = op.status
    _record(ctx, op, "decision", d.id, "update", 3)


def _apply_action_add(ctx: ApplyContext, op: ActionAdd) -> None:
    if not _evidence_ok(ctx, op.evidence):
        _reject(ctx, op.op, "evidence missing or outside window")
        return
    item = ctx.find_item(op.item_id)
    owner_name, owner_key = _resolve_owner(ctx, op.owner_alias)
    existing = [a.title for a in ctx.db.query(MeetppAction).filter_by(series_id=ctx.session.series_id).all()]
    if _is_duplicate(op.title, existing):
        _reject(ctx, op.op, "near-duplicate action")
        return
    series = _series(ctx)
    series.action_counter += 1
    action = MeetppAction(
        id=_ulid(),
        series_id=ctx.session.series_id,
        session_id=ctx.session.id,
        ref=f"A-{series.action_counter:02d}",
        title=op.title.strip()[:300],
        owner_name=owner_name,
        owner_person_key=owner_key,
        due_date=_valid_due(op.due),
        agenda_item_id=item.id if item else None,
        status="proposed",
        origin="ai" if ctx.actor == "ai" else "user",
        evidence_json=json.dumps(op.evidence),
    )
    ctx.db.add(action)
    ctx.db.flush()
    _record(ctx, op, "action", action.id, "add", 3)


def _apply_action_update(ctx: ApplyContext, op: ActionUpdate) -> None:
    a = ctx.db.get(MeetppAction, op.id)
    if a is None or a.series_id != ctx.session.series_id:
        _reject(ctx, op.op, "action not found")
        return
    if a.locked:
        _suggest(ctx, op, "action", a.id)
        return
    if op.status is not None:
        a.status = op.status
        if ctx.session.phase in ("previous_actions",):
            a.review_status = op.status
    if op.owner_alias is not None:
        a.owner_name, a.owner_person_key = _resolve_owner(ctx, op.owner_alias)
    if op.due is not None:
        a.due_date = _valid_due(op.due)
    if op.note is not None:
        a.note = op.note[:500]
    _record(ctx, op, "action", a.id, "update", 3)


def _apply_minutes_upsert(ctx: ApplyContext, op: MinutesUpsert) -> None:
    if ctx.minute_upserts >= MAX_MINUTE_UPSERTS_PER_TICK:
        _reject(ctx, op.op, "minute upsert limit per tick")
        return
    item = ctx.find_item(op.item_id)
    minute = (
        ctx.db.query(MeetppMinute)
        .filter_by(session_id=ctx.session.id, agenda_item_id=(item.id if item else None))
        .first()
    )
    if minute and minute.locked:
        _suggest(ctx, op, "minutes", minute.id)
        return
    if minute is None:
        minute = MeetppMinute(
            id=_ulid(),
            session_id=ctx.session.id,
            agenda_item_id=item.id if item else None,
            body_md=op.body_md,
            origin="ai" if ctx.actor == "ai" else "user",
        )
        ctx.db.add(minute)
    else:
        minute.body_md = op.body_md
        minute.version += 1
        minute.origin = "ai" if ctx.actor == "ai" else "user"
    ctx.db.flush()
    ctx.minute_upserts += 1
    _record(ctx, op, "minutes", minute.id, "update", 1)


def _apply_apologies(ctx: ApplyContext, op: AttendanceApologies) -> None:
    if not _evidence_ok(ctx, op.evidence):
        _reject(ctx, op.op, "evidence missing or outside window")
        return
    a = _find_attendee(ctx, op.person)
    if a is None:
        _reject(ctx, op.op, "person not known")
        return
    a.presence = "apologies"
    a.required_now = True
    _record(ctx, op, "attendance", a.id, "update", 2)


def _apply_require_next(ctx: ApplyContext, op: AttendanceRequireNext) -> None:
    count = ctx.db.query(MeetppAttendee).filter_by(session_id=ctx.session.id, required_next=True).count()
    if count >= MAX_ATTENDEES:
        _reject(ctx, op.op, "required-next limit reached")
        return
    a = _find_attendee(ctx, op.person)
    if a is None:
        if not op.person.strip():
            _reject(ctx, op.op, "empty person")
            return
        a = MeetppAttendee(
            id=_ulid(),
            session_id=ctx.session.id,
            person_key=_norm_person_key(op.person),
            display_name=op.person.strip()[:200],
            presence="absent",
        )
        ctx.db.add(a)
    a.required_next = True
    a.required_reason = op.reason[:300] or a.required_reason
    ctx.db.flush()
    _record(ctx, op, "attendance", a.id, "update", 2)


def _apply_phase_signal(ctx: ApplyContext, op: PhaseSignal) -> None:
    ctx.phase_signal = {"to": op.to, "confidence": op.confidence, "reason": op.reason}


def _apply_next_meeting(ctx: ApplyContext, op: NextMeetingPropose) -> None:
    ctx.next_meeting = {
        "date_text": op.date_text,
        "iso": op.iso,
        "duration_min": op.duration_min,
    }


def _suggest(ctx: ApplyContext, op: AnyOp, kind: str, obj_id: str) -> None:
    ctx.applied.append(AppliedOp(op.op, kind, obj_id, "update", "suggested", "item locked", 0))
    ctx.db.add(
        MeetppOp(
            session_id=ctx.session.id,
            version=ctx.ver(),
            op_type=op.op,
            payload_json=json.dumps(op.model_dump(), ensure_ascii=False),
            actor=ctx.actor,
            status="suggested",
            reason="item locked",
        )
    )


def _find_attendee(ctx: ApplyContext, person: str) -> MeetppAttendee | None:
    if not person:
        return None
    low = person.strip().strip('"\u201c\u201d ').lower()
    for a in ctx.db.query(MeetppAttendee).filter_by(session_id=ctx.session.id).all():
        if a.display_name.lower() == low or a.display_name.lower().startswith(low[:20]):
            return a
        if low in a.display_name.lower():
            return a
    return None


def _norm_person_key(name: str) -> str:
    from app.meetpp.util import person_key

    return person_key(None, name)


def _ulid() -> str:
    from ulid import ULID

    return str(ULID())


def _series(ctx: ApplyContext):
    from app.meetpp.models import MeetppSeries

    return ctx.db.get(MeetppSeries, ctx.session.series_id)


# ─── Serialisation ──────────────────────────────────────────────────────────


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def agenda_dict(i: MeetppAgendaItem) -> dict:
    return {
        "id": i.id,
        "position": i.position,
        "title": i.title,
        "presenter": i.presenter,
        "timebox": i.timebox_minutes,
        "outcome": i.desired_outcome,
        "status": i.status,
        "source": i.source,
        "started_at": _iso(i.started_at),
        "locked": i.locked,
    }


def decision_dict(d: MeetppDecision, previous: bool = False) -> dict:
    return {
        "id": d.id,
        "ref": d.ref,
        "text": d.text,
        "rationale": d.rationale,
        "item_id": d.agenda_item_id,
        "status": d.status,
        "origin": d.origin,
        "locked": d.locked,
        "evidence": json.loads(d.evidence_json or "[]"),
        "previous": previous,
    }


def action_dict(a: MeetppAction) -> dict:
    return {
        "id": a.id,
        "ref": a.ref,
        "session_id": a.session_id,
        "title": a.title,
        "owner": a.owner_name,
        "due": a.due_date,
        "item_id": a.agenda_item_id,
        "status": a.status,
        "review_status": a.review_status,
        "origin": a.origin,
        "locked": a.locked,
        "note": a.note,
        "evidence": json.loads(a.evidence_json or "[]"),
    }


def attendee_dict(a: MeetppAttendee) -> dict:
    return {
        "id": a.id,
        "person_key": a.person_key,
        "identities": json.loads(a.identities_json or "[]"),
        "name": a.display_name,
        "email": a.email,
        "role": a.role,
        "presence": a.presence,
        "talk_seconds": a.talk_seconds,
        "opted_out": a.opted_out,
        "required_now": a.required_now,
        "required_next": a.required_next,
        "required_reason": a.required_reason,
    }


def minute_dict(m: MeetppMinute) -> dict:
    return {
        "id": m.id,
        "item_id": m.agenda_item_id,
        "body_md": m.body_md,
        "version": m.version,
        "status": m.status,
        "locked": m.locked,
    }


def attachment_dict(a: MeetppAttachment) -> dict:
    return {
        "id": a.id,
        "item_id": a.agenda_item_id,
        "kind": a.kind,
        "filename": a.filename,
        "caption": a.caption,
        "author": a.author,
        "url": f"/api/v1/meetpp/sessions/{a.session_id}/attachments/{a.id}",
    }


def session_meta(s: MeetppSession) -> dict:
    try:
        settings = json.loads(s.settings_json or "{}")
    except ValueError:
        settings = {}
    try:
        editors = json.loads(s.editors_json or "[]")
    except ValueError:
        editors = []
    return {
        "editors": editors,
        "id": s.id,
        "status": s.status,
        "template": s.template,
        "mode": s.mode,
        "language": s.language,
        "goal": s.goal,
        "phase": s.phase,
        "phase_index": s.phase_index,
        "current_item_id": s.current_item_id,
        "version": s.state_version,
        "settings": settings,
        "started_at": _iso(s.started_at),
        "ended_at": _iso(s.ended_at),
    }


def build_state(db: Session, s: MeetppSession) -> dict:
    items = db.query(MeetppAgendaItem).filter_by(session_id=s.id).order_by(MeetppAgendaItem.position).all()
    decisions = (
        db.query(MeetppDecision).filter_by(session_id=s.id).order_by(MeetppDecision.created_at).all()
    )
    actions = (
        db.query(MeetppAction)
        .filter(
            MeetppAction.series_id == s.series_id,
            (MeetppAction.session_id == s.id)
            | (MeetppAction.status.notin_(("done", "dropped"))),
        )
        .order_by(MeetppAction.created_at)
        .all()
    )
    attendees = (
        db.query(MeetppAttendee).filter_by(session_id=s.id).order_by(MeetppAttendee.display_name).all()
    )
    minutes = db.query(MeetppMinute).filter_by(session_id=s.id).all()
    attachments = (
        db.query(MeetppAttachment).filter_by(session_id=s.id).order_by(MeetppAttachment.created_at).all()
    )
    # Previous session's decisions as a collapsed reference group.
    prev = (
        db.query(MeetppDecision)
        .filter(MeetppDecision.series_id == s.series_id, MeetppDecision.session_id != s.id)
        .order_by(MeetppDecision.created_at.desc())
        .limit(20)
        .all()
    )
    return {
        "session": session_meta(s),
        "agenda": [agenda_dict(i) for i in items],
        "decisions": [decision_dict(d) for d in decisions],
        "previous_decisions": [decision_dict(d, previous=True) for d in reversed(prev)],
        "actions": [action_dict(a) for a in actions],
        "attendance": [attendee_dict(a) for a in attendees],
        "minutes": [minute_dict(m) for m in minutes],
        "attachments": [attachment_dict(a) for a in attachments],
    }


# Delta buckets use the plural collection names the client merges (matching
# Appendix B: {"delta":{"actions":[…]}}). `changes[].kind` stays singular.
_DELTA_KEY = {
    "agenda": "agenda",
    "decision": "decisions",
    "action": "actions",
    "attendance": "attendance",
    "minutes": "minutes",
    "attachment": "attachments",
}


def delta_from_applied(ctx: ApplyContext) -> dict:
    """Build a compact delta for the applied operations (bounded ≤ 8 KB)."""
    delta: dict[str, list] = {}
    changes = []
    for ap in ctx.applied:
        if ap.action == "remove":
            changes.append({"kind": ap.kind, "id": ap.id, "op": "remove"})
            # Removal still needs the plural bucket so the client can drop it.
            key = _DELTA_KEY.get(ap.kind)
            if key:
                delta.setdefault(key, [])
            continue
        changes.append({"kind": ap.kind, "id": ap.id, "op": ap.action})
        if ap.id is None:
            continue
        obj = _load(ctx.db, ap.kind, ap.id)
        if obj is None:
            continue
        bucket = {
            "agenda": agenda_dict,
            "decision": decision_dict,
            "action": action_dict,
            "attendance": attendee_dict,
            "minutes": minute_dict,
            "attachment": attachment_dict,
        }.get(ap.kind)
        key = _DELTA_KEY.get(ap.kind)
        if bucket and key:
            delta.setdefault(key, []).append(bucket(obj))
    return {"changes": changes, "delta": delta}


def _load(db: Session, kind: str, obj_id: str):
    model = {
        "agenda": MeetppAgendaItem,
        "decision": MeetppDecision,
        "action": MeetppAction,
        "attendance": MeetppAttendee,
        "minutes": MeetppMinute,
        "attachment": MeetppAttachment,
    }.get(kind)
    return db.get(model, obj_id) if model else None


_FOCUS_PRIO = {"agenda": 6, "decision": 5, "action": 4, "attendance": 3, "minutes": 2}


def focus_for(ctx: ApplyContext) -> dict | None:
    """Highest-priority focus hint among the applied operations."""
    best: AppliedOp | None = None
    for ap in ctx.applied:
        if ap.action == "remove" or ap.id is None:
            continue
        if best is None or ap.focus_prio > best.focus_prio:
            best = ap
    if best is None:
        return None
    tab = {
        "agenda": "agenda",
        "decision": "decisions",
        "action": "actions",
        "attendance": "attendance",
        "minutes": "minutes",
    }.get(best.kind)
    if not tab:
        return None
    return {"tab": tab, "id": best.id, "prio": best.focus_prio or _FOCUS_PRIO.get(best.kind, 1)}

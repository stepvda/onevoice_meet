"""Operations, validation and state serialisation (contract §3, §3.1, §5).

The LLM only proposes; this module validates and applies. Validation is
lenient on shape (unknown fields ignored, long text truncated, unknown
sections fall back to the topic and then the live section) and strict on
meaning (evidence must cite the window, duplicates are refused or merged,
locked items only receive suggestions). Every rejection is stored in
meetpp_ops with its raw payload and reason.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.meetpp import agreement, governance, outline as outline_mod, util
from app.meetpp.models import (
    MeetppAction,
    MeetppActionReport,
    MeetppAttachment,
    MeetppAttendee,
    MeetppBallot,
    MeetppDecision,
    MeetppDocument,
    MeetppMinute,
    MeetppOp,
    MeetppRoster,
    MeetppSection,
    MeetppSegment,
    MeetppSeries,
    MeetppSession,
    MeetppVote,
)
from app.models import Meeting

log = logging.getLogger("app.meetpp")

MAX_AI_OPS_PER_TICK = 20
MAX_NOTES_PER_TICK = 6
MAX_EVIDENCE = 10
DUPLICATE_JACCARD = 0.85
PENDING_MERGE_JACCARD = 0.6
NOTE_DUPLICATE_JACCARD = 0.85

DECISION_STATUSES = ("pending", "proposed", "adopted", "rejected", "withdrawn")
ACTION_STATUSES = ("proposed", "open", "in_progress", "done", "cancelled")
OPEN_ACTION_STATUSES = ("proposed", "open", "in_progress")
ATTENDANCE_STATUSES = ("present", "represented", "absent", "excused", "not_registered")
PRIO = {"topic": 3, "decision": 5, "action": 4, "attendance": 2}
TAB = {"topic": "agenda", "decision": "decisions", "action": "actions", "attendance": "attendance"}
FINAL_PART_KINDS = ("opening", "adjournment", "voting_record", "provenance")


# ─── Lenient field types ───────────────────────────────────────────────────


def _text(limit: int):
    def conv(v):
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            v = "; ".join(str(x) for x in v if x is not None)
        elif isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)
        return util.truncate(v, limit)

    return BeforeValidator(conv)


def _int(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _evidence(v):
    if v is None:
        return []
    if not isinstance(v, (list, tuple)):
        v = [v]
    out: list[int] = []
    for x in v:
        if isinstance(x, str):
            out.extend(int(n) for n in re.findall(r"\d+", x))
        else:
            n = _int(x)
            if n is not None:
                out.append(n)
    seen: list[int] = []
    for n in out:
        if n not in seen:
            seen.append(n)
    return seen


def _names(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = [p for p in re.split(r"\s*(?:,|;| and )\s*", v) if p.strip()]
    out = []
    for x in v if isinstance(v, (list, tuple)) else [v]:
        if isinstance(x, dict):
            x = x.get("name") or x.get("person")
        name = util.truncate(x, 200)
        if name:
            out.append(name)
    return out[:10]


def _dict(v):
    return v if isinstance(v, dict) else None


Text200 = Annotated[str | None, _text(200)]
Text300 = Annotated[str | None, _text(300)]
Text600 = Annotated[str | None, _text(600)]
Text1200 = Annotated[str | None, _text(1200)]
Text4000 = Annotated[str | None, _text(4000)]
Text40k = Annotated[str | None, _text(40000)]
Int = Annotated[int | None, BeforeValidator(_int)]
Conf = Annotated[float, BeforeValidator(_float)]
Evidence = Annotated[list[int], BeforeValidator(_evidence)]
Names = Annotated[list[str] | None, BeforeValidator(_names)]
Obj = Annotated[dict | None, BeforeValidator(_dict)]
Ref = Annotated[str | None, _text(40)]


class _Op(BaseModel):
    model_config = ConfigDict(extra="ignore")
    op: str


class DecisionAdd(_Op):
    section: Ref = None
    section_id: Ref = None
    title: Text300 = None
    resolution: Text1200 = None
    how_taken: Text600 = None
    status: Text200 = None
    decided_at_seq: Int = None
    vote: Obj = None
    evidence: Evidence = []


class DecisionUpdate(_Op):
    ref: Ref = None
    id: Ref = None
    title: Text300 = None
    resolution: Text1200 = None
    how_taken: Text600 = None
    status: Text200 = None
    section: Ref = None
    section_id: Ref = None
    decided_at_seq: Int = None
    vote: Obj = None
    evidence: Evidence = []


class ActionAdd(_Op):
    section: Ref = None
    section_id: Ref = None
    title: Text300 = None
    description: Text4000 = None
    assignees: Names = None
    due: Text200 = None
    from_decision: Ref = None
    status: Text200 = None
    evidence: Evidence = []


class ActionUpdate(_Op):
    ref: Ref = None
    id: Ref = None
    title: Text300 = None
    description: Text4000 = None
    status: Text200 = None
    report_note: Text600 = None
    progress_note: Text600 = None
    completion_note: Text600 = None
    due: Text200 = None
    assignees: Names = None
    evidence: Evidence = []


class AttendanceSet(_Op):
    person: Text200 = None
    status: Text200 = None
    represented_by: Text200 = None
    mandate_ref: Text300 = None
    evidence: Evidence = []


class AttendanceRequireNext(_Op):
    person: Text200 = None
    reason: Text300 = None
    evidence: Evidence = []


class SectionAdd(_Op):
    kind: Text200 = None
    parent: Ref = None
    parent_id: Ref = None
    title: Text300 = None


class NextMeetingPropose(_Op):
    when_text: Text300 = None
    iso: Text200 = None
    evidence: Evidence = []


class IdOp(_Op):
    id: Ref = None


class MinutesEdit(_Op):
    section_id: Ref = None
    kind: Text200 = None
    narrative_md: Text40k = None


class SectionUpdate(_Op):
    id: Ref = None
    title: Text300 = None
    body: Text4000 = None
    presenter: Text200 = None
    timebox_minutes: Int = None


class SectionStatus(_Op):
    id: Ref = None
    status: Text200 = None


class Note(BaseModel):
    model_config = ConfigDict(extra="ignore")
    section: Ref = None
    text: Text300 = None
    evidence: Evidence = []


AI_OPS: dict[str, type[_Op]] = {
    "decision.add": DecisionAdd,
    "decision.update": DecisionUpdate,
    "action.add": ActionAdd,
    "action.update": ActionUpdate,
    "attendance.set": AttendanceSet,
    "attendance.require_next": AttendanceRequireNext,
    "section.add": SectionAdd,
    "next_meeting.propose": NextMeetingPropose,
}
HUMAN_OPS: dict[str, type[_Op]] = {
    **AI_OPS,
    "decision.confirm": IdOp,
    "decision.reject": IdOp,
    "action.confirm": IdOp,
    "action.reject": IdOp,
    "minutes.edit": MinutesEdit,
    "section.update": SectionUpdate,
    "section.status": SectionStatus,
}


def parse_op(raw: Any, vocabulary: dict[str, type[_Op]]) -> tuple[_Op | None, str | None]:
    if not isinstance(raw, dict):
        return None, "not an object"
    name = str(raw.get("op") or raw.get("type") or "").strip()
    model = vocabulary.get(name)
    if model is None:
        return None, f"unknown op {name!r}"[:120]
    try:
        return model.model_validate({**raw, "op": name}), None
    except ValidationError as exc:
        return None, f"invalid: {exc.errors()[0].get('msg', 'schema')}"[:200]


# ─── Change tracking ────────────────────────────────────────────────────────


@dataclass
class Changes:
    session: bool = False
    sections: set[str] = field(default_factory=set)
    decisions: set[str] = field(default_factory=set)
    actions: set[str] = field(default_factory=set)
    minutes: set[str] = field(default_factory=set)
    attendees: set[str] = field(default_factory=set)
    attachments: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)
    quorum: bool = False
    removed: list[dict] = field(default_factory=list)
    activations: list[dict] = field(default_factory=list)

    def any(self) -> bool:
        return bool(
            self.session or self.sections or self.decisions or self.actions or self.minutes
            or self.attendees or self.attachments or self.documents or self.quorum or self.removed
        )

    def merge(self, other: "Changes") -> "Changes":
        self.session = self.session or other.session
        self.quorum = self.quorum or other.quorum
        for name in ("sections", "decisions", "actions", "minutes", "attendees", "attachments", "documents"):
            getattr(self, name).update(getattr(other, name))
        self.removed.extend(other.removed)
        self.activations.extend(other.activations)
        return self

    def activate(self, kind: str, section_id: str | None, item_id: str | None = None) -> None:
        act = {"kind": kind, "tab": TAB[kind], "section_id": section_id, "prio": PRIO[kind]}
        if item_id:
            act["item_id"] = item_id
        for a in self.activations:
            if a == act:
                return
        self.activations.append(act)


# ─── Apply context ──────────────────────────────────────────────────────────


@dataclass
class ApplyContext:
    db: Session
    session: MeetppSession
    actor: str = "ai"
    window: set[int] = field(default_factory=set)
    context: set[int] = field(default_factory=set)
    topic_section_id: str | None = None
    changes: Changes = field(default_factory=Changes)
    applied: int = 0
    suggested: int = 0
    rejected: list[dict] = field(default_factory=list)
    compose: list[str] = field(default_factory=list)
    # "S5" → section id as shown in the prompt (AI ticks).
    pins: dict[str, str] | None = None
    _outline: outline_mod.Outline | None = None
    _series: MeetppSeries | None = None

    @property
    def human(self) -> bool:
        return self.actor != "ai"

    @property
    def outline(self) -> outline_mod.Outline:
        if self._outline is None:
            self._outline = outline_mod.load(self.db, self.session.id)
            self._outline.pins = self.pins
        return self._outline

    def reload_outline(self) -> None:
        self._outline = None

    @property
    def series(self) -> MeetppSeries:
        if self._series is None:
            self._series = self.db.get(MeetppSeries, self.session.series_id)
        return self._series

    @property
    def version(self) -> int:
        return int(self.session.state_version or 0) + 1


def _log(ctx: ApplyContext, op_type: str, raw: Any, status: str, reason: str | None = None, evidence: list[int] | None = None) -> None:
    try:
        payload = util.dumps(raw)[:20000]
    except (TypeError, ValueError):
        payload = str(raw)[:20000]
    ctx.db.add(
        MeetppOp(
            session_id=ctx.session.id,
            version=ctx.version,
            op_type=str(op_type or "?")[:40],
            payload_json=payload,
            actor=ctx.actor[:200],
            status=status,
            reason=(reason or None) and reason[:300],
            evidence_json=util.dumps(evidence or []),
        )
    )


class Reject(Exception):
    pass


class Suggest(Exception):
    pass


def _check_evidence(ctx: ApplyContext, evidence: list[int], required: bool = True) -> list[int]:
    if ctx.human:
        return [e for e in evidence][:MAX_EVIDENCE]
    known = ctx.window | ctx.context
    ev = [e for e in evidence if e in known][:MAX_EVIDENCE]
    if required and not any(e in ctx.window for e in ev):
        raise Reject("evidence must cite at least one new transcript line" if evidence else "evidence missing")
    return ev


def _section(ctx: ApplyContext, ref: str | None, section_id: str | None = None) -> MeetppSection | None:
    o = ctx.outline
    s = o.resolve(section_id) or o.resolve(ref)
    if s is None and ctx.topic_section_id:
        s = o.by_id.get(ctx.topic_section_id)
    if s is None and ctx.session.topic_section_id:
        s = o.by_id.get(ctx.session.topic_section_id)
    if s is None and ctx.session.live_section_id:
        s = o.by_id.get(ctx.session.live_section_id)
    return s


def _seg_time(ctx: ApplyContext, seq: int | None):
    if seq is None:
        return None
    seg = ctx.db.query(MeetppSegment).filter_by(session_id=ctx.session.id, seq=seq).first()
    if seg is None:
        return None
    return util.aware(seg.t_end or seg.t_start or seg.created_at)


def _norm_status(value: str | None, allowed: tuple[str, ...], default: str | None) -> str | None:
    if not value:
        return default
    v = value.strip().lower().replace(" ", "_").replace("-", "_")
    v = {
        "agreed": "adopted", "approved": "adopted", "carried": "adopted", "passed": "adopted", "accepted": "adopted",
        "declined": "rejected", "defeated": "rejected", "lost": "rejected",
        "completed": "done", "closed": "done", "finished": "done", "inprogress": "in_progress", "ongoing": "in_progress",
        "started": "in_progress", "dropped": "cancelled", "canceled": "cancelled",
        "apologies": "excused", "apology": "excused", "proxy": "represented",
    }.get(v, v)
    return v if v in allowed else default


def _due(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def _assignees(ctx: ApplyContext, names: list[str] | None) -> list[dict]:
    out = []
    for n in names or []:
        display, key = governance.match_person(ctx.db, ctx.session, n)
        if display and all(a["name"] != display for a in out):
            out.append({"name": display, "person_key": key})
    return out


def _decision_by_ref(ctx: ApplyContext, ref: str | None, id_: str | None) -> MeetppDecision | None:
    d = None
    if id_:
        d = ctx.db.get(MeetppDecision, id_)
    if d is None and ref:
        r = ref.strip().upper().replace(" ", "")
        m = re.fullmatch(r"D-?0*(\d+)", r)
        q = ctx.db.query(MeetppDecision).filter_by(series_id=ctx.session.series_id)
        d = q.filter_by(ref=f"D-{int(m.group(1))}").first() if m else q.filter_by(ref=ref.strip()).first()
    if d is not None and d.session_id != ctx.session.id:
        return None
    return d


def _action_by_ref(ctx: ApplyContext, ref: str | None, id_: str | None) -> MeetppAction | None:
    a = None
    if id_:
        a = ctx.db.get(MeetppAction, id_)
    if a is None and ref:
        r = ref.strip().upper().replace(" ", "")
        m = re.fullmatch(r"A-?0*(\d+)", r)
        q = ctx.db.query(MeetppAction).filter_by(series_id=ctx.session.series_id)
        a = q.filter_by(ref=f"A-{int(m.group(1))}").first() if m else q.filter_by(ref=ref.strip()).first()
    if a is not None and a.series_id != ctx.session.series_id:
        return None
    return a


def _decision_text(d) -> str:
    return f"{d.title or ''} {d.resolution or ''}"


def _similar(a_title: str, a_full: str, b_title: str, b_full: str) -> float:
    return max(util.jaccard(a_title, b_title), util.jaccard(a_full, b_full))


def is_previous_action(a: MeetppAction, session: MeetppSession) -> bool:
    return a.session_id != session.id or a.origin == "pdf"


# ─── Appliers ───────────────────────────────────────────────────────────────


def _decision_fields(ctx: ApplyContext, d: MeetppDecision, op, *, evidence: list[int]) -> bool:
    """Apply title/resolution/how_taken/status/vote. Returns True if the
    decision became adopted or rejected (activation)."""
    before = d.status
    if op.title:
        d.title = op.title
    if op.resolution:
        d.resolution = op.resolution
    if op.how_taken:
        d.how_taken = op.how_taken
    status = _norm_status(op.status, DECISION_STATUSES, None)
    if status == "pending" and not ctx.human:
        status = None
    # The AI adopts only on agreement heard in the lines it cites; an opinion
    # or an item being introduced stays proposed, and its vote is not taken.
    unheard = (
        not ctx.human and before != "adopted"
        and (status == "adopted" or (status is None and op.vote))
        and not _agreement_cited(ctx, evidence, f"{d.title or ''} {d.resolution or ''} {op.title or ''} {op.resolution or ''}")
    )
    if unheard:
        log.info("meetpp: %s not adopted: no agreement in the cited lines %s", d.ref or "decision", evidence)
        status = "proposed" if before in ("pending", "proposed") else None
    if status:
        d.status = status
    if getattr(op, "decided_at_seq", None) is not None:
        d.decided_seq = op.decided_at_seq
        d.decided_at = _seg_time(ctx, op.decided_at_seq) or d.decided_at
    if d.status in ("adopted", "rejected") and d.decided_at is None:
        seq = max(evidence) if evidence else None
        d.decided_seq = d.decided_seq or seq
        d.decided_at = _seg_time(ctx, seq) or util.now()
    if evidence:
        merged = util.loads(d.evidence_json, [])
        for e in evidence:
            if e not in merged:
                merged.append(e)
        d.evidence_json = util.dumps(merged[-30:])
    if op.vote and not unheard:
        vote_data = dict(op.vote)
        governance.apply_vote(ctx.db, ctx.session, d, vote_data, confirmed=True if ctx.human else False, ai=not ctx.human)
        vote = ctx.db.query(MeetppVote).filter_by(decision_id=d.id).first()
        if vote is not None and vote.result and not status:
            d.status = vote.result
    elif (
        not ctx.human and d.status == "adopted" and before != "adopted" and governance.is_formal(ctx.series)
        and ctx.db.query(MeetppVote).filter_by(decision_id=d.id).first() is None
    ):
        # Adopted with no count heard: recorded as taken with the assent of the
        # voting members present (the chair can correct it in review).
        governance.apply_vote(ctx.db, ctx.session, d, {"method": "assent"}, confirmed=False, ai=True)
    if ctx.human:
        d.locked = True
    return d.status != before and d.status in ("adopted", "rejected")


# Lines before a cited line that also tell what the exchange is about (the
# vote "Will you both be in favour? Yes. Approved." follows the proposal).
ABOUT_LEAD_LINES = 3


def _agreement_cited(ctx: ApplyContext, evidence: list[int], subject: str) -> bool:
    if not evidence:
        return False
    around = {e - k for e in evidence for k in range(ABOUT_LEAD_LINES + 1)}
    rows = (
        ctx.db.query(MeetppSegment.seq, MeetppSegment.identity, MeetppSegment.text, MeetppSegment.text_refined)
        .filter(MeetppSegment.session_id == ctx.session.id, MeetppSegment.seq.in_(around), MeetppSegment.is_gap.is_(False))
        .order_by(MeetppSegment.seq)
        .all()
    )
    cited = set(evidence)
    lines = [(r.identity, r.text_refined or r.text or "") for r in rows if r.seq in cited]
    return agreement.any_agreement(lines) and agreement.about(subject, (r.text_refined or r.text or "" for r in rows))


def _new_ref(ctx: ApplyContext, kind: str) -> str:
    series = ctx.series
    if kind == "D":
        series.decision_counter = int(series.decision_counter or 0) + 1
        return f"D-{series.decision_counter}"
    series.action_counter = int(series.action_counter or 0) + 1
    return f"A-{series.action_counter}"


def _apply_decision_add(ctx: ApplyContext, op: DecisionAdd) -> str:
    title = op.title or (util.truncate(re.sub(r"^that\s+", "", op.resolution or "", flags=re.I), 120) if op.resolution else None)
    if not title:
        raise Reject("title missing")
    evidence = _check_evidence(ctx, op.evidence)
    section = _section(ctx, op.section, op.section_id)
    o = ctx.outline
    existing = ctx.db.query(MeetppDecision).filter_by(session_id=ctx.session.id).all()
    full = f"{title} {op.resolution or ''}"
    top_id = o.top(section).id if section else None
    # A decision that was prefilled as "to take" is updated, not duplicated.
    best, best_score = None, 0.0
    for d in existing:
        if d.status != "pending":
            continue
        ds = o.by_id.get(d.section_id or "")
        if top_id and ds is not None and o.top(ds).id != top_id:
            continue
        score = _similar(title, full, d.title, _decision_text(d))
        if score > best_score:
            best, best_score = d, score
    target = best if best is not None and best_score >= PENDING_MERGE_JACCARD else None
    if target is None:
        for d in existing:
            if d.status == "pending":
                continue
            if _similar(title, full, d.title, _decision_text(d)) >= DUPLICATE_JACCARD:
                new_status = _norm_status(op.status, DECISION_STATUSES, None)
                if new_status and new_status != d.status and not d.locked:
                    target = d
                    break
                raise Reject(f"duplicate of {d.ref}")
    if target is not None:
        if target.locked and not ctx.human:
            raise Suggest(f"{target.ref} is locked")
        if not op.status:
            op.status = "adopted" if ctx.human else "proposed"
        took = _decision_fields(ctx, target, op, evidence=evidence)
        ctx.changes.decisions.add(target.id)
        ctx.changes.sections.add(target.section_id or "")
        if took or target.status in ("adopted", "rejected"):
            ctx.changes.activate("decision", target.section_id, target.id)
        ctx.changes.quorum = True
        return f"merged into {target.ref}"
    d = MeetppDecision(
        id=util.ulid(),
        session_id=ctx.session.id,
        series_id=ctx.session.series_id,
        ref=_new_ref(ctx, "D"),
        section_id=section.id if section else None,
        title=title,
        status="proposed",
        origin="user" if ctx.human else "ai",
        confirmed=ctx.human,
        evidence_json="[]",
    )
    ctx.db.add(d)
    ctx.db.flush()
    if not op.status:
        op.status = "adopted" if ctx.human else "proposed"
    _decision_fields(ctx, d, op, evidence=evidence)
    ctx.changes.decisions.add(d.id)
    if d.section_id:
        ctx.changes.sections.add(d.section_id)
    ctx.changes.activate("decision", d.section_id, d.id)
    return d.ref


def _apply_decision_update(ctx: ApplyContext, op: DecisionUpdate) -> str:
    d = _decision_by_ref(ctx, op.ref, op.id)
    if d is None:
        raise Reject("decision not found")
    evidence = _check_evidence(ctx, op.evidence)
    if d.locked and not ctx.human:
        raise Suggest(f"{d.ref} is locked")
    if ctx.human and (op.section_id or op.section):
        s = ctx.outline.resolve(op.section_id) or ctx.outline.resolve(op.section)
        if s is not None and s.id != d.section_id:
            ctx.changes.sections.update({d.section_id or "", s.id})
            d.section_id = s.id
    took = _decision_fields(ctx, d, op, evidence=evidence)
    ctx.changes.decisions.add(d.id)
    if took:
        ctx.changes.activate("decision", d.section_id, d.id)
    return d.ref


def _apply_action_add(ctx: ApplyContext, op: ActionAdd) -> str:
    if not op.title:
        raise Reject("title missing")
    evidence = _check_evidence(ctx, op.evidence)
    section = _section(ctx, op.section, op.section_id)
    series_actions = ctx.db.query(MeetppAction).filter_by(series_id=ctx.session.series_id).all()
    for a in series_actions:
        if util.jaccard(a.title, op.title) < DUPLICATE_JACCARD:
            continue
        if a.session_id == ctx.session.id and not is_previous_action(a, ctx.session):
            raise Reject(f"duplicate of {a.ref}")
        if a.status in OPEN_ACTION_STATUSES:
            raise Reject(f"duplicate of previous action {a.ref} (use action.update)")
    decision = _decision_by_ref(ctx, op.from_decision, None) if op.from_decision else None
    status = _norm_status(op.status, ACTION_STATUSES, None) if ctx.human else None
    a = MeetppAction(
        id=util.ulid(),
        series_id=ctx.session.series_id,
        session_id=ctx.session.id,
        ref=_new_ref(ctx, "A"),
        section_id=section.id if section else None,
        title=op.title,
        description=op.description,
        assignees_json=util.dumps(_assignees(ctx, op.assignees)),
        due_date=_due(op.due),
        status=status or ("open" if ctx.human else "proposed"),
        decision_id=decision.id if decision else None,
        origin="user" if ctx.human else "ai",
        locked=ctx.human,
        evidence_json=util.dumps(evidence),
    )
    ctx.db.add(a)
    ctx.db.flush()
    ctx.changes.actions.add(a.id)
    if a.section_id:
        ctx.changes.sections.add(a.section_id)
    ctx.changes.activate("action", a.section_id, a.id)
    return a.ref


def _report_for(ctx: ApplyContext, a: MeetppAction) -> MeetppActionReport:
    r = ctx.db.query(MeetppActionReport).filter_by(action_id=a.id, session_id=ctx.session.id).first()
    if r is None:
        r = MeetppActionReport(action_id=a.id, session_id=ctx.session.id, note="", evidence_json="[]")
        ctx.db.add(r)
    return r


def _append_text(existing: str | None, new: str, sep: str = " ") -> str:
    if not existing:
        return new
    if util.jaccard(existing, new) >= NOTE_DUPLICATE_JACCARD or new in existing:
        return existing
    return f"{existing}{sep}{new}"


def _action_update_is_noop(ctx: ApplyContext, a: MeetppAction, op: ActionUpdate, status: str | None) -> bool:
    """The model often repeats an update it already made; re-applying it would
    only flash the same item on the board again."""
    if status and status != a.status:
        return False
    if op.due and _due(op.due) and _due(op.due) != a.due_date:
        return False
    if op.assignees:
        return False
    report = ctx.db.query(MeetppActionReport).filter_by(action_id=a.id, session_id=ctx.session.id).first()
    for text, existing in (
        (op.report_note, report.note if report else None),
        (op.completion_note, a.completion_note),
        (op.progress_note, a.progress_notes),
    ):
        if text and _append_text(existing, text) != (existing or ""):
            return False
    return True


def _apply_action_update(ctx: ApplyContext, op: ActionUpdate) -> str:
    a = _action_by_ref(ctx, op.ref, op.id)
    if a is None:
        raise Reject("action not found")
    evidence = _check_evidence(ctx, op.evidence)
    if a.locked and not ctx.human:
        raise Suggest(f"{a.ref} is locked")
    status = _norm_status(op.status, ACTION_STATUSES, None)
    if not ctx.human and _action_update_is_noop(ctx, a, op, status):
        raise Reject("no change")
    if status and status != a.status:
        a.status = status
        if status == "done":
            a.completed_at = a.completed_at or util.now()
        elif status in OPEN_ACTION_STATUSES:
            a.completed_at = None
    if op.title and ctx.human:
        a.title = op.title
    if op.description and ctx.human:
        a.description = op.description
    if op.due:
        due = _due(op.due)
        if due:
            a.due_date = due
    if op.assignees is not None and op.assignees:
        a.assignees_json = util.dumps(_assignees(ctx, op.assignees))
    if op.completion_note:
        a.completion_note = op.completion_note if ctx.human else _append_text(a.completion_note, op.completion_note)
    if op.progress_note:
        stamp = util.now().date().isoformat()
        a.progress_notes = _append_text(a.progress_notes, f"{stamp}: {op.progress_note}", "\n")
    report = None
    if op.report_note or status or op.completion_note or op.progress_note:
        report = _report_for(ctx, a)
        if op.report_note:
            report.note = op.report_note if ctx.human else _append_text(report.note, op.report_note)
        report.status_at_report = a.status
        if evidence:
            ev = util.loads(report.evidence_json, [])
            report.evidence_json = util.dumps((ev + [e for e in evidence if e not in ev])[-30:])
    if evidence:
        ev = util.loads(a.evidence_json, [])
        a.evidence_json = util.dumps((ev + [e for e in evidence if e not in ev])[-30:])
    if ctx.human:
        a.locked = True
    ctx.changes.actions.add(a.id)
    ctx.changes.activate("action", action_section_id(ctx.db, ctx.session, a, ctx.outline), a.id)
    return a.ref


def _attendee_for(ctx: ApplyContext, person: str, create_status: str) -> MeetppAttendee | None:
    display, key = governance.match_person(ctx.db, ctx.session, person)
    if not display:
        return None
    key = key or util.name_key(display)
    a = ctx.db.query(MeetppAttendee).filter_by(session_id=ctx.session.id, person_key=key).first()
    if a is None:
        roster = ctx.db.query(MeetppRoster).filter_by(series_id=ctx.session.series_id, person_key=key).first()
        a = MeetppAttendee(
            id=util.ulid(),
            session_id=ctx.session.id,
            person_key=key,
            roster_id=roster.id if roster else None,
            display_name=roster.display_name if roster else display,
            username=roster.username if roster else None,
            email=roster.email if roster else None,
            status=create_status,
            voting=bool(roster.voting) if roster else False,
            identities_json="[]",
        )
        ctx.db.add(a)
        ctx.db.flush()
    return a


def _apply_attendance_set(ctx: ApplyContext, op: AttendanceSet) -> str:
    if not op.person:
        raise Reject("person missing")
    status = _norm_status(op.status, ATTENDANCE_STATUSES, None)
    if status is None:
        raise Reject("invalid attendance status")
    evidence = _check_evidence(ctx, op.evidence)
    a = _attendee_for(ctx, op.person, status)
    if a is None:
        raise Reject("person not recognised")
    if not ctx.human and a.online and status in ("absent", "excused"):
        raise Reject("person is connected")
    a.status = status
    if status == "represented":
        if op.represented_by:
            a.represented_by = op.represented_by
        if op.mandate_ref:
            a.mandate_ref = op.mandate_ref
    _ = evidence
    ctx.changes.attendees.add(a.id)
    ctx.changes.quorum = True
    ctx.changes.activate("attendance", ctx.session.live_section_id, a.id)
    return a.display_name


def _apply_require_next(ctx: ApplyContext, op: AttendanceRequireNext) -> str:
    if not op.person:
        raise Reject("person missing")
    _check_evidence(ctx, op.evidence)
    a = _attendee_for(ctx, op.person, "absent")
    if a is None:
        raise Reject("person not recognised")
    a.required_next = True
    if op.reason:
        a.required_reason = op.reason
    ctx.changes.attendees.add(a.id)
    ctx.changes.activate("attendance", ctx.session.live_section_id, a.id)
    return a.display_name


def _apply_section_add(ctx: ApplyContext, op: SectionAdd) -> str:
    if not op.title:
        raise Reject("title missing")
    kind = (op.kind or "subpoint").strip().lower()
    parent_id = None
    if kind in ("subpoint", "sub_point", "sub-point", "agenda") and (op.parent or op.parent_id):
        parent = ctx.outline.resolve(op.parent_id) or ctx.outline.resolve(op.parent)
        if parent is None:
            raise Reject("parent section not found")
        parent_id = ctx.outline.top(parent).id
    elif kind in ("subpoint", "sub_point", "sub-point"):
        if ctx.human:
            raise Reject("parent required for a sub-point")
        base = _section(ctx, None)
        if base is None:
            raise Reject("parent required for a sub-point")
        parent_id = ctx.outline.top(base).id
    try:
        s = outline_mod.add_section(
            ctx.db,
            ctx.session,
            title=op.title,
            kind="aob" if kind == "aob" else "agenda",
            parent_id=parent_id,
            source="user" if ctx.human else "ai",
        )
    except outline_mod.OutlineError as exc:
        raise Reject(str(exc)) from exc
    ctx.reload_outline()
    # Positions of other rows may have changed: send the whole outline.
    ctx.changes.sections.update(x.id for x in ctx.outline.flat)
    return s.id


def _apply_next_meeting(ctx: ApplyContext, op: NextMeetingPropose) -> str:
    _check_evidence(ctx, op.evidence)
    final = util.loads(ctx.session.final_json, {})
    final["next_meeting_proposal"] = {"when_text": op.when_text, "iso": op.iso}
    ctx.session.final_json = util.dumps(final)
    return "next meeting"


def _apply_confirm(ctx: ApplyContext, op: IdOp) -> str:
    if op.op == "decision.confirm":
        d = _decision_by_ref(ctx, op.id, op.id)
        if d is None:
            raise Reject("decision not found")
        d.confirmed = True
        d.locked = True
        if d.status == "pending":
            d.status = "proposed"
        ctx.changes.decisions.add(d.id)
        return d.ref
    a = _action_by_ref(ctx, op.id, op.id)
    if a is None:
        raise Reject("action not found")
    if a.status == "proposed":
        a.status = "open"
    a.locked = True
    ctx.changes.actions.add(a.id)
    return a.ref


def delete_decision(db: Session, d: MeetppDecision) -> None:
    vote = db.query(MeetppVote).filter_by(decision_id=d.id).first()
    if vote is not None:
        db.query(MeetppBallot).filter_by(vote_id=vote.id).delete(synchronize_session=False)
        db.delete(vote)
    db.query(MeetppAction).filter_by(decision_id=d.id).update({MeetppAction.decision_id: None}, synchronize_session=False)
    db.delete(d)


def _apply_reject(ctx: ApplyContext, op: IdOp) -> str:
    if op.op == "decision.reject":
        d = _decision_by_ref(ctx, op.id, op.id)
        if d is None:
            raise Reject("decision not found")
        ref, sid = d.ref, d.section_id
        delete_decision(ctx.db, d)
        ctx.changes.removed.append({"kind": "decision", "id": op.id})
        if sid:
            ctx.changes.sections.add(sid)
        return ref
    a = _action_by_ref(ctx, op.id, op.id)
    if a is None:
        raise Reject("action not found")
    if is_previous_action(a, ctx.session):
        raise Reject("a previous action cannot be removed; set its status to cancelled")
    ref, sid = a.ref, a.section_id
    ctx.db.query(MeetppActionReport).filter_by(action_id=a.id).delete(synchronize_session=False)
    ctx.db.delete(a)
    ctx.changes.removed.append({"kind": "action", "id": op.id})
    if sid:
        ctx.changes.sections.add(sid)
    return ref


def _apply_minutes_edit(ctx: ApplyContext, op: MinutesEdit) -> str:
    if op.narrative_md is None:
        raise Reject("narrative_md missing")
    if op.section_id:
        s = ctx.outline.resolve(op.section_id)
        if s is None:
            raise Reject("section not found")
        m = outline_mod.minute_for(ctx.db, ctx.session, ctx.outline.top(s).id)
    else:
        kind = (op.kind or "").strip()
        if kind not in FINAL_PART_KINDS:
            raise Reject("section_id or kind required")
        m = outline_mod.minute_for(ctx.db, ctx.session, None, kind=kind)
    m.narrative_md = op.narrative_md
    m.status = "edited"
    m.locked = True
    m.error = None
    m.version = int(m.version or 0) + 1
    m.composed_at = util.now()
    ctx.changes.minutes.add(m.id)
    return m.id


def _apply_section_update(ctx: ApplyContext, op: SectionUpdate) -> str:
    s = ctx.outline.resolve(op.id)
    if s is None:
        raise Reject("section not found")
    if op.title:
        s.title = op.title
    if op.body is not None:
        s.body = op.body
    if op.presenter is not None:
        s.presenter = op.presenter
    if op.timebox_minutes is not None:
        s.timebox_minutes = max(0, min(600, op.timebox_minutes)) or None
    s.locked = True
    ctx.changes.sections.add(s.id)
    return s.id


def _apply_section_status(ctx: ApplyContext, op: SectionStatus) -> str:
    s = ctx.outline.resolve(op.id)
    if s is None:
        raise Reject("section not found")
    status = (op.status or "").strip().lower()
    if status not in ("done", "deferred"):
        raise Reject("status must be done or deferred")
    if s.id == ctx.session.live_section_id:
        raise Reject("the live section is closed with Next")
    s.status = status
    s.ended_at = s.ended_at or util.now()
    ctx.changes.sections.add(s.id)
    if not s.parent_id:
        for c in ctx.outline.children.get(s.id, []):
            if c.status == "pending":
                c.status = "done"
                ctx.changes.sections.add(c.id)
        if status == "done":
            ctx.compose.append(s.id)
    return s.id


_APPLIERS = {
    "decision.add": _apply_decision_add,
    "decision.update": _apply_decision_update,
    "action.add": _apply_action_add,
    "action.update": _apply_action_update,
    "attendance.set": _apply_attendance_set,
    "attendance.require_next": _apply_require_next,
    "section.add": _apply_section_add,
    "next_meeting.propose": _apply_next_meeting,
    "decision.confirm": _apply_confirm,
    "action.confirm": _apply_confirm,
    "decision.reject": _apply_reject,
    "action.reject": _apply_reject,
    "minutes.edit": _apply_minutes_edit,
    "section.update": _apply_section_update,
    "section.status": _apply_section_status,
}


def apply_ops(ctx: ApplyContext, raw_ops: list) -> ApplyContext:
    """Validate and apply operations. Never raises; every outcome is logged."""
    vocabulary = HUMAN_OPS if ctx.human else AI_OPS
    if not isinstance(raw_ops, list):
        raw_ops = []
    limit = 200 if ctx.human else MAX_AI_OPS_PER_TICK
    for i, raw in enumerate(raw_ops):
        if i >= limit:
            _log(ctx, "?", raw, "rejected", "too many operations in one tick")
            ctx.rejected.append({"op": raw, "reason": "too many operations"})
            continue
        op, err = parse_op(raw, vocabulary)
        if op is None:
            name = raw.get("op") if isinstance(raw, dict) else "?"
            _log(ctx, str(name), raw, "rejected", err)
            ctx.rejected.append({"op": raw, "reason": err})
            continue
        try:
            detail = _APPLIERS[op.op](ctx, op)
            ctx.applied += 1
            _log(ctx, op.op, raw, "applied", detail if isinstance(detail, str) and detail.startswith("merged") else None,
                 getattr(op, "evidence", None))
        except Suggest as exc:
            ctx.suggested += 1
            _log(ctx, op.op, raw, "suggested", str(exc), getattr(op, "evidence", None))
            ctx.rejected.append({"op": raw, "reason": f"suggested: {exc}"})
        except Reject as exc:
            _log(ctx, op.op, raw, "rejected", str(exc), getattr(op, "evidence", None))
            ctx.rejected.append({"op": raw, "reason": str(exc)})
        except SQLAlchemyError as exc:
            # The unit of work is unusable: drop this batch, keep the audit row.
            log.exception("meetpp: op %s failed (rolled back)", op.op)
            ctx.db.rollback()
            ctx.changes = Changes()
            ctx.applied = 0
            ctx.reload_outline()
            ctx._series = None
            _log(ctx, op.op, raw, "rejected", f"error: {exc}"[:300])
            ctx.rejected.append({"op": raw, "reason": "database error"})
        except Exception as exc:  # noqa: BLE001 — one bad op never kills the tick
            log.exception("meetpp: op %s failed", op.op)
            ctx.reload_outline()
            _log(ctx, op.op, raw, "rejected", f"error: {exc}")
            ctx.rejected.append({"op": raw, "reason": f"error: {exc}"[:200]})
    ctx.changes.sections.discard("")
    return ctx


def apply_notes(ctx: ApplyContext, raw_notes: list) -> int:
    """Append running notes (append-only, deduplicated, evidence required)."""
    added = 0
    if not isinstance(raw_notes, list):
        return 0
    for i, raw in enumerate(raw_notes):
        if i >= MAX_NOTES_PER_TICK:
            _log(ctx, "note", raw, "rejected", "too many notes in one tick")
            continue
        if isinstance(raw, str):
            raw = {"text": raw}
        try:
            note = Note.model_validate(raw if isinstance(raw, dict) else {})
        except ValidationError:
            _log(ctx, "note", raw, "rejected", "invalid note")
            continue
        if not note.text:
            _log(ctx, "note", raw, "rejected", "empty note")
            continue
        try:
            evidence = _check_evidence(ctx, note.evidence)
        except Reject as exc:
            _log(ctx, "note", raw, "rejected", str(exc))
            continue
        section = _section(ctx, note.section)
        m = outline_mod.minute_for(ctx.db, ctx.session, section.id if section else None)
        notes = util.loads(m.notes_json, [])
        if any(util.jaccard(n.get("text", ""), note.text) >= NOTE_DUPLICATE_JACCARD for n in notes):
            _log(ctx, "note", raw, "rejected", "duplicate note")
            continue
        notes.append({"text": note.text, "evidence": evidence, "at": util.iso(util.now())})
        m.notes_json = util.dumps(notes)
        ctx.changes.minutes.add(m.id)
        added += 1
    return added


# ─── DTOs (contract §3) ─────────────────────────────────────────────────────


def _settings(s: MeetppSession) -> dict:
    data = util.loads(s.settings_json, {})
    return {
        "show_public": bool(data.get("show_public", False)),
        "in_recordings": bool(data.get("in_recordings", True)),
        "timebox_nudges": bool(data.get("timebox_nudges", True)),
        "speak": bool(data.get("speak", True)),
    }


def session_meta(db: Session, s: MeetppSession, *, series: MeetppSeries | None = None, meeting: Meeting | None = None) -> dict:
    from app.meetpp import llm

    series = series or db.get(MeetppSeries, s.series_id)
    meeting = meeting or db.get(Meeting, s.meeting_id)
    agent = util.loads(s.agent_json, {})
    proposal = util.loads(s.proposal_json, {}) if s.proposal_json else None
    jobs = util.loads(s.jobs_json, {})
    return {
        "id": s.id,
        "status": s.status,
        "template": s.template,
        "mode": s.mode,
        "language": s.language or "en",
        "goal": s.goal,
        "meeting_type": series.meeting_type if series else "informal",
        "majority_rule": series.majority_rule if series else "ordinary",
        # Series rules for the setup/review screens (clarification of §3).
        "quorum_required": series.quorum_required if series else None,
        "voting_body": governance.BODY_LABELS.get(series.meeting_type) if series and governance.is_formal(series) else None,
        "series_id": s.series_id,
        "series_title": series.title if series else None,
        "meeting_id": s.meeting_id,
        "room": meeting.room_name if meeting else None,
        "live_section_id": s.live_section_id,
        "topic_section_id": s.topic_section_id,
        "started_at": util.iso(s.started_at),
        "ended_at": util.iso(s.ended_at),
        "published_at": util.iso(s.published_at),
        "settings": _settings(s),
        "editors": util.loads(s.editors_json, []),
        "undo": outline_mod.undo_info(s),
        "proposal": (
            {k: proposal.get(k) for k in ("pid", "to", "reason", "confidence")} if proposal and proposal.get("pid") else None
        ),
        "agent": {
            "status": agent.get("status") or "offline",
            "backlog_s": agent.get("backlog_s") or 0,
            "speakers": [
                {"name": sp.get("name"), "ok": bool(sp.get("ok", True))}
                for sp in (agent.get("speakers") or [])
                if isinstance(sp, dict)
            ],
            "tier2": agent.get("tier2") or "off",
        },
        "ai": {"status": llm.ai_status(s.id)},
        "jobs": jobs or None,
    }


def section_dto(s: MeetppSection, o: outline_mod.Outline, session: MeetppSession, counts: dict) -> dict:
    return {
        "id": s.id,
        "kind": s.kind,
        "parent_id": s.parent_id,
        "position": s.position,
        "number": o.numbers.get(s.id),
        "title": s.title,
        "body": s.body,
        "presenter": s.presenter,
        "timebox_minutes": s.timebox_minutes,
        "status": s.status,
        "started_at": util.iso(s.started_at),
        "ended_at": util.iso(s.ended_at),
        "elapsed_seconds": round(float(s.elapsed_seconds or 0.0), 1),
        "source": s.source,
        "locked": bool(s.locked),
        "counts": counts.get(s.id, {"decisions": 0, "actions": 0}),
        # The section that lists the previous actions (the agenda's own
        # actions point when there is one, else "Previous actions").
        "previous_actions_home": s.id == _prev_section_id(o),
    }


def vote_dto(v: MeetppVote | None, ballots: list[MeetppBallot], session_id: str) -> dict | None:
    if v is None:
        return None
    return {
        "method": v.method,
        "for": v.tally_for,
        "against": v.tally_against,
        "abstain": v.tally_abstain,
        "eligible": v.eligible_count,
        "present": v.present_count,
        "quorum_required": v.quorum_required,
        "quorum_met": v.quorum_met,
        "result": v.result,
        "outcome_note": v.outcome_note,
        "confirmed": bool(v.confirmed),
        "ballots": [
            {
                "name": b.name, "person_key": util.public_person_key(session_id, b.person_key), "choice": b.choice,
                "cast_by": b.cast_by, "proxy": bool(b.proxy),
            }
            for b in ballots
        ],
    }


def decision_dto(d: MeetppDecision, vote: MeetppVote | None, ballots: list[MeetppBallot], previous: bool = False) -> dict:
    return {
        "id": d.id,
        "ref": d.ref,
        "section_id": d.section_id,
        "title": d.title,
        "resolution": d.resolution,
        "how_taken": d.how_taken,
        "status": d.status,
        "decided_at": util.iso(d.decided_at),
        "origin": d.origin,
        "confirmed": bool(d.confirmed),
        "locked": bool(d.locked),
        "evidence": util.loads(d.evidence_json, []),
        "previous": previous,
        "vote": vote_dto(vote, ballots, d.session_id),
    }


def _prev_section_id(o: outline_mod.Outline) -> str | None:
    home = outline_mod.previous_actions_home(o)
    return home.id if home is not None else None


def action_section_id(db: Session, session: MeetppSession, a: MeetppAction, o: outline_mod.Outline | None = None) -> str | None:
    o = o or outline_mod.load(db, session.id)
    if is_previous_action(a, session):
        return _prev_section_id(o) or (a.section_id if a.section_id in o.by_id else None)
    return a.section_id


def action_dto(
    a: MeetppAction,
    session: MeetppSession,
    section_id: str | None,
    report: MeetppActionReport | None,
    decision_refs: dict[str, str],
) -> dict:
    previous = is_previous_action(a, session)
    return {
        "id": a.id,
        "ref": a.ref,
        "section_id": section_id,
        "title": a.title,
        "description": a.description,
        "assignees": [
            {"name": x.get("name"), "person_key": util.public_person_key(session.id, x.get("person_key"))}
            for x in util.loads(a.assignees_json, [])
            if isinstance(x, dict)
        ],
        "due": a.due_date,
        "status": a.status,
        "decision_ref": decision_refs.get(a.decision_id or ""),
        "completed_at": util.iso(a.completed_at),
        "completion_note": a.completion_note,
        "progress_notes": a.progress_notes,
        "origin": a.origin,
        "locked": bool(a.locked),
        "evidence": util.loads(a.evidence_json, []),
        "previous": previous,
        "carried_forward": previous and a.status in OPEN_ACTION_STATUSES,
        "report": (
            {"note": report.note or "", "status": report.status_at_report, "at": util.iso(report.updated_at or report.created_at)}
            if report is not None
            else None
        ),
    }


def minute_dto(m: MeetppMinute) -> dict:
    return {
        "id": m.id,
        "kind": m.kind,
        "section_id": m.section_id,
        "notes": [
            {"text": n.get("text"), "evidence": n.get("evidence") or [], "at": n.get("at")}
            for n in util.loads(m.notes_json, [])
            if isinstance(n, dict)
        ],
        "narrative_md": m.narrative_md,
        "version": m.version,
        "status": m.status,
        "source_tier": m.source_tier,
        "composed_at": util.iso(m.composed_at),
        "locked": bool(m.locked),
        "error": m.error,
    }


def attendee_dto(a: MeetppAttendee, include_email: bool = False) -> dict:
    return {
        "id": a.id,
        "person_key": util.public_person_key(a.session_id, a.person_key),
        "name": a.display_name,
        "username": a.username,
        "email": a.email if include_email else None,
        "status": a.status,
        "online": bool(a.online),
        "voting": bool(a.voting),
        "represented_by": a.represented_by,
        "mandate_ref": a.mandate_ref,
        "opted_out": bool(a.opted_out),
        "required_next": bool(a.required_next),
        "required_reason": a.required_reason,
        "talk_seconds": round(float(a.talk_seconds or 0.0), 1),
    }


def attachment_dto(a: MeetppAttachment) -> dict:
    return {
        "id": a.id,
        "section_id": a.section_id,
        "kind": a.kind,
        "filename": a.filename,
        "caption": a.caption,
        "author": a.author,
        "created_at": util.iso(a.created_at),
        "url": f"/api/v1/meetpp/sessions/{a.session_id}/attachments/{a.id}",
    }


def document_dto(d: MeetppDocument) -> dict:
    return {
        "id": d.id,
        "kind": d.kind,
        "filename": d.filename,
        "title": d.title,
        "status": d.status,
        "error": d.error,
        "page_count": d.page_count,
        "summary": util.loads(d.summary_json, {}) or None,
    }


def segment_dto(s: MeetppSegment) -> dict:
    return {
        "seq": s.seq,
        "identity": s.identity,
        "name": s.name,
        "person_key": util.public_person_key(s.session_id, s.person_key),
        "t_start": util.iso(s.t_start),
        "t_end": util.iso(s.t_end),
        "text": s.text,
        "text_refined": s.text_refined,
        "tier": s.tier,
        "is_gap": bool(s.is_gap),
        "gap_reason": s.gap_reason,
    }


def series_dto(db: Session, series: MeetppSeries) -> dict:
    voting = db.query(MeetppRoster).filter_by(series_id=series.id, active=True, voting=True).count()
    return {
        "id": series.id,
        "title": series.title,
        "meeting_id": series.meeting_id,
        "meeting_type": series.meeting_type,
        "majority_rule": series.majority_rule,
        "quorum_required": series.quorum_required,
        "quorum_default": voting // 2 + 1 if voting else 0,
        "voting_members": voting,
    }


def roster_dto(r: MeetppRoster) -> dict:
    return {
        "id": r.id,
        "person_key": r.person_key,
        "name": r.display_name,
        "display_name": r.display_name,
        "username": r.username,
        "email": r.email,
        "voting": bool(r.voting),
        "active": bool(r.active),
        "first_seen_at": util.iso(r.first_seen_at),
        "last_seen_at": util.iso(r.last_seen_at),
    }


# ─── Snapshot and deltas ────────────────────────────────────────────────────


def session_actions(db: Session, session: MeetppSession) -> list[MeetppAction]:
    """Actions shown for this session: raised here, plus series actions from
    earlier sessions that are still open or were reported on at this meeting."""
    reported = {
        r.action_id for r in db.query(MeetppActionReport.action_id).filter_by(session_id=session.id).all()
    }
    rows = db.query(MeetppAction).filter_by(series_id=session.series_id).all()
    # Actions raised in later sessions of the series never show in an earlier one.
    cutoff = util.aware(session.ended_at) or util.now()
    out = [
        a
        for a in rows
        if a.session_id == session.id
        or a.id in reported
        or (a.status in OPEN_ACTION_STATUSES and util.aware(a.created_at) <= cutoff)
    ]
    # Previous actions first (by ref number), then this meeting's.
    def key(a: MeetppAction):
        m = re.search(r"\d+", a.ref or "")
        return (0 if is_previous_action(a, session) else 1, int(m.group(0)) if m else 0)

    return sorted(out, key=key)


def _collect(db: Session, session: MeetppSession, include_emails: bool) -> dict:
    o = outline_mod.load(db, session.id)
    series = db.get(MeetppSeries, session.series_id)
    decisions = db.query(MeetppDecision).filter_by(session_id=session.id).order_by(MeetppDecision.created_at).all()
    votes = {v.decision_id: v for v in db.query(MeetppVote).filter(MeetppVote.decision_id.in_([d.id for d in decisions] or [""])).all()}
    ballots: dict[str, list[MeetppBallot]] = {}
    if votes:
        for b in db.query(MeetppBallot).filter(MeetppBallot.vote_id.in_([v.id for v in votes.values()])).order_by(MeetppBallot.id).all():
            ballots.setdefault(b.vote_id, []).append(b)
    actions = session_actions(db, session)
    reports = {
        r.action_id: r
        for r in db.query(MeetppActionReport).filter_by(session_id=session.id).all()
    }
    decision_refs = {
        d.id: d.ref
        for d in db.query(MeetppDecision.id, MeetppDecision.ref).filter_by(series_id=session.series_id).all()
    }
    counts: dict[str, dict] = {}
    for d in decisions:
        if d.section_id:
            counts.setdefault(d.section_id, {"decisions": 0, "actions": 0})["decisions"] += 1
    action_dtos = []
    for a in actions:
        sid = action_section_id(db, session, a, o)
        if sid:
            counts.setdefault(sid, {"decisions": 0, "actions": 0})["actions"] += 1
        action_dtos.append(action_dto(a, session, sid, reports.get(a.id), decision_refs))
    minutes = db.query(MeetppMinute).filter_by(session_id=session.id).all()
    attendees = db.query(MeetppAttendee).filter_by(session_id=session.id).order_by(MeetppAttendee.display_name).all()
    attachments = db.query(MeetppAttachment).filter_by(session_id=session.id).order_by(MeetppAttachment.created_at).all()
    documents = db.query(MeetppDocument).filter_by(session_id=session.id).order_by(MeetppDocument.created_at).all()
    return {
        "session": session_meta(db, session, series=series),
        "sections": [section_dto(s, o, session, counts) for s in o.flat],
        "decisions": [decision_dto(d, votes.get(d.id), ballots.get(votes[d.id].id, []) if d.id in votes else []) for d in decisions],
        "actions": action_dtos,
        "minutes": [minute_dto(m) for m in minutes],
        "attendees": [attendee_dto(a, include_emails) for a in attendees],
        "attachments": [attachment_dto(a) for a in attachments],
        "documents": [document_dto(d) for d in documents],
        "quorum": governance.quorum(db, session, series) if governance.is_formal(series) else None,
    }


def build_state(db: Session, session: MeetppSession, *, include_emails: bool = False) -> dict:
    data = _collect(db, session, include_emails)
    return {"v": 1, "type": "state", "sid": session.id, "version": session.state_version, **data}


def without_person_keys(state: dict) -> dict:
    """The state for a viewer of the public page: no person keys at all."""
    for a in state.get("attendees") or []:
        a["person_key"] = None
    for d in state.get("decisions") or []:
        for b in (d.get("vote") or {}).get("ballots") or []:
            b["person_key"] = None
    for a in state.get("actions") or []:
        for x in a.get("assignees") or []:
            x["person_key"] = None
    return state


def build_delta(db: Session, session: MeetppSession, changes: Changes) -> dict:
    data = _collect(db, session, include_emails=False)
    delta: dict = {}
    if changes.session:
        delta["session"] = data["session"]
    buckets = (
        ("sections", changes.sections),
        ("decisions", changes.decisions),
        ("actions", changes.actions),
        ("minutes", changes.minutes),
        ("attendees", changes.attendees),
        ("attachments", changes.attachments),
        ("documents", changes.documents),
    )
    # Counts on sections follow decision/action changes.
    touched_sections = set(changes.sections)
    for d in data["decisions"]:
        if d["id"] in changes.decisions and d["section_id"]:
            touched_sections.add(d["section_id"])
    for a in data["actions"]:
        if a["id"] in changes.actions and a["section_id"]:
            touched_sections.add(a["section_id"])
    for name, ids in buckets:
        if name == "sections":
            ids = touched_sections
        if not ids:
            continue
        items = [x for x in data[name] if x["id"] in ids]
        if items:
            delta[name] = items
    if changes.quorum or changes.attendees:
        delta["quorum"] = data["quorum"]
    if changes.removed:
        delta["removed"] = changes.removed
    return delta


def sorted_activations(changes: Changes, limit: int = 3) -> list[dict]:
    acts = sorted(changes.activations, key=lambda a: -int(a.get("prio") or 0))
    return acts[:limit]


def bump_version(db: Session, session: MeetppSession) -> int:
    """Atomic `state_version + 1` in SQL; the in-memory object is updated
    without marking the column dirty, so a later flush cannot write back a
    stale value."""
    from sqlalchemy import text
    from sqlalchemy.orm.attributes import set_committed_value

    row = db.execute(
        text("UPDATE meetpp_sessions SET state_version = state_version + 1 WHERE id = :id RETURNING state_version"),
        {"id": session.id},
    ).fetchone()
    version = int(row[0]) if row else int(session.state_version or 0) + 1
    set_committed_value(session, "state_version", version)
    return version


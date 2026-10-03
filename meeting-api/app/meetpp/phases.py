"""Phase controller.

Deterministic rules plus the LLM's `phase.signal`. The AI proposes; the
chair accepts, unless Lead mode auto-accepts after an 8 s cancel window.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.meetpp.locales import t
from app.meetpp.models import (
    MeetppAction,
    MeetppAgendaItem,
    MeetppAttendee,
    MeetppOp,
    MeetppSegment,
    MeetppSession,
    utcnow,
)

log = logging.getLogger("app.meetpp")

PHASES = ["opening", "previous_actions", "agenda", "discussion", "aob", "new_actions", "closing"]
GOAL_DISPLAY = {"agenda": "goal", "aob": "next_steps", "new_actions": "planning"}
LEAD_AUTO_CONFIDENCE = 0.85
LLM_MIN_CONFIDENCE = 0.75
CANCEL_WINDOW_SECONDS = 8
SUPPRESS_SECONDS = 180


def phase_index(phase: str) -> int:
    base = (phase or "opening").split(":")[0]
    return PHASES.index(base) + 1 if base in PHASES else 1


def next_phase(phase: str) -> str:
    base = (phase or "opening").split(":")[0]
    i = PHASES.index(base) if base in PHASES else 0
    return PHASES[min(i + 1, len(PHASES) - 1)]


def prev_phase(phase: str) -> str:
    base = (phase or "opening").split(":")[0]
    i = PHASES.index(base) if base in PHASES else 0
    return PHASES[max(i - 1, 0)]


def _display_key(template: str, phase: str) -> str:
    base = (phase or "opening").split(":")[0]
    if template == "goal":
        return GOAL_DISPLAY.get(base, base)
    return base


def _elapsed(session: MeetppSession) -> float:
    if not session.started_at:
        return 0.0
    started = session.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds()


def _last_segment_age(db: Session, session: MeetppSession) -> float | None:
    row = (
        db.query(MeetppSegment.t_end)
        .filter(MeetppSegment.session_id == session.id)
        .order_by(MeetppSegment.seq.desc())
        .first()
    )
    if not row or row[0] is None:
        return None
    end = row[0]
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - end).total_seconds()


def _has_open_series_actions(db: Session, series_id: str, session_id: str) -> bool:
    return (
        db.query(MeetppAction.id)
        .filter(
            MeetppAction.series_id == series_id,
            MeetppAction.session_id != session_id,
            MeetppAction.status.notin_(("done", "dropped")),
        )
        .first()
        is not None
    )


def _all_previous_reviewed(db: Session, series_id: str, session_id: str) -> bool:
    pending = (
        db.query(MeetppAction.id)
        .filter(
            MeetppAction.series_id == series_id,
            MeetppAction.session_id != session_id,
            MeetppAction.status.notin_(("done", "dropped")),
            MeetppAction.review_status.is_(None),
        )
        .first()
    )
    return pending is None


def _all_new_actions_assigned(db: Session, session_id: str) -> bool:
    pending = (
        db.query(MeetppAction.id)
        .filter(
            MeetppAction.session_id == session_id,
            MeetppAction.status.notin_(("done", "dropped")),
            (MeetppAction.owner_name.is_(None)) | (MeetppAction.due_date.is_(None)),
        )
        .first()
    )
    return pending is None


def rule_proposal(db: Session, session: MeetppSession) -> dict | None:
    """Deterministic proposal (confidence 0.9), or None."""
    phase = (session.phase or "opening").split(":")[0]
    elapsed = _elapsed(session)

    if phase == "opening":
        if elapsed >= 120:
            if _has_open_series_actions(db, session.series_id, session.id):
                return _proposal(session, "previous_actions", None, 0.9, "rule", "opening elapsed")
            has_agenda = db.query(MeetppAgendaItem.id).filter_by(session_id=session.id).first() is not None
            return _proposal(session, "agenda" if has_agenda else "aob", None, 0.9, "rule", "opening elapsed")
        return None

    if phase == "previous_actions":
        if _all_previous_reviewed(db, session.series_id, session.id):
            return _proposal(session, "agenda", None, 0.9, "rule", "all previous actions reviewed")
        return None

    if phase == "agenda" and elapsed >= 180:
        item = _first_pending(db, session)
        if item is not None:
            return _proposal(session, "discussion", item.id, 0.9, "rule", "agenda confirmed")
        return _proposal(session, "aob", None, 0.9, "rule", "agenda confirmed, no items")

    if phase == "aob":
        age = _last_segment_age(db, session)
        if age is not None and age >= 60:
            return _proposal(session, "new_actions", None, 0.9, "rule", "no new topic")

    if phase == "new_actions" and _all_new_actions_assigned(db, session.id):
        return _proposal(session, "closing", None, 0.9, "rule", "new actions assigned")

    return None


def _first_pending(db: Session, session: MeetppSession) -> MeetppAgendaItem | None:
    return (
        db.query(MeetppAgendaItem)
        .filter_by(session_id=session.id, status="pending")
        .order_by(MeetppAgendaItem.position)
        .first()
    )


def llm_proposal(session: MeetppSession, signal: dict | None) -> dict | None:
    if not signal:
        return None
    try:
        confidence = float(signal.get("confidence") or 0)
    except (TypeError, ValueError):
        return None
    to = str(signal.get("to") or "").strip()
    if confidence < LLM_MIN_CONFIDENCE or not to:
        return None
    # Only the next phase or the next agenda item. Backward jumps are never
    # proposed by the AI.
    target_base = to.split(":")[0]
    allowed = {next_phase(session.phase), session.phase, "discussion:next_item", "discussion"}
    if to not in allowed and target_base != next_phase(session.phase):
        return None
    return _proposal(session, to, None, confidence, "llm", str(signal.get("reason") or "")[:160])


def _proposal(session: MeetppSession, to: str, item_id: str | None, confidence: float, source: str, reason: str) -> dict:
    pid = f"p{session.state_version}-{int(utcnow().timestamp())}"
    auto_at = None
    if session.mode == "lead" and confidence >= LEAD_AUTO_CONFIDENCE:
        auto_at = (utcnow() + timedelta(seconds=CANCEL_WINDOW_SECONDS)).isoformat()
    return {
        "pid": pid,
        "to": to,
        "item_id": item_id,
        "confidence": confidence,
        "source": source,
        "reason": reason,
        "auto_at": auto_at,
    }


def current_proposal(session: MeetppSession) -> dict | None:
    if not session.proposal_json:
        return None
    try:
        data = json.loads(session.proposal_json)
        return data if isinstance(data, dict) and "to" in data else None
    except ValueError:
        return None


def propose(db: Session, session: MeetppSession, llm_signal: dict | None = None) -> dict | None:
    """Combine rule and LLM proposals, honouring a suppression window."""
    existing = current_proposal(session)
    if existing:
        suppressed = existing.get("suppressed_until")
        if suppressed:
            try:
                until = datetime.fromisoformat(suppressed)
            except ValueError:
                until = None
            if until is not None:
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if utcnow() < until:
                    return None
    proposal = llm_proposal(session, llm_signal) or rule_proposal(db, session)
    if proposal is None:
        return None
    # Don't re-propose an identical target that is already pending.
    if existing and existing.get("to") == proposal["to"] and existing.get("item_id") == proposal.get("item_id"):
        return existing
    session.proposal_json = json.dumps(proposal)
    return proposal


def suppress(db: Session, session: MeetppSession, proposal: dict | None) -> None:
    if proposal is None:
        return
    data = dict(proposal)
    # A suppressed proposal must never auto-accept later.
    data.pop("auto_at", None)
    data["auto_at"] = None
    data["suppressed_until"] = (utcnow() + timedelta(seconds=SUPPRESS_SECONDS)).isoformat()
    session.proposal_json = json.dumps(data)


def clear(session: MeetppSession) -> None:
    session.proposal_json = None


def acceptance(db: Session, session: MeetppSession, to: str, item_id: str | None, by: str) -> dict:
    """Apply an accepted transition and return the announcement payload."""
    base = to.split(":")[0]
    if to == "discussion:next_item" or base == "discussion":
        # advance to the next pending item, or aob if none
        if to == "discussion:next_item":
            cur = db.get(MeetppAgendaItem, session.current_item_id) if session.current_item_id else None
            if cur is not None and cur.status == "active":
                cur.status = "done"
                cur.closed_at = utcnow()
            nxt = _first_pending(db, session)
            if nxt is None:
                base = "aob"
                session.current_item_id = None
            else:
                nxt.status = "active"
                nxt.started_at = utcnow()
                session.current_item_id = nxt.id
        else:
            if item_id:
                item = db.get(MeetppAgendaItem, item_id)
                if item is not None:
                    item.status = "active"
                    item.started_at = item.started_at or utcnow()
                    session.current_item_id = item.id
            else:
                nxt = _first_pending(db, session)
                if nxt is not None:
                    nxt.status = "active"
                    nxt.started_at = utcnow()
                    session.current_item_id = nxt.id
        phase_to = "discussion"
    else:
        phase_to = base
        if base == "closing":
            session.ended_at = session.ended_at or utcnow()

    session.phase = phase_to
    session.phase_index = phase_index(phase_to)
    clear(session)
    db.add(
        MeetppOp(
            session_id=session.id,
            version=session.state_version,
            op_type="phase.change",
            payload_json=json.dumps({"to": to, "item_id": item_id, "by": by}),
            actor=by,
            status="applied",
        )
    )
    return announcement(db, session, phase_to, item_id)


def announcement(db: Session, session: MeetppSession, phase: str, item_id: str | None = None) -> dict:
    """Build the announcement (title/subtitle) for a phase."""
    lang = session.language
    base = (phase or "opening").split(":")[0]
    item = None
    if base == "discussion":
        target_id = item_id or session.current_item_id
        if target_id:
            item = db.get(MeetppAgendaItem, target_id)
    if base == "discussion" and item is not None:
        idx = item.position
        title = t(lang, "announce.discussion.title", n=idx, item=item.title)
        presenter = item.presenter or ""
        subtitle = t(lang, "announce.discussion.subtitle", presenter=presenter) if presenter else ""
    else:
        key = _display_key(session.template, base)
        title = t(lang, f"announce.{key}.title")
        subtitle = t(lang, f"announce.{key}.subtitle")
    return {
        "phase": base,
        "item_id": item.id if item else (item_id or None),
        "title": title[:60],
        "subtitle": subtitle[:90],
    }


def timebox_nudge(db: Session, session: MeetppSession) -> dict | None:
    """Return a nudge announcement if the active item has passed its timebox."""
    if not session.current_item_id:
        return None
    item = db.get(MeetppAgendaItem, session.current_item_id)
    if item is None or not item.timebox_minutes or not item.started_at:
        return None
    started = item.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    over = (datetime.now(timezone.utc) - started).total_seconds() > item.timebox_minutes * 60
    if not over:
        return None
    return {
        "phase": "discussion",
        "item_id": item.id,
        "title": t(session.language, "announce.timebox.title", n=item.position)[:60],
        "subtitle": "",
    }

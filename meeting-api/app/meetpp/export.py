"""Export dict of a session (contract §7) — the input of the report renderer
and of the R2 push to the OM module."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.meetpp import compose, governance, ops, outline as outline_mod, util
from app.meetpp.models import (
    MeetppActionReport,
    MeetppAttachment,
    MeetppAttendee,
    MeetppBallot,
    MeetppDecision,
    MeetppDocument,
    MeetppMinute,
    MeetppMinutesVersion,
    MeetppSeries,
    MeetppSession,
    MeetppVote,
)
from app.models import Meeting

STATUS_LABELS = {
    "present": "Present",
    "represented": "Represented",
    "absent": "Absent",
    "excused": "Excused",
    "not_registered": "Not registered",
}
DECISION_LABELS = {"adopted": "Adopted", "rejected": "Rejected", "proposed": "Proposed", "withdrawn": "Withdrawn", "pending": "Pending"}
ACTION_LABELS = {"proposed": "Proposed", "open": "Open", "in_progress": "In progress", "done": "Done", "cancelled": "Cancelled"}


def _placeholder_fields() -> list[str]:
    out = []
    for name in ("enterprise", "iban"):
        value = getattr(settings, f"meetpp_org_{name}", "") or ""
        if not value.strip() or "9999" in value:
            out.append(name)
    email = settings.meetpp_org_email or ""
    if not email.strip():
        out.append("email")
    return out


def org_block() -> dict:
    placeholders = _placeholder_fields()
    return {
        "name": settings.meetpp_org_name,
        "tagline": settings.meetpp_org_tagline,
        "seat": settings.meetpp_org_seat,
        "enterprise": settings.meetpp_org_enterprise or None,
        "rpr": settings.meetpp_org_rpr or None,
        "email": settings.meetpp_org_email or None,
        "website": settings.meetpp_org_website or None,
        "iban": settings.meetpp_org_iban or None,
        "logo_path": None,
        "placeholders": placeholders or False,
    }


def _subpoint_label(i: int) -> str:
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(ord("a") + r) + out
    return out


def _session_date(s: MeetppSession) -> datetime | None:
    return util.aware(s.started_at or s.created_at)


def build_export(db: Session, session: MeetppSession) -> dict:
    series = db.get(MeetppSeries, session.series_id)
    meeting = db.get(Meeting, session.meeting_id)
    o = outline_mod.load(db, session.id)
    formal = governance.is_formal(series)
    meeting_type = series.meeting_type if series else "informal"
    location = f"{settings.public_url}/{meeting.room_name}" if meeting else None

    # Attendance
    attendees = db.query(MeetppAttendee).filter_by(session_id=session.id).all()
    order = {"present": 0, "represented": 1, "excused": 2, "absent": 3, "not_registered": 4}
    attendees.sort(key=lambda a: (order.get(a.status, 9), util.norm_name(a.display_name)))
    summary = {k: 0 for k in ("present", "represented", "absent", "excused", "not_registered")}
    rows = []
    for a in attendees:
        summary[a.status] = summary.get(a.status, 0) + 1
        rows.append(
            {
                "name": a.display_name,
                "username": a.username,
                "status_label": STATUS_LABELS.get(a.status, a.status.title()),
                "represented_by": a.represented_by,
                "mandate": a.mandate_ref,
            }
        )

    quorum_note = None
    if formal:
        q = governance.quorum(db, session, series)
        noun = "directors" if meeting_type == "board" else "voting members"
        state = "Quorum met" if q["met"] else "Quorum not met"
        quorum_note = (
            f"{state} ({q['voting_present']} of {q['voting_total']} {noun} present or represented; quorum {q['required']})."
        )
        if session.started_at:
            quorum_note += f" Business taken from {util.aware(session.started_at).strftime('%H:%M')} UTC"
            if session.ended_at:
                quorum_note += f"; adjourned {util.aware(session.ended_at).strftime('%H:%M')} UTC"
            quorum_note += "."

    # Agenda
    agenda = []
    for s in o.tops():
        if s.kind != "agenda":
            continue
        agenda.append(
            {
                "number": o.numbers.get(s.id),
                "title": s.title,
                "body": s.body,
                "subpoints": [
                    {"label": _subpoint_label(i), "title": c.title, "body": c.body}
                    for i, c in enumerate(o.children.get(s.id, []))
                ],
            }
        )

    # Decisions (pending decisions that were never taken are left out)
    decisions = [
        d
        for d in db.query(MeetppDecision).filter_by(session_id=session.id).order_by(MeetppDecision.created_at).all()
        if d.status != "pending"
    ]
    rule = series.majority_rule if series else "ordinary"
    decision_rows = []
    for n, d in enumerate(decisions, start=1):
        vote = db.query(MeetppVote).filter_by(decision_id=d.id).first()
        vote_out = None
        if formal and vote is not None:
            ballots = db.query(MeetppBallot).filter_by(vote_id=vote.id).order_by(MeetppBallot.id).all()
            result = vote.result or (d.status if d.status in ("adopted", "rejected") else None)
            vote_out = {
                "question": vote.question or d.title,
                "for": vote.tally_for,
                "against": vote.tally_against,
                "abstain": vote.tally_abstain,
                "result_label": governance.RESULT_LABELS.get(result or "", DECISION_LABELS.get(d.status, "")),
                "voting_body_label": governance.BODY_LABELS.get(meeting_type, "Board"),
                "majority_label": governance.MAJORITY_LABELS.get(rule, "Simple majority"),
                "eligible": vote.eligible_count,
                "basis_label": "Directors in office" if meeting_type == "board" else "Voting members",
                "present_or_represented": vote.present_count,
                "quorum_required": vote.quorum_required,
                "quorum_met": vote.quorum_met,
                "outcome_sentence": governance.outcome_sentence(
                    result=result,
                    meeting_type=meeting_type,
                    rule=rule,
                    n_for=vote.tally_for,
                    n_against=vote.tally_against,
                    n_abstain=vote.tally_abstain,
                    eligible=vote.eligible_count,
                    present=vote.present_count,
                    quorum_required=vote.quorum_required,
                    quorum_met=vote.quorum_met,
                ),
                "ballots": [
                    {
                        "name": b.name,
                        "vote_label": governance.CHOICE_LABELS.get(b.choice, "Not recorded"),
                        "cast_by": b.cast_by,
                        "proxy": bool(b.proxy),
                    }
                    for b in ballots
                ],
            }
        section = o.by_id.get(d.section_id or "")
        decision_rows.append(
            {
                "number": n,
                "ref": d.ref,
                "title": d.title,
                "resolution": d.resolution,
                "how_taken": d.how_taken,
                "status_label": DECISION_LABELS.get(d.status, d.status.title()),
                "decided_at_iso": util.iso(d.decided_at),
                "agenda_number": o.numbers.get(section.id) if section else None,
                "vote": vote_out,
            }
        )

    # Actions
    sessions_by_id = {s.id: s for s in db.query(MeetppSession).filter_by(series_id=session.series_id).all()}
    meetings_by_id = {}

    def _session_title(sid: str) -> tuple[str, str | None]:
        s = sessions_by_id.get(sid)
        if s is None:
            return (series.title if series else "", None)
        if s.meeting_id not in meetings_by_id:
            meetings_by_id[s.meeting_id] = db.get(Meeting, s.meeting_id)
        m = meetings_by_id[s.meeting_id]
        return (m.display_title if m else (series.title if series else "")), util.iso(_session_date(s))

    decision_titles = {d.id: d.title for d in db.query(MeetppDecision).filter_by(series_id=session.series_id).all()}
    action_rows = []
    for n, a in enumerate(ops.session_actions(db, session), start=1):
        previous = ops.is_previous_action(a, session)
        report = db.query(MeetppActionReport).filter_by(action_id=a.id, session_id=session.id).first()
        also_on = []
        if a.session_id != session.id:
            title, when = _session_title(a.session_id)
            also_on.append({"title": title, "date_iso": when, "raised": True})
        for r in db.query(MeetppActionReport).filter(MeetppActionReport.action_id == a.id, MeetppActionReport.session_id != session.id).all():
            if r.session_id == a.session_id:
                continue
            title, when = _session_title(r.session_id)
            also_on.append({"title": title, "date_iso": when})
        also_on.sort(key=lambda e: e.get("date_iso") or "")
        status = a.status
        action_rows.append(
            {
                "number": n,
                "ref": a.ref,
                "title": a.title,
                "carried_forward": previous and status in ops.OPEN_ACTION_STATUSES,
                "description": a.description,
                "status_label": ACTION_LABELS.get(status, status.title()),
                "assignees": [x.get("name") for x in util.loads(a.assignees_json, []) if isinstance(x, dict) and x.get("name")],
                "due_iso": a.due_date,
                "completed_iso": util.iso(a.completed_at),
                "reported_note": (report.note or None) if report is not None else None,
                "progress_notes": a.progress_notes,
                "completion_note": a.completion_note,
                "also_on": also_on,
                "from_decision": decision_titles.get(a.decision_id or ""),
            }
        )

    # Papers: uploaded documents and snapshots
    papers = []
    for doc in db.query(MeetppDocument).filter_by(session_id=session.id).order_by(MeetppDocument.created_at).all():
        papers.append(
            {
                "title": doc.title or ("Agenda" if doc.kind == "agenda" else "Notes of the previous meeting"),
                "file": doc.filename,
                "agenda_number": None,
            }
        )
    for att in db.query(MeetppAttachment).filter_by(session_id=session.id).order_by(MeetppAttachment.created_at).all():
        section = o.by_id.get(att.section_id or "")
        papers.append(
            {
                "title": att.caption or "Whiteboard snapshot",
                "file": att.filename,
                "agenda_number": o.numbers.get(o.top(section).id) if section else None,
            }
        )

    # Minutes
    minutes_rows = db.query(MeetppMinute).filter_by(session_id=session.id).all()
    saved = max((util.aware(m.updated_at) for m in minutes_rows if m.updated_at), default=None)
    last_version = (
        db.query(MeetppMinutesVersion).filter_by(session_id=session.id).order_by(MeetppMinutesVersion.version.desc()).first()
    )
    version = last_version.version if last_version is not None and session.status == "published" else (
        (last_version.version + 1) if last_version is not None else 1
    )

    # Next meeting
    review = util.loads(session.review_json, {})
    final = util.loads(session.final_json, {})
    next_items = review.get("next_agenda") if isinstance(review.get("next_agenda"), list) else final.get("next_agenda") or []
    next_agenda = [
        {"number": str(i), "title": util.truncate(it.get("title"), 300), "body": util.truncate(it.get("body"), 4000)}
        for i, it in enumerate((x for x in next_items if isinstance(x, dict) and x.get("title")), start=1)
    ]
    nm = review.get("next_meeting") if isinstance(review.get("next_meeting"), dict) else {}
    next_meeting = None
    if nm.get("date_iso"):
        room = meeting.room_name if meeting else ""
        if nm.get("room") == "new" and nm.get("room_name"):
            room = nm["room_name"]
        next_meeting = {"date_iso": nm.get("date_iso"), "location": f"{settings.public_url}/{room}" if room else None}

    return {
        "org": org_block(),
        "meeting": {
            "title": meeting.display_title if meeting else (series.title if series else "Meeting"),
            "series_title": series.title if series else None,
            "date_iso": util.iso(session.started_at or (meeting.scheduled_at if meeting else None) or session.created_at),
            "location": location,
            "type_label": governance.TYPE_LABELS.get(meeting_type, "Meeting"),
            "status_label": "Held",
            "convened_at_iso": util.iso(session.started_at),
            "adjourned_at_iso": util.iso(session.ended_at),
            "quorum_note": quorum_note,
            "formal": formal,
        },
        "generated_at_iso": util.iso(util.now()),
        "attendance": {"summary": summary, "rows": rows},
        "agenda": agenda,
        "decisions": decision_rows,
        "actions": action_rows,
        "papers": papers,
        "minutes": {
            "saved_at_iso": util.iso(saved),
            "version": version,
            "markdown": compose.minutes_markdown(db, session),
        },
        "next_agenda": next_agenda,
        "next_meeting": next_meeting,
    }

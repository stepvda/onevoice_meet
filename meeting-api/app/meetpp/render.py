"""Output rendering: Jinja2 HTML → WeasyPrint PDF, plus JSON/Markdown export.

WeasyPrint is imported lazily so the app and the test suite still run when the
system pango libraries are absent; `_html_to_pdf` then falls back to returning
the HTML bytes with a marker so the failure is visible but non-fatal.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

from app.meetpp import ops
from app.meetpp.locales import t
from app.meetpp.models import (
    MeetppAction,
    MeetppAgendaItem,
    MeetppAttachment,
    MeetppAttendee,
    MeetppDecision,
    MeetppMinute,
    MeetppSession,
)

log = logging.getLogger("app.meetpp")

_TEMPLATES = Path(__file__).parent / "templates"
# Autoescape explicitly: the template filenames end in `.j2`, so the
# suffix-based select_autoescape() would disable escaping for both templates.
# All rendered fields (participant names, LLM-authored minutes, agenda text)
# are untrusted and must be escaped before WeasyPrint renders the HTML.
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    autoescape=True,
)


def _tr(lang: str, key: str, **vars) -> str:
    """Like locales.t but supports a `defaultValue` fallback and tolerates
    unused placeholders (used by the HTML templates)."""
    default = vars.pop("defaultValue", None)
    value = t(lang, key)
    if value == key and default is not None:
        value = str(default)
    try:
        return value.format(**vars) if vars else value
    except (KeyError, IndexError):
        return value


def _attendee_groups(db: Session, session: MeetppSession) -> dict:
    attendees = db.query(MeetppAttendee).filter_by(session_id=session.id).all()
    present = [a for a in attendees if a.presence in ("present", "left") and not a.opted_out]
    opted = [a for a in attendees if a.opted_out or a.presence == "not_transcribed"]
    apologies = [a for a in attendees if a.presence == "apologies"]
    absent = [a for a in attendees if a.presence == "absent" and not a.opted_out]
    return {"present": present, "apologies": apologies, "absent": absent, "not_transcribed": opted}


def _minutes_context(db: Session, session: MeetppSession) -> dict:
    from datetime import datetime, timezone

    from app.config import settings
    from app.models import Meeting

    meeting = db.get(Meeting, session.meeting_id)
    items = db.query(MeetppAgendaItem).filter_by(session_id=session.id).order_by(MeetppAgendaItem.position).all()
    minutes = {m.agenda_item_id: m for m in db.query(MeetppMinute).filter_by(session_id=session.id).all()}
    decisions = db.query(MeetppDecision).filter_by(session_id=session.id).order_by(MeetppDecision.ref).all()
    actions = (
        db.query(MeetppAction)
        .filter(MeetppAction.session_id == session.id)
        .order_by(MeetppAction.ref)
        .all()
    )
    open_actions = (
        db.query(MeetppAction)
        .filter(MeetppAction.series_id == session.series_id, MeetppAction.status.notin_(("done", "dropped")))
        .order_by(MeetppAction.ref)
        .all()
    )
    attachments = db.query(MeetppAttachment).filter_by(session_id=session.id).all()
    final = {}
    if session.final_json:
        try:
            final = json.loads(session.final_json)
        except ValueError:
            final = {}
    groups = _attendee_groups(db, session)
    counts = {
        "present": len([a for a in groups["present"] if a.display_name]),
        "excused": len(groups["apologies"]),
        "absent": len(groups["absent"]),
        "not_transcribed": len(groups["not_transcribed"]),
    }
    org = {
        "name": settings.meetpp_org_name,
        "tagline": settings.meetpp_org_tagline,
        "seat": settings.meetpp_org_seat,
        "enterprise": settings.meetpp_org_enterprise,
        "rpr": settings.meetpp_org_rpr,
        "email": settings.meetpp_org_email,
        "website": settings.meetpp_org_website,
        "iban": settings.meetpp_org_iban,
        "notice": settings.meetpp_report_notice,
    }
    return {
        "session": session,
        "meeting": meeting,
        "items": items,
        "minutes": minutes,
        "decisions": decisions,
        "actions": actions,
        "open_actions": open_actions,
        "attachments": attachments,
        "final": final,
        "groups": groups,
        "counts": counts,
        "org": org,
        "public_url": settings.public_url,
        "generated_at": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        "t": lambda key, **v: _tr(session.language, key, **v),
    }


def render_minutes_html(db: Session, session: MeetppSession) -> str:
    ctx = _minutes_context(db, session)
    meeting = ctx.get("meeting")
    ctx["chair"] = (ctx["final"].get("chair") or (meeting.owner_name if meeting else "") or "")
    ctx["version"] = max(1, session.publish_version)
    return _env.get_template("minutes.html.j2").render(**ctx)


def render_agenda_html(db: Session, session: MeetppSession, booking: dict) -> str:
    ctx = _minutes_context(db, session)
    next_agenda = ctx["final"].get("next_agenda") or []
    required = [a for a in db.query(MeetppAttendee).filter_by(session_id=session.id, required_next=True).all()]
    return _env.get_template("agenda.html.j2").render(
        session=session,
        booking=booking,
        next_agenda=next_agenda,
        required=required,
        open_actions=ctx["open_actions"],
        t=lambda key, **v: t(session.language, key, **v),
    )


def html_to_pdf(html: str) -> bytes:
    try:
        from weasyprint import HTML  # type: ignore

        return HTML(string=html).write_pdf()
    except Exception as exc:  # noqa: BLE001
        log.warning("meetpp: PDF render unavailable (%s); falling back to HTML bytes", exc)
        return html.encode("utf-8")


def export_json(db: Session, session: MeetppSession) -> dict:
    state = ops.build_state(db, session)
    return {
        "schema": "meetpp.session/v1",
        "session": state["session"],
        "agenda": state["agenda"],
        "decisions": state["decisions"],
        "actions": state["actions"],
        "attendance": state["attendance"],
        "minutes": state["minutes"],
        "attachments": state["attachments"],
        "final": json.loads(session.final_json) if session.final_json else None,
    }


def export_markdown(db: Session, session: MeetppSession) -> str:
    state = ops.build_state(db, session)
    lang = session.language
    lines = [f"# {t(lang, 'pdf.minutes_title')} — {session.id}", ""]
    lines.append(f"## {t(lang, 'pdf.attendance')}")
    for a in state["attendance"]:
        lines.append(f"- {a['name']} — {a['presence']}")
    lines.append("")
    lines.append(f"## {t(lang, 'pdf.decisions')}")
    for d in state["decisions"]:
        lines.append(f"- **{d['ref']}** {d['text']}")
    lines.append("")
    lines.append(f"## {t(lang, 'pdf.actions')}")
    for a in state["actions"]:
        lines.append(f"- **{a['ref']}** {a['title']} — {a['owner'] or '-'} — {a['due'] or '-'} ({a['status']})")
    lines.append("")
    lines.append(f"## {t(lang, 'pdf.minutes_title')}")
    for m in state["minutes"]:
        lines.append(m["body_md"])
        lines.append("")
    return "\n".join(lines)

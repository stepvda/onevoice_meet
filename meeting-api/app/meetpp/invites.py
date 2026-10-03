"""Next-meeting creation / RRULE occurrence handling, ICS invitations and
e-mail fan-out. Nothing is sent before the chair publishes."""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session
from ulid import ULID

from app.config import settings
from app.meetpp import render
from app.meetpp.locales import t
from app.meetpp.models import MeetppAttendee, MeetppOutput, MeetppSession, utcnow
from app.models import Meeting
from app.services.email import send_email
from app.services.ics import ics_invite

log = logging.getLogger("app.meetpp")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def parse_booking(session: MeetppSession, meeting: Meeting) -> dict:
    booking: dict = {}
    if session.review_json:
        try:
            booking = json.loads(session.review_json) or {}
        except ValueError:
            booking = {}
    base = _aware(meeting.scheduled_at) or utcnow()
    booking.setdefault("title", meeting.display_title)
    booking.setdefault("duration_min", meeting.duration_minutes or 60)
    booking.setdefault("room", meeting.room_name)
    booking.setdefault("iso", next_occurrence_iso(meeting, base))
    return booking


def _rrule_parts(rrule: str | None) -> tuple[str, int] | None:
    if not rrule:
        return None
    m_freq = re.search(r"FREQ=(\w+)", rrule)
    if not m_freq:
        return None
    m_int = re.search(r"INTERVAL=(\d+)", rrule)
    return m_freq.group(1).upper(), int(m_int.group(1)) if m_int else 1


def next_occurrence_iso(meeting: Meeting, after: datetime) -> str:
    """Compute the next occurrence start as an ISO string. For non-recurring
    meetings this is `after` + the meeting duration so the draft is sensible."""
    parts = _rrule_parts(meeting.recurrence_rule)
    base = _aware(meeting.scheduled_at) or after
    if not parts:
        return (after + timedelta(minutes=meeting.duration_minutes or 60)).isoformat()
    freq, interval = parts
    cur = base
    if freq == "DAILY":
        step = timedelta(days=interval)
    elif freq == "WEEKLY":
        step = timedelta(weeks=interval)
    elif freq == "MONTHLY":
        step = timedelta(days=30 * interval)
    else:
        step = timedelta(weeks=interval)
    # advance until strictly after `after`
    guard = 0
    while cur <= after and guard < 1000:
        cur = cur + step
        guard += 1
    return cur.isoformat()


def _attendee_emails(db: Session, session: MeetppSession) -> list[dict]:
    out = []
    seen = set()
    for a in db.query(MeetppAttendee).filter_by(session_id=session.id).all():
        if a.email and a.email not in seen:
            seen.add(a.email)
            out.append({"name": a.display_name, "email": a.email})
    return out


def _outputs_dir(session: MeetppSession) -> Path:
    p = Path(settings.meetpp_data_dir) / session.id / "outputs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_output(
    db: Session,
    session: MeetppSession,
    kind: str,
    filename: str,
    data: bytes,
    version: int,
    recipients: list[dict] | None = None,
) -> MeetppOutput:
    path = _outputs_dir(session) / f"v{version}-{filename}"
    path.write_bytes(data)
    row = MeetppOutput(
        id=str(ULID()),
        session_id=session.id,
        kind=kind,
        version=version,
        path=str(path),
        filename=filename,
        recipients_json=json.dumps(recipients or []),
    )
    db.add(row)
    return row


async def publish(db: Session, session: MeetppSession) -> dict:
    """Render outputs, create/attach the next meeting and send e-mails.
    Idempotent per publish version (re-publish writes a new version)."""
    meeting = db.get(Meeting, session.meeting_id)
    if meeting is None:
        raise ValueError("meeting not found")
    version = session.publish_version + 1
    booking = parse_booking(session, meeting)
    join_url = f"{settings.public_url}/{booking.get('room') or meeting.room_name}"
    lang = session.language

    minutes_pdf = render.html_to_pdf(render.render_minutes_html(db, session))
    agenda_pdf = render.html_to_pdf(render.render_agenda_html(db, session, booking))
    _write_output(db, session, "minutes_pdf", "minutes.pdf", minutes_pdf, version)
    _write_output(db, session, "agenda_pdf", "agenda.pdf", agenda_pdf, version)

    # Next meeting handling. RRULE: the existing row already holds the
    # occurrence — attach the agenda, create no duplicate. One-off: create a
    # new meeting in the same series.
    next_meeting = None
    if meeting.recurrence_rule:
        next_meeting = meeting
    else:
        next_meeting = Meeting(
            id=str(ULID()),
            room_name=f"{meeting.room_name[:40]}-{str(ULID())[:6].lower()}",
            display_title=booking.get("title") or meeting.display_title,
            owner_user_id=meeting.owner_user_id,
            owner_email=meeting.owner_email,
            owner_name=meeting.owner_name,
            scheduled_at=_parse_iso(booking.get("iso")),
            duration_minutes=int(booking.get("duration_min") or 60),
            meetpp_series_id=session.series_id,
        )
        db.add(next_meeting)
        db.flush()

    # ICS invitation.
    uid = f"meetpp-{session.series_id}-{datetime.now(timezone.utc).strftime('%Y%m%d')}@meet.witysk.org"
    required = (
        db.query(MeetppAttendee)
        .filter_by(session_id=session.id, required_next=True)
        .all()
    )
    attendees = (
        [{"name": a.display_name, "email": a.email} for a in required if a.email]
        or _attendee_emails(db, session)
    )
    # Add explicit review recipients that have an email.
    for r in booking.get("recipients") or []:
        if isinstance(r, dict) and r.get("email"):
            attendees.append({"name": r.get("name") or r["email"], "email": r["email"]})
    seen = set()
    attendees = [a for a in attendees if not (a["email"] in seen or seen.add(a["email"]))]

    dtstart = _parse_iso(booking.get("iso")) or utcnow()
    dtend = dtstart + timedelta(minutes=int(booking.get("duration_min") or 60))
    agenda_text = "\n".join(
        f"{i+1}. {it.get('title')}" for i, it in enumerate(
            (json.loads(session.final_json) if session.final_json else {}).get("next_agenda") or []
        )
    )
    ics_text = ics_invite(
        uid=uid,
        sequence=session.publish_version,
        summary=booking.get("title") or meeting.display_title,
        join_url=join_url,
        dtstart=dtstart,
        dtend=dtend,
        organizer_name=meeting.owner_name,
        organizer_email=meeting.owner_email,
        attendees=attendees,
        description_text=(agenda_text + "\n\n" if agenda_text else "") + f"Join: {join_url}",
    )
    _write_output(db, session, "ics", "invite.ics", ics_text.encode("utf-8"), version)

    results = {"minutes": [], "invites": []}
    if settings.resend_api_key:
        # Minutes to attendees.
        for a in _attendee_emails(db, session):
            ok = await send_email(
                to=a["email"],
                subject=t(lang, "email.minutes_subject", title=meeting.display_title),
                html=_simple_email(
                    t(lang, "email.greeting"),
                    t(lang, "email.minutes_intro", title=meeting.display_title),
                ),
                reply_to=meeting.owner_email or settings.invite_reply_to or None,
                attachments=[
                    {
                        "filename": "minutes.pdf",
                        "content": base64.b64encode(minutes_pdf).decode("ascii"),
                        "content_type": "application/pdf",
                    }
                ],
            )
            results["minutes"].append({"email": a["email"], "ok": ok})
        # Invitations to required attendees.
        for a in attendees[: settings.meetpp_invite_max_recipients]:
            ok = await send_email(
                to=a["email"],
                subject=t(lang, "email.invite_subject", title=booking.get("title") or meeting.display_title),
                html=_simple_email(
                    t(lang, "email.greeting"),
                    t(lang, "email.invite_intro", title=booking.get("title") or meeting.display_title),
                ),
                reply_to=meeting.owner_email or settings.invite_reply_to or None,
                attachments=[
                    {"filename": "agenda.pdf", "content": base64.b64encode(agenda_pdf).decode("ascii"), "content_type": "application/pdf"},
                    {"filename": "invite.ics", "content": base64.b64encode(ics_text.encode("utf-8")).decode("ascii"), "content_type": "text/calendar"},
                ],
            )
            results["invites"].append({"email": a["email"], "ok": ok})

    session.status = "published"
    session.published_at = utcnow()
    session.publish_version = version
    db.add(
        MeetppOutput(
            id=str(ULID()),
            session_id=session.id,
            kind="email",
            version=version,
            recipients_json=json.dumps(results),
            results_json=json.dumps(results),
        )
    )
    db.commit()
    log.info(
        "MEETPP_OUTPUT sid=%s version=%s minutes_sent=%s invites_sent=%s next_meeting=%s",
        session.id, version, len(results["minutes"]), len(results["invites"]),
        next_meeting.id if next_meeting else None,
    )
    return {
        "version": version,
        "uid": uid,
        "next_meeting_id": next_meeting.id if next_meeting else None,
        "recurring": bool(meeting.recurrence_rule),
        "results": results,
    }


async def prepare_outputs(db: Session, session: MeetppSession) -> dict:
    """Render the download outputs (minutes PDF, agenda PDF, .ics) without
    sending any e-mail. Used by the end-of-meeting modal so the chair can
    download everything once finalisation is done. Rendering runs off the
    event loop."""
    import asyncio

    meeting = db.get(Meeting, session.meeting_id)
    if meeting is None:
        raise ValueError("meeting not found")
    version = max(1, session.publish_version)
    booking = parse_booking(session, meeting)
    join_url = f"{settings.public_url}/{booking.get('room') or meeting.room_name}"
    lang = session.language

    minutes_html = render.render_minutes_html(db, session)
    agenda_html = render.render_agenda_html(db, session, booking)
    minutes_pdf = await asyncio.to_thread(render.html_to_pdf, minutes_html)
    agenda_pdf = await asyncio.to_thread(render.html_to_pdf, agenda_html)

    uid = f"meetpp-{session.series_id}-{datetime.now(timezone.utc).strftime('%Y%m%d')}@meet.witysk.org"
    required = (
        db.query(MeetppAttendee).filter_by(session_id=session.id, required_next=True).all()
    )
    attendees = (
        [{"name": a.display_name, "email": a.email} for a in required if a.email]
        or _attendee_emails(db, session)
    )
    for r in booking.get("recipients") or []:
        if isinstance(r, dict) and r.get("email"):
            attendees.append({"name": r.get("name") or r["email"], "email": r["email"]})
    seen: set[str] = set()
    attendees = [a for a in attendees if not (a["email"] in seen or seen.add(a["email"]))]

    dtstart = _parse_iso(booking.get("iso")) or utcnow()
    dtend = dtstart + timedelta(minutes=int(booking.get("duration_min") or 60))
    next_agenda = []
    if session.final_json:
        try:
            next_agenda = (json.loads(session.final_json) or {}).get("next_agenda") or []
        except ValueError:
            next_agenda = []
    agenda_text = "\n".join(f"{i+1}. {it.get('title')}" for i, it in enumerate(next_agenda))
    ics_text = ics_invite(
        uid=uid,
        sequence=session.publish_version,
        summary=booking.get("title") or meeting.display_title,
        join_url=join_url,
        dtstart=dtstart,
        dtend=dtend,
        organizer_name=meeting.owner_name,
        organizer_email=meeting.owner_email,
        attendees=attendees,
        description_text=(agenda_text + "\n\n" if agenda_text else "") + f"Join: {join_url}",
    )

    minutes_out = _write_output(db, session, "minutes_pdf", "minutes.pdf", minutes_pdf, version)
    agenda_out = _write_output(db, session, "agenda_pdf", "agenda.pdf", agenda_pdf, version)
    ics_out = _write_output(db, session, "ics", "invite.ics", ics_text.encode("utf-8"), version)
    db.commit()
    _ = (lang, required)
    return {
        "outputs": [
            {"id": minutes_out.id, "kind": "minutes_pdf", "filename": "minutes.pdf"},
            {"id": agenda_out.id, "kind": "agenda_pdf", "filename": "agenda.pdf"},
            {"id": ics_out.id, "kind": "ics", "filename": "invite.ics"},
        ]
    }


def list_outputs(db: Session, session: MeetppSession) -> dict:
    rows = (
        db.query(MeetppOutput)
        .filter(MeetppOutput.session_id == session.id, MeetppOutput.kind.in_(("minutes_pdf", "agenda_pdf", "ics")))
        .order_by(MeetppOutput.created_at.desc())
        .all()
    )
    latest: dict[str, MeetppOutput] = {}
    for r in rows:
        latest.setdefault(r.kind, r)
    outputs = [
        {
            "id": o.id,
            "kind": o.kind,
            "filename": o.filename,
            "download_url": f"/api/v1/meetpp/sessions/{session.id}/outputs/{o.id}",
        }
        for o in latest.values()
    ]
    kinds = {o["kind"] for o in outputs}
    finalised = session.status in ("review", "published")
    return {
        "status": session.status,
        "finalised": finalised,
        "ready": {"minutes_pdf", "agenda_pdf", "ics"}.issubset(kinds),
        "outputs": outputs,
        "auto_sent": session.status == "published",
        "recurring": bool((db.get(Meeting, session.meeting_id).recurrence_rule) if db.get(Meeting, session.meeting_id) else False),
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _simple_email(greeting: str, body: str) -> str:
    return (
        f"<div style='font-family:sans-serif;color:#111827'>"
        f"<p>{greeting}</p><p>{body}</p>"
        f"<p style='color:#6b7280;font-size:12px'>Generated with Meet++</p></div>"
    )

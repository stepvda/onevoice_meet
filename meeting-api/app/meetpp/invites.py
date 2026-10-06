"""Review draft, publishing and distribution (contract §2.3).

Publish = render the meeting report PDF (and the next-meeting agenda PDF when
a next meeting is set) through `app.meetpp.report`, write the ICS invitation,
e-mail the report and the invitations (Resend), snapshot the minutes, delete
the session's audio store and log every output. Nothing is sent before the
chair publishes.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import shutil
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.meetpp import compose, export as export_mod, util
from app.meetpp.models import (
    MeetppAction,
    MeetppAttendee,
    MeetppMinutesVersion,
    MeetppOutput,
    MeetppRoster,
    MeetppSession,
)
from app.models import Meeting
from app.services.email import send_email
from app.services.ics import ics_invite

log = logging.getLogger("app.meetpp")


class ReportUnavailable(RuntimeError):
    pass


def report_module():
    try:
        from app.meetpp import report
    except Exception as exc:  # noqa: BLE001
        raise ReportUnavailable(f"the report renderer (app.meetpp.report) is not available: {exc}") from exc
    return report


async def render_report(export: dict) -> bytes:
    report = report_module()
    return await asyncio.to_thread(report.render_meeting_report, export)


async def render_agenda(export: dict) -> bytes:
    report = report_module()
    return await asyncio.to_thread(report.render_agenda, export)


# ─── Review draft ───────────────────────────────────────────────────────────


def _rrule_parts(rrule: str | None) -> tuple[str, int] | None:
    if not rrule:
        return None
    m_freq = re.search(r"FREQ=(\w+)", rrule)
    if not m_freq:
        return None
    m_int = re.search(r"INTERVAL=(\d+)", rrule)
    return m_freq.group(1).upper(), int(m_int.group(1)) if m_int else 1


def next_occurrence(meeting: Meeting, after: datetime) -> datetime | None:
    parts = _rrule_parts(meeting.recurrence_rule)
    base = util.aware(meeting.scheduled_at)
    if not parts or base is None:
        return None
    freq, interval = parts
    step = {"DAILY": timedelta(days=interval), "WEEKLY": timedelta(weeks=interval), "MONTHLY": timedelta(days=30 * interval)}.get(
        freq, timedelta(weeks=interval)
    )
    cur = base
    guard = 0
    while cur <= after and guard < 2000:
        cur += step
        guard += 1
    return cur


def _people_with_email(db: Session, session: MeetppSession) -> list[dict]:
    out: dict[str, dict] = {}
    for a in db.query(MeetppAttendee).filter_by(session_id=session.id).all():
        if a.email:
            out.setdefault(a.email.lower(), {"name": a.display_name, "email": a.email})
    for r in db.query(MeetppRoster).filter_by(series_id=session.series_id, active=True).all():
        if r.email:
            out.setdefault(r.email.lower(), {"name": r.display_name, "email": r.email})
    return list(out.values())


def default_review(db: Session, session: MeetppSession) -> dict:
    meeting = db.get(Meeting, session.meeting_id)
    final = util.loads(session.final_json, {})
    nxt = next_occurrence(meeting, util.now()) if meeting else None
    proposal = final.get("next_meeting_proposal") or {}
    if nxt is None and proposal.get("iso"):
        nxt = util.parse_dt(proposal["iso"])
    everyone = _people_with_email(db, session)
    required = [
        {"name": a.display_name, "email": a.email}
        for a in db.query(MeetppAttendee).filter_by(session_id=session.id, required_next=True).all()
        if a.email
    ]
    return {
        "next_meeting": {
            "date_iso": util.iso(nxt),
            "duration_min": (meeting.duration_minutes if meeting and meeting.duration_minutes else 60),
            "room": "same",
        },
        "next_agenda": [
            {"title": it.get("title"), "body": it.get("body")} for it in final.get("next_agenda") or [] if isinstance(it, dict)
        ],
        "recipients": {"report": everyone, "invite": required or everyone},
        "distribution": {"send_report": True, "send_invites": nxt is not None, "attach_snapshots": True, "include_transcript": False},
    }


def review_draft(db: Session, session: MeetppSession) -> dict:
    stored = util.loads(session.review_json, {})
    draft = default_review(db, session)
    for key in ("next_meeting", "recipients", "distribution"):
        if isinstance(stored.get(key), dict):
            draft[key] = {**draft[key], **stored[key]}
    if isinstance(stored.get("next_agenda"), list):
        draft["next_agenda"] = stored["next_agenda"]
    return draft


def clean_review(body: dict) -> dict:
    def _people(items) -> list[dict]:
        out = []
        for x in items or []:
            if isinstance(x, dict) and x.get("email") and "@" in str(x["email"]):
                out.append({"name": util.truncate(x.get("name"), 200) or str(x["email"]), "email": str(x["email"]).strip()[:300]})
        return out[: settings.meetpp_invite_max_recipients]

    nm = body.get("next_meeting") if isinstance(body.get("next_meeting"), dict) else {}
    dist = body.get("distribution") if isinstance(body.get("distribution"), dict) else {}
    rec = body.get("recipients") if isinstance(body.get("recipients"), dict) else {}
    when = util.parse_dt(nm.get("date_iso"))
    duration = nm.get("duration_min")
    try:
        duration = max(5, min(24 * 60, int(duration))) if duration not in (None, "") else 60
    except (TypeError, ValueError):
        duration = 60
    return {
        "next_meeting": {"date_iso": util.iso(when), "duration_min": duration, "room": "new" if nm.get("room") == "new" else "same"},
        "next_agenda": [
            {"title": util.truncate(x.get("title"), 300), "body": util.truncate(x.get("body"), 4000)}
            for x in body.get("next_agenda") or []
            if isinstance(x, dict) and str(x.get("title") or "").strip()
        ][:40],
        "recipients": {"report": _people(rec.get("report")), "invite": _people(rec.get("invite"))},
        "distribution": {
            "send_report": bool(dist.get("send_report", True)),
            "send_invites": bool(dist.get("send_invites", False)),
            "attach_snapshots": bool(dist.get("attach_snapshots", True)),
            "include_transcript": bool(dist.get("include_transcript", False)),
        },
    }


# ─── Outputs ────────────────────────────────────────────────────────────────


def _outputs_dir(session: MeetppSession) -> Path:
    p = Path(settings.meetpp_data_dir) / session.id / "outputs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_output(db: Session, session: MeetppSession, kind: str, filename: str, data: bytes, version: int, recipients=None, results=None) -> MeetppOutput:
    path = _outputs_dir(session) / f"v{version}-{filename}"
    path.write_bytes(data)
    row = MeetppOutput(
        id=util.ulid(),
        session_id=session.id,
        kind=kind,
        version=version,
        path=str(path),
        filename=filename,
        recipients_json=util.dumps(recipients or []),
        results_json=util.dumps(results) if results is not None else None,
    )
    db.add(row)
    return row


def output_dto(o: MeetppOutput) -> dict:
    return {
        "id": o.id,
        "kind": o.kind,
        "version": o.version,
        "filename": o.filename,
        "created_at": util.iso(o.created_at),
        "url": f"/api/v1/meetpp/sessions/{o.session_id}/outputs/{o.id}" if o.path else None,
        "recipients": util.loads(o.recipients_json, []),
        "results": util.loads(o.results_json, None) if o.results_json else None,
    }


def list_outputs(db: Session, session: MeetppSession) -> list[dict]:
    rows = db.query(MeetppOutput).filter_by(session_id=session.id).order_by(MeetppOutput.created_at.desc()).all()
    return [output_dto(o) for o in rows]


def delete_audio(session_id: str) -> bool:
    base = Path(settings.meetpp_data_dir) / session_id / "audio"
    if not base.exists():
        return False
    shutil.rmtree(base, ignore_errors=True)
    return True


def _email_html(greeting: str, paragraphs: list[str], link: str | None = None, link_label: str | None = None) -> str:
    body = "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
    if link:
        body += f"<p><a href='{escape(link)}'>{escape(link_label or link)}</a></p>"
    return (
        "<div style='font-family:sans-serif;color:#111827;max-width:560px'>"
        f"<p>{escape(greeting)}</p>{body}"
        "<p style='color:#6b7280;font-size:12px'>Generated with Meet++</p></div>"
    )


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "meeting"


async def publish(db: Session, session: MeetppSession) -> dict:
    """Render, invite, send, snapshot, delete audio. Re-publishing writes a
    new version and sends again."""
    meeting = db.get(Meeting, session.meeting_id)
    if meeting is None:
        raise ValueError("meeting not found")
    report = report_module()  # fail early with a clear error
    review = review_draft(db, session)
    version = int(session.publish_version or 0) + 1
    # Proposed actions are accepted by publishing; open ones are carried.
    for a in db.query(MeetppAction).filter_by(session_id=session.id, status="proposed").all():
        a.status = "open"
    db.flush()
    stored = util.loads(session.review_json, {})
    stored.update({k: review[k] for k in ("next_meeting", "next_agenda", "recipients", "distribution")})
    session.review_json = util.dumps(stored)
    db.flush()

    data = export_mod.build_export(db, session)
    stamp = util.aware(session.started_at or session.created_at).strftime("%Y-%m-%d")
    base = f"{_safe_name(meeting.display_title)}-{stamp}"
    report_pdf = await asyncio.to_thread(report.render_meeting_report, data)
    outputs = [_write_output(db, session, "report_pdf", f"{base}-report.pdf", report_pdf, version)]

    nm = review["next_meeting"]
    when = util.parse_dt(nm.get("date_iso"))
    agenda_pdf = None
    ics_text = None
    next_meeting_id = None
    if when is not None:
        agenda_pdf = await asyncio.to_thread(report.render_agenda, data)
        outputs.append(_write_output(db, session, "agenda_pdf", f"{base}-next-agenda.pdf", agenda_pdf, version))
        room = meeting.room_name
        if nm.get("room") == "new":
            nxt = Meeting(
                id=util.ulid(),
                room_name=f"{meeting.room_name[:40]}-{util.ulid()[-6:].lower()}",
                display_title=meeting.display_title,
                owner_user_id=meeting.owner_user_id,
                owner_email=meeting.owner_email,
                owner_name=meeting.owner_name,
                scheduled_at=when,
                duration_minutes=int(nm.get("duration_min") or 60),
                meetpp_series_id=session.series_id,
            )
            db.add(nxt)
            db.flush()
            room, next_meeting_id = nxt.room_name, nxt.id
        join_url = f"{settings.public_url}/{room}"
        agenda_text = "\n".join(f"{i}. {it['title']}" for i, it in enumerate(review["next_agenda"], start=1))
        ics_text = ics_invite(
            uid=f"meetpp-{session.series_id}-{when.strftime('%Y%m%dT%H%M')}@meet.witysk.org",
            sequence=version - 1,
            summary=meeting.display_title,
            join_url=join_url,
            dtstart=when,
            dtend=when + timedelta(minutes=int(nm.get("duration_min") or 60)),
            organizer_name=meeting.owner_name,
            organizer_email=meeting.owner_email,
            attendees=review["recipients"]["invite"],
            description_text=(agenda_text + "\n\n" if agenda_text else "") + f"Join: {join_url}",
        )
        outputs.append(_write_output(db, session, "ics", "invite.ics", ics_text.encode("utf-8"), version, review["recipients"]["invite"]))

    results: list[dict] = []
    dist = review["distribution"]
    reply_to = meeting.owner_email or settings.invite_reply_to or None
    if dist.get("send_report"):
        for r in review["recipients"]["report"]:
            ok = await send_email(
                to=r["email"],
                subject=f"Meeting report: {meeting.display_title}",
                html=_email_html(
                    f"Dear {r['name']},",
                    [f"The report of {meeting.display_title} ({stamp}) is attached: attendance, decisions, follow-up actions and the minutes."],
                ),
                reply_to=reply_to,
                attachments=[{"filename": f"{base}-report.pdf", "content": base64.b64encode(report_pdf).decode("ascii"), "content_type": "application/pdf"}],
            )
            results.append({"kind": "report", "email": r["email"], "ok": bool(ok)})
    if dist.get("send_invites") and ics_text is not None:
        for r in review["recipients"]["invite"]:
            atts = [{"filename": "invite.ics", "content": base64.b64encode(ics_text.encode("utf-8")).decode("ascii"), "content_type": "text/calendar"}]
            if agenda_pdf:
                atts.insert(0, {"filename": f"{base}-next-agenda.pdf", "content": base64.b64encode(agenda_pdf).decode("ascii"), "content_type": "application/pdf"})
            ok = await send_email(
                to=r["email"],
                subject=f"Invitation: {meeting.display_title} — {when.strftime('%d/%m/%Y %H:%M')} UTC",
                html=_email_html(f"Dear {r['name']},", [f"You are invited to {meeting.display_title}. The agenda is attached."], f"{settings.public_url}/{meeting.room_name}", "Join the meeting"),
                reply_to=reply_to,
                attachments=atts,
            )
            results.append({"kind": "invite", "email": r["email"], "ok": bool(ok)})
    if results:
        db.add(
            MeetppOutput(
                id=util.ulid(), session_id=session.id, kind="email", version=version,
                recipients_json=util.dumps([r["email"] for r in results]), results_json=util.dumps(results),
            )
        )
    db.add(MeetppMinutesVersion(session_id=session.id, version=version, markdown=data["minutes"]["markdown"]))
    session.status = "published"
    session.published_at = util.now()
    session.publish_version = version
    db.commit()
    audio_deleted = delete_audio(session.id)
    log.info(
        "MEETPP_REPORT sid=%s version=%s outputs=%s emails=%s audio_deleted=%s",
        session.id, version, [o.kind for o in outputs], len(results), audio_deleted,
    )
    return {
        "published_at": util.iso(session.published_at),
        "version": version,
        "outputs": [output_dto(o) for o in outputs],
        "email_results": results,
        "next_meeting_id": next_meeting_id,
    }


def minutes_markdown(db: Session, session: MeetppSession) -> str:
    return compose.minutes_markdown(db, session)


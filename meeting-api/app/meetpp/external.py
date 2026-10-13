"""Meet++ prepared from One Voice Mail (docs/MAIL_INTEGRATION.md; Mail's
contract is its docs/meet-integration.md §4.2).

Mail pushes one sitting at a time (`PUT /meetings/{mid}/meetpp/external`):
its agenda, decisions, actions and roster, keyed by Mail's ids (`external_ref`
on sections, decisions and actions; the session's `external_ref` is Mail's id
of the sitting). The series is the one with `external_ref = "mail:<series_ref>"`.
Everything goes through the existing machinery — the outline replace, the
series action counters, the roster and expected attendees — so a prepared
session behaves like one set up in Meet.

  - setup: the agenda points follow Mail's list (a point edited in Meet —
    `locked` — keeps its text); decisions are upserted by ref under their
    point (pending unless Mail says decided) and Mail decisions no longer
    listed go; Mail actions are upserted by ref as previous actions of the
    series (origin "mail") and open ones Mail no longer lists leave the
    sitting; the roster is upserted by e-mail.
  - running / paused: additive — new points, decisions, actions and roster
    members are added; titles and bodies of items nobody edited in Meet follow
    Mail (a decision only while still pending); nothing is removed.
  - finalising / review / published / aborted: 409 `meetpp_closed`.

`result()` reports the outcome in Mail's terms (GET …/external) and
`send_next_agenda()` hands the draft agenda of the next sitting to Mail.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import AuthUser
from app.config import settings
from app.meetpp import bus, compose, governance, ops, outline as outline_mod, util
from app.meetpp.models import (
    MeetppAction,
    MeetppActionReport,
    MeetppAttendee,
    MeetppBallot,
    MeetppDecision,
    MeetppRoster,
    MeetppSeries,
    MeetppSession,
    MeetppVote,
)
from app.models import Meeting
from app.services import mail_webhook

log = logging.getLogger("app.meetpp")

SOURCE = "mail"
CLOSED_STATUSES = ("finalising", "review", "published", "aborted")
# Another sitting in one of these blocks preparing this one (one live session per room).
BUSY_STATUSES = ("running", "paused", "finalising")
DECIDED = ("adopted", "rejected", "withdrawn")
MAIL_ACTION_STATUSES = ("open", "in_progress", "done", "cancelled")
MAX_ITEMS = 200


class ExternalError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ─── Request bodies (Mail's MeetppSyncPayload) ──────────────────────────────


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AgendaIn(_In):
    ref: str = Field(min_length=1, max_length=200)
    title: str = ""
    body: str | None = None


class DecisionIn(_In):
    ref: str = Field(min_length=1, max_length=200)
    agenda_ref: str | None = None
    title: str = ""
    resolution: str | None = None
    status: str = "pending"


class PersonIn(_In):
    name: str | None = None
    email: str | None = None


class ActionIn(_In):
    ref: str = Field(min_length=1, max_length=200)
    title: str = ""
    description: str | None = None
    assignees: list[PersonIn] = Field(default_factory=list)
    due: str | None = None
    status: str = "open"


class RosterIn(PersonIn):
    voting: bool = True


class OccurrenceIn(_In):
    ref: str = Field(min_length=1, max_length=200)
    starts_at: str | None = None


class SyncBody(_In):
    occurrence_ref: str = Field(min_length=1, max_length=200)
    series_ref: str = Field(min_length=1, max_length=190)
    title: str = ""
    starts_at: str | None = None
    duration_minutes: int | None = None
    meeting_type: str | None = None
    open_url: str | None = None
    agenda: list[AgendaIn] = Field(default_factory=list)
    decisions: list[DecisionIn] = Field(default_factory=list)
    actions: list[ActionIn] = Field(default_factory=list)
    roster: list[RosterIn] = Field(default_factory=list)
    next_occurrence: OccurrenceIn | None = None


class NextAgendaItem(_In):
    title: str = ""
    body: str | None = None


class NextAgendaBody(_In):
    agenda: list[NextAgendaItem] = Field(default_factory=list)


# ─── Helpers ────────────────────────────────────────────────────────────────


def info(session: MeetppSession) -> dict:
    return util.loads(session.external_json, {})


def _save_info(session: MeetppSession, data: dict) -> None:
    session.external_json = util.dumps(data)


def external_dto(session: MeetppSession, meeting: Meeting | None = None) -> dict | None:
    """`SessionMeta.external`: null unless the session was prepared from Mail."""
    if not session.external_ref:
        return None
    data = info(session)
    nxt = data.get("next_occurrence") if isinstance(data.get("next_occurrence"), dict) else None
    return {
        "source": (meeting.external_source if meeting is not None and meeting.external_source else SOURCE),
        "occurrence_ref": session.external_ref,
        "open_url": data.get("open_url"),
        "next_occurrence": {"ref": nxt.get("ref"), "starts_at": nxt.get("starts_at")} if nxt else None,
        "next_agenda_sent": bool(data.get("next_agenda_sent")),
    }


def _clean_url(value: str | None) -> str | None:
    """Only http(s) links are shown (the board renders it as a link)."""
    url = (value or "").strip()
    return url[:1000] if re.match(r"^https?://[^\s]+$", url) else None


def _email(value: str | None) -> str | None:
    email = (value or "").strip().lower()
    return email[:300] if re.fullmatch(r"[^@\s]+@[^@\s]+", email) else None


def _same_email(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


def _ref(value: str | None) -> str | None:
    return (value or "").strip()[:200] or None


def _delete_setup_session(db: Session, session: MeetppSession) -> None:
    """Another sitting's session that never started: the room offers the
    current sitting only (one live session per room)."""
    from app.meetpp.routes import _delete_session_rows

    sid = session.id
    _delete_session_rows(db, sid)
    db.flush()
    path = Path(settings.meetpp_data_dir) / sid
    if path.exists() and re.fullmatch(r"[0-9A-Z]{26}", sid):
        shutil.rmtree(path, ignore_errors=True)
    log.info("MEETPP_SESSION sid=%s meeting_id=%s event=external_replaced", sid, session.meeting_id)


def _series(db: Session, meeting: Meeting, user: AuthUser, body: SyncBody) -> MeetppSeries:
    key = f"mail:{body.series_ref.strip()}"
    series = (
        db.query(MeetppSeries)
        .filter(MeetppSeries.external_ref == key, MeetppSeries.owner_sub.in_({meeting.owner_user_id, user.sub}))
        .order_by(MeetppSeries.created_at)
        .first()
    )
    title = util.truncate(body.title, 300)
    if series is None:
        series = MeetppSeries(
            id=util.ulid(), owner_sub=meeting.owner_user_id, title=title or meeting.display_title,
            meeting_id=meeting.id, external_ref=key,
        )
        db.add(series)
        db.flush()
    elif title:
        series.title = title
    meeting.meetpp_series_id = series.id
    return series


def _new_session(db: Session, meeting: Meeting, series: MeetppSeries, user: AuthUser, occurrence: str) -> MeetppSession:
    session = MeetppSession(
        id=util.ulid(),
        meeting_id=meeting.id,
        series_id=series.id,
        created_by_user_id=user.sub,
        status="setup",
        template="agenda",
        mode="lead",
        language="en",
        settings_json=util.dumps({"show_public": False, "in_recordings": True, "timebox_nudges": True, "speak": True}),
        external_ref=occurrence,
    )
    db.add(session)
    db.flush()
    outline_mod.create_fixed_sections(db, session)
    log.info("MEETPP_SESSION sid=%s meeting_id=%s event=create source=mail occurrence=%s", session.id, meeting.id, occurrence)
    return session


# ─── Agenda ─────────────────────────────────────────────────────────────────


def _replace_agenda(
    db: Session, session: MeetppSession, items: list[AgendaIn], decisions: list[DecisionIn], changes: ops.Changes,
) -> None:
    # Decisions Mail still lists keep their record when their point goes
    # (the outline would delete a removed point's pending decisions); they
    # are put under their new point right after.
    listed = {r for r in (_ref(d.ref) for d in decisions) if r}
    if listed:
        db.query(MeetppDecision).filter(
            MeetppDecision.session_id == session.id, MeetppDecision.external_ref.in_(listed)
        ).update({MeetppDecision.section_id: None}, synchronize_session=False)
    o = outline_mod.load(db, session.id)
    by_ref = {s.external_ref: s for s in o.tops() if s.kind == "agenda" and s.external_ref}
    points: list[dict] = []
    refs: list[str] = []
    for it in items[:60]:
        ref, title = _ref(it.ref), util.truncate(it.title, 400)
        if not ref or not title or ref in refs:
            continue
        sec = by_ref.get(ref)
        point = {"id": sec.id if sec else None, "title": title, "body": util.truncate(it.body, 4000)}
        if sec is not None:
            if sec.locked:
                point["title"], point["body"] = sec.title, sec.body
            point["presenter"], point["timebox_minutes"] = sec.presenter, sec.timebox_minutes
            # Sub-points added in Meet stay with their point.
            point["subpoints"] = [{"id": c.id, "title": c.title, "body": c.body} for c in o.children.get(sec.id, [])]
        points.append(point)
        refs.append(ref)
    before = {s.id for s in o.flat}
    changed = outline_mod.replace_agenda(db, session, points, source=SOURCE)
    o = outline_mod.load(db, session.id)
    # replace_agenda orders the agenda points as given, so they pair with refs.
    for sec, ref in zip([s for s in o.tops() if s.kind == "agenda"], refs):
        sec.external_ref = ref
    after = {s.id for s in o.flat}
    changes.sections.update(after | (changed & after))
    changes.removed.extend({"kind": "section", "id": sid} for sid in sorted(before - after))


def _add_agenda(db: Session, session: MeetppSession, items: list[AgendaIn], changes: ops.Changes) -> None:
    o = outline_mod.load(db, session.id)
    by_ref = {s.external_ref: s for s in o.flat if s.external_ref}
    for it in items[:60]:
        ref, title = _ref(it.ref), util.truncate(it.title, 400)
        if not ref or not title:
            continue
        sec = by_ref.get(ref)
        if sec is not None:
            if not sec.locked and (sec.title != title or sec.body != util.truncate(it.body, 4000)):
                sec.title, sec.body = title, util.truncate(it.body, 4000)
                changes.sections.add(sec.id)
            continue
        try:
            sec = outline_mod.add_section(db, session, title=title, kind="agenda", source=SOURCE)
        except outline_mod.OutlineError:
            # Already on the agenda under that title (added in Meet): bind it.
            sec = next(
                (s for s in outline_mod.load(db, session.id).tops()
                 if s.kind == "agenda" and not s.external_ref
                 and (util.norm_name(s.title) == util.norm_name(title) or util.jaccard(s.title, title) >= 0.85)),
                None,
            )
            if sec is None:
                continue
        else:
            sec.body = util.truncate(it.body, 4000)
        sec.external_ref = ref
        by_ref[ref] = sec
        changes.sections.update(s.id for s in outline_mod.load(db, session.id).flat)


# ─── Decisions ──────────────────────────────────────────────────────────────


def _decisions(db: Session, session: MeetppSession, series: MeetppSeries, items: list[DecisionIn], changes: ops.Changes, *, setup: bool) -> None:
    o = outline_mod.load(db, session.id)
    section_by_ref = {s.external_ref: s for s in o.flat if s.external_ref}
    existing = {
        d.external_ref: d
        for d in db.query(MeetppDecision).filter(MeetppDecision.session_id == session.id, MeetppDecision.external_ref.isnot(None)).all()
    }
    seen: set[str] = set()
    for it in items[:MAX_ITEMS]:
        ref = _ref(it.ref)
        title = util.truncate(it.title, 300) or util.truncate(re.sub(r"^that\s+", "", it.resolution or "", flags=re.I), 120)
        if not ref or not title or ref in seen:
            continue
        seen.add(ref)
        status = it.status if it.status in DECIDED else "pending"
        section = section_by_ref.get(_ref(it.agenda_ref) or "")
        d = existing.get(ref)
        if d is None:
            series.decision_counter = int(series.decision_counter or 0) + 1
            d = MeetppDecision(
                id=util.ulid(),
                session_id=session.id,
                series_id=series.id,
                ref=f"D-{series.decision_counter}",
                section_id=section.id if section else None,
                title=title,
                resolution=util.truncate(it.resolution, 1200),
                status=status,
                # A record Mail already holds is not an AI guess.
                confirmed=status != "pending",
                origin=SOURCE,
                evidence_json="[]",
                external_ref=ref,
            )
            db.add(d)
        elif d.locked:
            continue
        elif setup:
            d.title, d.resolution, d.status = title, util.truncate(it.resolution, 1200), status
            d.section_id = section.id if section else None
            d.confirmed = status != "pending"
        elif d.status == "pending":
            # Live: the wording follows Mail until the meeting takes it up.
            d.title, d.resolution = title, util.truncate(it.resolution, 1200)
        else:
            continue
        changes.decisions.add(d.id)
        if d.section_id:
            changes.sections.add(d.section_id)
    if setup:
        for ref, d in existing.items():
            if ref not in seen and not d.locked:
                changes.removed.append({"kind": "decision", "id": d.id})
                ops.delete_decision(db, d)
    db.flush()


# ─── Actions ────────────────────────────────────────────────────────────────


def _assignees(db: Session, session: MeetppSession, people: list[PersonIn]) -> list[dict]:
    roster = db.query(MeetppRoster).filter_by(series_id=session.series_id).all()
    out: list[dict] = []
    for p in people[:10]:
        name, email = util.truncate(p.name, 200), _email(p.email)
        if not name and not email:
            continue
        member = next((r for r in roster if email and _same_email(r.email, email)), None)
        if member is not None:
            display, key = name or member.display_name, member.person_key
        elif name:
            display, key = governance.match_person(db, session, name)
        else:
            display, key = email, None
        out.append({"name": display or name or email, "person_key": key, "email": email})
    return out


def _outcome_pending(db: Session, a: MeetppAction) -> bool:
    """The action was raised or reported on at a sitting whose outcome Mail
    may not have taken yet: its status in Meet is newer than Mail's."""
    sids = {a.session_id} | {r.session_id for r in db.query(MeetppActionReport.session_id).filter_by(action_id=a.id).all()}
    return (
        db.query(MeetppSession.id)
        .filter(MeetppSession.id.in_(sids), MeetppSession.status.in_(("running", "paused", "finalising", "review")))
        .first()
        is not None
    )


def _set_status(a: MeetppAction, status: str) -> None:
    if a.status == status:
        return
    a.status = status
    if status == "done":
        a.completed_at = a.completed_at or util.now()
    elif status in ops.OPEN_ACTION_STATUSES:
        a.completed_at = None


def _actions(db: Session, session: MeetppSession, series: MeetppSeries, items: list[ActionIn], changes: ops.Changes, *, setup: bool) -> None:
    existing = {
        a.external_ref: a
        for a in db.query(MeetppAction).filter(MeetppAction.series_id == series.id, MeetppAction.external_ref.isnot(None)).all()
    }
    data = info(session)
    left = set(data.get("left_action_ids") or [])
    seen: set[str] = set()
    for it in items[:MAX_ITEMS * 2]:
        ref, title = _ref(it.ref), util.truncate(it.title, 300)
        if not ref or not title or ref in seen:
            continue
        seen.add(ref)
        status = it.status if it.status in MAIL_ACTION_STATUSES else "open"
        a = existing.get(ref)
        if a is None:
            # Done or cancelled in Mail: nothing to review at this sitting.
            if status not in ops.OPEN_ACTION_STATUSES:
                continue
            series.action_counter = int(series.action_counter or 0) + 1
            a = MeetppAction(
                id=util.ulid(),
                series_id=series.id,
                session_id=session.id,
                ref=f"A-{series.action_counter}",
                title=title,
                description=util.truncate(it.description, 4000),
                assignees_json=util.dumps(_assignees(db, session, it.assignees)),
                due_date=ops._due(it.due),
                status=status,
                origin=SOURCE,
                evidence_json="[]",
                external_ref=ref,
            )
            db.add(a)
            existing[ref] = a
        else:
            left.discard(a.id)
            if a.locked:
                changes.actions.add(a.id)
                continue
            a.title = title
            a.description = util.truncate(it.description, 4000)
            if setup:
                a.assignees_json = util.dumps(_assignees(db, session, it.assignees))
                a.due_date = ops._due(it.due)
                if not _outcome_pending(db, a):
                    _set_status(a, status)
        changes.actions.add(a.id)
    if setup:
        # Open Mail actions Mail no longer lists leave this sitting: removed
        # when they only exist for it, else hidden here (their record at an
        # earlier sitting stays as it was).
        for ref, a in existing.items():
            if ref in seen or a.origin != SOURCE or a.status not in ops.OPEN_ACTION_STATUSES:
                continue
            reported = db.query(MeetppActionReport.id).filter_by(action_id=a.id).first() is not None
            if a.session_id == session.id and not reported:
                db.query(MeetppActionReport).filter_by(action_id=a.id).delete(synchronize_session=False)
                db.delete(a)
            else:
                left.add(a.id)
            changes.removed.append({"kind": "action", "id": a.id})
    # A hidden action Mail lists again comes back (live too: that is additive).
    data["left_action_ids"] = sorted(left)
    _save_info(session, data)
    db.flush()


# ─── Roster ─────────────────────────────────────────────────────────────────


def _roster(db: Session, session: MeetppSession, series: MeetppSeries, people: list[RosterIn], changes: ops.Changes, *, setup: bool) -> None:
    rows = db.query(MeetppRoster).filter_by(series_id=series.id).all()
    touched: dict[str, MeetppRoster] = {}
    for p in people[:MAX_ITEMS]:
        email = _email(p.email)
        name = util.truncate(p.name, 200) or (email.split("@")[0] if email else None)
        if not name:
            continue
        r = next((x for x in rows if email and _same_email(x.email, email)), None)
        key = util.name_key(name)
        if r is None:
            same_key = next((x for x in rows if x.person_key == key), None)
            if same_key is not None and (not same_key.email or not email):
                r = same_key
            elif same_key is not None:
                # Two members with the same name: keep them apart.
                key = f"{key} <{email}>"[:200]
        if r is None:
            r = MeetppRoster(
                id=util.ulid(), series_id=series.id, person_key=key, display_name=name, email=email,
                voting=bool(p.voting), active=True,
                # Like members seeded from a previous report: established
                # (they decide who votes by default, runtime.upsert_roster).
                first_session_id=None,
            )
            db.add(r)
            rows.append(r)
        elif setup:
            r.voting = bool(p.voting)
            r.active = True
            if email and not r.email:
                r.email = email
            if r.person_key.startswith("name:"):
                # Not matched to a participant yet: Mail's name stands.
                r.display_name = name
        touched[r.person_key] = r
    db.flush()
    from app.meetpp.runtime import expected_attendees

    changes.attendees.update(expected_attendees(db, session))
    if setup:
        for a in db.query(MeetppAttendee).filter_by(session_id=session.id).all():
            r = touched.get(a.person_key)
            if r is None:
                continue
            a.voting = bool(r.voting)
            if not a.email and r.email:
                a.email = r.email
            if a.status == "not_registered":
                a.display_name = r.display_name
            changes.attendees.add(a.id)
    if changes.attendees:
        changes.quorum = True


# ─── PUT /meetings/{mid}/meetpp/external ────────────────────────────────────


async def sync(db: Session, meeting: Meeting, user: AuthUser, body: SyncBody) -> dict:
    occurrence = body.occurrence_ref.strip()
    sessions = (
        db.query(MeetppSession).filter_by(meeting_id=meeting.id).order_by(MeetppSession.created_at.desc()).all()
    )
    target = next((s for s in sessions if s.external_ref == occurrence), None)
    if target is not None and target.status in CLOSED_STATUSES:
        raise ExternalError(409, "meetpp_closed")
    if any(s is not target and s.status in BUSY_STATUSES for s in sessions):
        raise ExternalError(409, "meetpp_busy")
    series = _series(db, meeting, user, body)
    if target is not None and target.status == "setup" and target.series_id != series.id:
        # The sitting moved to another series in Mail (it became recurring):
        # prepared again from scratch in that series.
        target = None
    for s in sessions:
        if s is not target and s.status == "setup":
            _delete_setup_session(db, s)
    created = target is None
    session = target or _new_session(db, meeting, series, user, occurrence)
    series = db.get(MeetppSeries, session.series_id)
    setup = session.status == "setup"
    if setup and body.meeting_type in governance.MEETING_TYPES:
        series.meeting_type = body.meeting_type

    data = info(session)
    data.update(
        open_url=_clean_url(body.open_url),
        next_occurrence=(
            {"ref": body.next_occurrence.ref.strip(), "starts_at": body.next_occurrence.starts_at}
            if body.next_occurrence else None
        ),
        title=util.truncate(body.title, 300),
        starts_at=body.starts_at,
        duration_minutes=body.duration_minutes,
    )
    _save_info(session, data)

    changes = ops.Changes(session=True)
    if setup:
        _replace_agenda(db, session, body.agenda, body.decisions, changes)
    else:
        _add_agenda(db, session, body.agenda, changes)
    _decisions(db, session, series, body.decisions, changes, setup=setup)
    # The roster first: action assignees are matched to its members by e-mail.
    _roster(db, session, series, body.roster, changes, setup=setup)
    _actions(db, session, series, body.actions, changes, setup=setup)
    changes.sections.update(outline_mod.apply_skip_rules(db, session))
    if created:
        changes.sections.update(s.id for s in outline_mod.load(db, session.id).flat)
    changes.sections.discard("")
    await bus.publish_changes(db, session, changes)
    log.info(
        "MEETPP_EXTERNAL sid=%s meeting_id=%s occurrence=%s status=%s agenda=%d decisions=%d actions=%d roster=%d",
        session.id, meeting.id, occurrence, session.status, len(body.agenda), len(body.decisions), len(body.actions), len(body.roster),
    )
    return {"session_id": session.id, "status": session.status, "series_id": series.id}


# ─── GET /meetpp/sessions/{sid}/external ────────────────────────────────────


def _emails(db: Session, session: MeetppSession):
    """person_key → e-mail, from the attendees, the roster and the accounts."""
    from app.meetpp.runtime import _user_profile

    book: dict[str, str | None] = {}
    for a in db.query(MeetppAttendee).filter_by(session_id=session.id).all():
        if a.email:
            book.setdefault(a.person_key, a.email)
    for r in db.query(MeetppRoster).filter_by(series_id=session.series_id).all():
        if r.email:
            book.setdefault(r.person_key, r.email)

    def lookup(key: str | None) -> str | None:
        if not key:
            return None
        if key not in book:
            book[key] = _user_profile(db, key[4:]).get("email") if key.startswith("sub:") else None
        return book[key]

    return lookup


def _next_agenda(session: MeetppSession) -> list[dict]:
    data = info(session)
    if data.get("next_agenda_sent") and isinstance(data.get("next_agenda"), list):
        items = data["next_agenda"]
    else:
        review = util.loads(session.review_json, {})
        final = util.loads(session.final_json, {})
        items = review.get("next_agenda") if isinstance(review.get("next_agenda"), list) else final.get("next_agenda") or []
    return [
        {"title": util.truncate(it.get("title"), 300), "body": util.truncate(it.get("body"), 4000)}
        for it in items
        if isinstance(it, dict) and it.get("title")
    ]


def result(db: Session, session: MeetppSession) -> dict:
    o = outline_mod.load(db, session.id)

    def agenda_ref(section_id: str | None) -> str | None:
        s = o.by_id.get(section_id or "")
        if s is None:
            return None
        return s.external_ref or o.top(s).external_ref

    email_of = _emails(db, session)

    # Pending decisions that were never taken are left out (as in the report).
    decisions = [
        d for d in db.query(MeetppDecision).filter_by(session_id=session.id).order_by(MeetppDecision.created_at).all()
        if d.status != "pending"
    ]
    votes = {v.decision_id: v for v in db.query(MeetppVote).filter(MeetppVote.decision_id.in_([d.id for d in decisions] or [""])).all()}
    decision_rows = []
    for d in decisions:
        v = votes.get(d.id)
        decision_rows.append({
            "id": d.id,
            "ref": d.external_ref,
            "agenda_ref": agenda_ref(d.section_id),
            "title": d.title,
            "resolution": d.resolution or "",
            "how_taken": d.how_taken,
            "status": d.status,
            "decided_at": util.iso(d.decided_at),
            "vote": (
                {
                    "for": int(v.tally_for or 0),
                    "against": int(v.tally_against or 0),
                    "abstain": int(v.tally_abstain or 0),
                    "result": v.result or d.status,
                }
                if v is not None else None
            ),
        })

    reports = {r.action_id: r for r in db.query(MeetppActionReport).filter_by(session_id=session.id).all()}
    action_rows = []
    for a in ops.session_actions(db, session):
        report = reports.get(a.id)
        previous = ops.is_previous_action(a, session)
        topic = (report.section_id if report is not None else None) if previous else a.section_id
        action_rows.append({
            "id": a.id,
            "ref": a.external_ref,
            "agenda_ref": agenda_ref(topic),
            "title": a.title,
            "description": a.description or "",
            "assignees": [
                {"name": x.get("name") or "", "email": x.get("email") or email_of(x.get("person_key"))}
                for x in util.loads(a.assignees_json, []) if isinstance(x, dict)
            ],
            "due": a.due_date,
            # An AI action nobody confirmed yet is still an open action for Mail.
            "status": "open" if a.status == "proposed" else a.status,
            "completion_note": a.completion_note,
            "report_note": (report.note or None) if report is not None else None,
            "decision_id": a.decision_id,
        })

    attendance = [
        {
            "name": a.display_name,
            "email": a.email or email_of(a.person_key),
            # Expected but never joined (before finalisation marks them absent).
            "status": "absent" if a.status == "not_registered" else a.status,
        }
        for a in db.query(MeetppAttendee).filter_by(session_id=session.id).order_by(MeetppAttendee.display_name).all()
    ]
    composed = session.status in ("review", "published")
    return {
        "session": {
            "id": session.id,
            "status": session.status,
            "occurrence_ref": session.external_ref,
            "started_at": util.iso(session.started_at),
            "ended_at": util.iso(session.ended_at),
            "published_at": util.iso(session.published_at),
        },
        # The minutes as GET …/export.md gives them, once composed.
        "minutes_markdown": compose.minutes_markdown(db, session) if composed else "",
        "decisions": decision_rows,
        "actions": action_rows,
        "attendance": attendance,
        "next_agenda": _next_agenda(session),
        "next_agenda_sent": bool(info(session).get("next_agenda_sent")),
    }


# ─── POST /meetpp/sessions/{sid}/external/next-agenda ───────────────────────


async def send_next_agenda(db: Session, session: MeetppSession, meeting: Meeting, body: NextAgendaBody) -> dict:
    if not session.external_ref:
        raise ExternalError(409, "not prepared from Meetings")
    agenda = [
        {"title": util.truncate(it.title, 300), "body": util.truncate(it.body, 4000)}
        for it in body.agenda[:60]
        if util.truncate(it.title, 300)
    ]
    if not agenda:
        raise ExternalError(400, "the agenda is empty")
    data = info(session)
    data.update(next_agenda=agenda, next_agenda_sent=True, next_agenda_sent_at=util.iso(util.now()))
    _save_info(session, data)
    # The review shows (and publishes) what was sent.
    review = util.loads(session.review_json, {})
    review["next_agenda"] = agenda
    session.review_json = util.dumps(review)
    await bus.publish_changes(db, session, ops.Changes(session=True))
    mail_webhook.send_session_event(mail_webhook.NEXT_AGENDA_EVENT, session)
    log.info("MEETPP_EXTERNAL sid=%s event=next_agenda items=%d", session.id, len(agenda))
    return {"ok": True, "external": external_dto(session, meeting), "next_agenda": agenda}

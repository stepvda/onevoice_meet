"""Meet++ unit tests: ICS invitations, the operation validator/applier and the
phase controller."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.meetpp import ops, phases
from app.meetpp.models import (
    Base,
    MeetppAction,
    MeetppAgendaItem,
    MeetppAttendee,
    MeetppDecision,
    MeetppMinute,
    MeetppSeries,
    MeetppSession,
)
from app.services.ics import ics_invite


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _seed(db, template="agenda"):
    db.add(MeetppSeries(id="S1", owner_sub="42", title="Weekly Ops"))
    db.add(
        MeetppSession(
            id="SESS1",
            meeting_id="M1",
            series_id="S1",
            created_by_user_id="42",
            status="running",
            language="en",
            phase="discussion",
            template=template,
            state_version=3,
        )
    )
    db.add(
        MeetppAgendaItem(
            id="I1",
            session_id="SESS1",
            position=1,
            title="Vendor contract",
            status="active",
            source="user",
        )
    )
    db.add(MeetppAttendee(id="AT1", session_id="SESS1", person_key="user-1", display_name="Jan P."))
    db.commit()
    return db.get(MeetppSession, "SESS1")


def _ctx(db, session):
    return ops.ApplyContext(
        db=db,
        session=session,
        actor="ai",
        window_min=400,
        window_max=500,
        aliases={"P1": ("Jan P.", "user-1")},
    )


def test_ics_invite_has_request_sequence_and_attendee():
    start = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
    text = ics_invite(
        uid="meetpp-S1-20261010@meet.witysk.org",
        sequence=2,
        summary="Weekly Ops Sync",
        join_url="https://meet.witysk.org/amber-river-fox",
        dtstart=start,
        dtend=start + timedelta(hours=1),
        organizer_name="Stephane",
        organizer_email="chair@example.org",
        attendees=[{"name": "Jan P.", "email": "jan@example.org"}],
        description_text="Agenda",
    )
    assert "METHOD:REQUEST" in text
    assert "SEQUENCE:2" in text
    assert "ORGANIZER;CN=Stephane:mailto:chair@example.org" in text
    assert "ATTENDEE;CN=Jan P.;ROLE=REQ-PARTICIPANT" in text
    assert "DTSTART:20261010T080000Z" in text
    assert text.endswith("\r\n")


def test_ics_folds_on_octets():
    from app.services.ics import _fold

    long_line = "DESCRIPTION:" + "é" * 100
    folded = _fold(long_line)
    for line in folded.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


def test_action_add_requires_valid_evidence(db):
    session = _seed(db)
    ctx = _ctx(db, session)
    ops.apply_ops(
        ctx,
        [
            {"op": "action.add", "title": "Send the draft", "owner_alias": "P1", "due": "2099-01-01", "evidence": [410]},
            {"op": "action.add", "title": "No evidence", "evidence": [9999]},
        ],
    )
    actions = db.query(MeetppAction).all()
    assert len(actions) == 1
    assert actions[0].owner_name == "Jan P."
    assert actions[0].ref == "A-01"
    assert len(ctx.rejected) == 1


def test_action_add_rejects_near_duplicate(db):
    session = _seed(db)
    ctx = _ctx(db, session)
    payload = {"op": "action.add", "title": "please send the vendor contract draft to legal", "evidence": [410]}
    ops.apply_ops(ctx, [payload])
    ops.apply_ops(ctx, [dict(payload, title="please send the vendor contract draft to legal team")])
    assert db.query(MeetppAction).count() == 1


def test_locked_item_becomes_suggestion(db):
    session = _seed(db)
    minute = MeetppMinute(id="MN1", session_id="SESS1", agenda_item_id="I1", body_md="orig", locked=True)
    db.add(minute)
    db.commit()
    ctx = _ctx(db, session)
    ops.apply_ops(ctx, [{"op": "minutes.upsert", "item_id": "I1", "body_md": "changed"}])
    assert db.get(MeetppMinute, "MN1").body_md == "orig"
    assert ctx.applied[0].status == "suggested"


def test_decisions_number_per_series(db):
    session = _seed(db)
    ctx = _ctx(db, session)
    ops.apply_ops(
        ctx,
        [
            {"op": "decision.add", "item_id": "I1", "text": "Accept the revised SLA", "evidence": [410]},
            {"op": "decision.add", "item_id": "I1", "text": "Budget approved for Q4", "evidence": [411]},
        ],
    )
    refs = [d.ref for d in db.query(MeetppDecision).order_by(MeetppDecision.ref).all()]
    assert refs == ["D-01", "D-02"]


def test_llm_breaker_singleton_exists():
    """Guards the module-level circuit breaker instance (a missing singleton
    used to make every tick raise NameError)."""
    from app.meetpp import llm

    assert llm.circuit_state() in ("open", "closed")
    assert llm.provider() is None or hasattr(llm.provider(), "complete_json")


def test_suppress_clears_auto_at(db):
    import json

    session = _seed(db)
    session.mode = "lead"
    db.commit()
    phases.suppress(db, session, {"pid": "p1", "to": "agenda", "item_id": None, "auto_at": "2099-01-01T00:00:00Z", "confidence": 0.9})
    data = json.loads(session.proposal_json or "{}")
    assert data["auto_at"] is None
    assert "suppressed_until" in data


def test_person_key_is_shared():
    from app.meetpp.ops import _norm_person_key
    from app.meetpp.util import person_key

    assert person_key(None, "  Jan   P. ") == _norm_person_key("  Jan   P. ")
    assert person_key("user-7", "Jan") == "user-7"


def test_minutes_html_escapes_names(db):
    from app.meetpp import render

    session = _seed(db)
    db.add(MeetppAttendee(id="ATX", session_id="SESS1", person_key="x", display_name="<script>alert(1)</script>"))
    db.commit()
    html = render.render_minutes_html(db, session)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_meeting_has_meetpp_columns_and_series_for(db):
    """Regression: the Meeting model must map meetpp_series_id (a missing
    mapped column made POST .../meetpp/sessions return 500)."""
    from app.meetpp.routes import _series_for
    from app.models import Meeting

    m = Meeting(id="M9", room_name="room-9", display_title="T", owner_user_id="42")
    db.add(m)
    db.commit()
    assert m.meetpp_series_id is None

    class _U:
        sub = "42"

    series = _series_for(m, _U(), None, db)
    db.commit()
    assert m.meetpp_series_id == series.id


def test_import_agenda_populates_items(db):
    from app.meetpp import ingest as ingest_mod
    from app.meetpp.models import MeetppAgendaItem

    session = _seed(db)
    # start from an empty agenda for this session
    db.query(MeetppAgendaItem).filter_by(session_id=session.id).delete()
    db.commit()
    n = ingest_mod.import_agenda(
        db,
        session,
        {"items": [{"title": "Kick-off", "presenter": "Jan", "timebox_minutes": 10}, {"title": "Budget"}]},
    )
    assert n == 2
    items = db.query(MeetppAgendaItem).filter_by(session_id=session.id).order_by(MeetppAgendaItem.position).all()
    assert [i.title for i in items] == ["Kick-off", "Budget"]
    assert items[0].source == "pdf" and items[0].timebox_minutes == 10
    assert session.template == "agenda"


def test_phase_helpers():
    assert phases.phase_index("discussion") == 4
    assert phases.next_phase("opening") == "previous_actions"
    assert phases.prev_phase("closing") == "new_actions"
    from app.meetpp.locales import t

    assert t("en", "announce.start.title").startswith("Meet++")
    assert t("nl", "announce.start.title").startswith("Meet++")


def test_phase_announcement_for_goal_template(db):
    session = _seed(db, template="goal")
    session.phase = "agenda"
    db.commit()
    ann = phases.announcement(db, session, "agenda")
    # Goal-driven template uses the Goal wording.
    assert "goal" in ann["title"].lower()

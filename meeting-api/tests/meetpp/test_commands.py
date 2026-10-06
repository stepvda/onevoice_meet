"""Spoken commands of the chair (FDD v3.2) and the single home of the previous
actions (an agenda point that reviews them replaces "Previous actions")."""
from __future__ import annotations

import pytest

from app.db import SessionLocal
from app.meetpp import commands, ingest, ops, outline, util
from app.meetpp.models import MeetppAction, MeetppSection, MeetppSession

from .conftest import drain, get, join, make_session, query, say, sids, start

AGENDA = [
    {"title": "Approval of the minutes"},
    {"title": "Community garden"},
    {"title": "Budget 2027"},
]


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    commands._last.clear()
    yield
    commands._last.clear()


async def meeting(fakes) -> str:
    sid = make_session(agenda=AGENDA)
    await start(sid)
    await join(sid, "42", "Alice Moreau")  # the chair (meeting owner)
    await join(sid, "43", "Ben Hartley")
    return sid


@pytest.mark.parametrize("text,expected", [
    ("Go to the next agenda point.", ("next", None)),
    ("Go to nd the next agenda points.", ("next", None)),
    ("OK, next item please", ("next", None)),
    ("Let's move on to the next topic.", ("next", None)),
    ("Move to item 4.", ("goto", 4)),
    ("Let's go to agenda point number three", ("goto", 3)),
    ("I think we can end this meeting now.", ("end", None)),
    ("I declare the meeting adjourned", ("end", None)),
    ("Before we move to the next item, one thing.", None),
    ("Don't go to the next item yet", None),
    ("We can't end the meeting yet", None),
    ("We should finish the meeting report", None),
    ("The garden needs a new tank.", None),
])
def test_detect(text, expected):
    assert commands.detect(text) == expected


async def test_chair_saying_next_moves_with_undo_and_a_participant_does_not(fakes):
    sid = await meeting(fakes)
    ids = sids(sid)
    live = get(MeetppSession, sid).live_section_id
    await say(sid, "43", "Ben Hartley", "Shall we go to the next agenda point?")
    assert get(MeetppSession, sid).live_section_id == live
    await say(sid, "42", "Alice Moreau", "Go to the next agenda point.")
    assert get(MeetppSession, sid).live_section_id == ids["Approval of the minutes"]
    pos = fakes.bus.last("position")
    assert pos["by"] == "ai" and pos["undo_until"]
    await drain()
    assert "Heard:" in fakes.bus.last("announce")["subtitle"]
    # Said twice in a row (tier 1 and a repeat): one move only.
    await say(sid, "42", "Alice Moreau", "Next item please.")
    assert get(MeetppSession, sid).live_section_id == ids["Approval of the minutes"]


async def test_chair_moves_to_a_numbered_point(fakes):
    sid = await meeting(fakes)
    await say(sid, "42", "Alice Moreau", "Let's go to item 3, the budget.")
    assert get(MeetppSession, sid).live_section_id == sids(sid)["Budget 2027"]
    await drain()


async def test_end_this_meeting_asks_the_chair_to_confirm(fakes):
    sid = await meeting(fakes)
    await say(sid, "42", "Alice Moreau", "Thank you all, I think we can end this meeting now.")
    room, msg, dest = next((r, m, d) for r, m, d in fakes.bus.sent if m.get("type") == "end_request")
    assert dest == ["user-42"] and "end this meeting" in msg["heard"]
    assert get(MeetppSession, sid).status == "running"


async def test_agenda_point_reviewing_actions_is_their_only_home(fakes):
    sid = make_session(agenda=[{"title": "Actions from 30 September", "body": "The standing review."}, *AGENDA])
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        changes = ops.Changes()
        ingest.merge_actions(db, session, [
            {"title": "Ask two suppliers for a quote for the rainwater tank", "assignees": ["Sam Lee"], "status": "open"},
            {"title": "Circulate the watering rota", "assignees": ["Ben Hartley"], "status": "in_progress"},
        ], changes)
        outline.apply_skip_rules(db, session)
        db.commit()
        o = outline.load(db, sid)
        home = outline.previous_actions_home(o)
        a = db.query(MeetppAction).filter_by(session_id=sid).first()
        assert home.title == "Actions from 30 September"
        assert ops.action_section_id(db, session, a, o) == home.id
        dtos = {s.title: ops.section_dto(s, o, session, {}) for s in o.tops()}
    finally:
        db.close()
    assert get(MeetppSection, sids(sid)["Previous actions"]).status == "skipped"
    assert dtos["Actions from 30 September"]["previous_actions_home"] is True
    assert dtos["Previous actions"]["previous_actions_home"] is False


async def test_overlapping_actions_from_two_documents_are_merged(fakes):
    sid = make_session(agenda=AGENDA)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        changes = ops.Changes()
        first = ingest.merge_actions(db, session, [
            {"title": "Ask two suppliers for a quote for the rainwater tank", "assignees": ["Sam Lee"], "status": "open",
             "description": "Before winter."},
            {"title": "Circulate the watering rota", "assignees": ["Ben Hartley"], "status": "open"},
        ], changes)
        second = ingest.merge_actions(db, session, [
            # Same work, worded differently, same assignee; further along.
            {"title": "Quote for the rainwater tank from two suppliers", "assignees": ["Sam Lee"], "status": "in_progress",
             "progress_notes": "One quote received."},
            {"title": "Repaint the garden shed", "assignees": ["Robin Hale"], "status": "open"},
        ], changes)
        db.commit()
    finally:
        db.close()
    assert (first, second) == (2, 1)
    rows = {a.title: a for a in query(lambda db: db.query(MeetppAction).filter_by(session_id=sid).all())}
    assert len(rows) == 3
    tank = rows["Ask two suppliers for a quote for the rainwater tank"]
    assert tank.status == "in_progress" and tank.progress_notes == "One quote received." and tank.description == "Before winter."
    assert util.loads(tank.assignees_json, [])[0]["name"] == "Sam Lee"

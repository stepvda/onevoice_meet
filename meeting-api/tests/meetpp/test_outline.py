"""Outline: fixed sections, skip rules, numbering, Next/Back/Move, AI moves,
undo and suppression (contract §1, FDD §8.3)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.db import SessionLocal
from app.meetpp import outline, runtime as rt, util
from app.meetpp.models import MeetppAction, MeetppMinute, MeetppSection, MeetppSession

from .conftest import drain, get, make_session, query, sids, start

AGENDA = [
    {"title": "Approval of the minutes", "body": "Approve version 2."},
    {"title": "Community garden", "subpoints": [{"title": "Rainwater tank"}, {"title": "Municipal connection"}]},
    {"title": "Budget 2027", "timebox_minutes": 10},
]


def _titles(sid):
    db = SessionLocal()
    try:
        o = outline.load(db, sid)
        return [(o.numbers.get(s.id), s.title, s.kind, s.status) for s in o.flat]
    finally:
        db.close()


def test_agenda_template_fixed_sections_and_skip_rules():
    sid = make_session(agenda=AGENDA)
    rows = _titles(sid)
    assert [r[1] for r in rows] == [
        "Opening", "Previous actions", "Approval of the minutes", "Community garden", "Rainwater tank",
        "Municipal connection", "Budget 2027", "Any other business", "New actions", "Closing",
    ]
    numbers = {r[1]: r[0] for r in rows}
    assert numbers["Approval of the minutes"] == "1"
    assert numbers["Rainwater tank"] == "2.1"
    assert numbers["Municipal connection"] == "2.2"
    assert numbers["Budget 2027"] == "3"
    assert numbers["Opening"] is None and numbers["Any other business"] is None
    status = {r[1]: r[3] for r in rows}
    # No open series actions → previous actions skipped; AOB applies.
    assert status["Previous actions"] == "skipped"
    assert status["Any other business"] == "pending"


def test_goal_template():
    sid = make_session(template="goal")
    assert [r[2] for r in _titles(sid)] == ["opening", "goal", "deliverables", "next_steps", "planning", "closing"]


def test_aob_skipped_when_last_point_is_aob_and_previous_actions_unskipped():
    sid = make_session(agenda=AGENDA + [{"title": "Any other urgent matter for discussion?"}])
    status = {r[1]: r[3] for r in _titles(sid)}
    assert status["Any other business"] == "skipped"
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        db.add(MeetppAction(id=util.ulid(), series_id=session.series_id, session_id=sid, ref="A-1", title="Old", status="open", origin="pdf"))
        db.flush()
        outline.apply_skip_rules(db, session)
        db.commit()
    finally:
        db.close()
    status = {r[1]: r[3] for r in _titles(sid)}
    assert status["Previous actions"] == "pending"


def test_replace_agenda_keeps_ids_and_removes_points():
    sid = make_session(agenda=AGENDA)
    ids = sids(sid)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        outline.replace_agenda(db, session, [
            {"id": ids["Budget 2027"], "title": "Budget 2027 (draft)"},
            {"id": ids["Community garden"], "title": "Community garden", "subpoints": [{"id": ids["Rainwater tank"], "title": "Tank"}]},
        ])
        db.commit()
    finally:
        db.close()
    rows = _titles(sid)
    titles = [r[1] for r in rows]
    assert "Approval of the minutes" not in titles and "Municipal connection" not in titles
    assert get(MeetppSection, ids["Budget 2027"]).title == "Budget 2027 (draft)"
    numbers = {r[1]: r[0] for r in rows}
    assert numbers["Budget 2027 (draft)"] == "1" and numbers["Tank"] == "2.1"


async def test_next_back_move_and_section_closing(fakes):
    sid = make_session(agenda=AGENDA)
    ids = sids(sid)
    await start(sid)
    session = get(MeetppSession, sid)
    assert session.status == "running"
    assert session.live_section_id == ids["Opening"]
    assert fakes.bus.last("session")["state"] == "started"

    # Next skips the skipped "Previous actions" and closes Opening.
    res = await rt.chair_move(sid, "next")
    assert res["live_section_id"] == ids["Approval of the minutes"]
    assert get(MeetppSection, ids["Opening"]).status == "done"
    assert get(MeetppSection, ids["Previous actions"]).status == "skipped"
    pos = fakes.bus.last("position")
    assert pos["by"] == "chair" and pos["prev_section_id"] == ids["Opening"] and pos["undo_until"] is None
    assert pos["version"] == res["version"]
    # position first, then the state delta with the same version.
    types = [m["type"] for _r, m, _d in fakes.bus.sent]
    i = len(types) - 1 - types[::-1].index("position")
    state = fakes.bus.sent[i + 1][1]
    assert state["type"] == "state" and state["version"] == pos["version"]
    assert state["delta"]["session"]["live_section_id"] == ids["Approval of the minutes"]
    assert {s["id"] for s in state["delta"]["sections"]} >= {ids["Opening"], ids["Approval of the minutes"]}
    assert (sid, ids["Opening"], {"delay": 0}) in fakes.scheduled
    await drain()
    ann = fakes.bus.last("announce")
    assert ann["kind"] == "position" and ann["title"] == "1 · Approval of the minutes"
    assert ann["audio_url"] == f"/api/v1/meetpp/tts/{'ab' * 16}.ogg"
    assert ("tts", "Moving on to item 1: Approval of the minutes.") in fakes.agent.calls

    # Chair moves suppress AI moves for 60 s.
    assert outline.ai_moves_suppressed(get(MeetppSession, sid))

    await rt.chair_move(sid, "next")
    assert get(MeetppSession, sid).live_section_id == ids["Community garden"]
    # Back reopens the previous section; the one we left returns to pending.
    db = SessionLocal()
    try:
        m = outline.minute_for(db, db.get(MeetppSession, sid), ids["Approval of the minutes"])
        m.status, m.narrative_md = "composed", "Composed text."
        db.commit()
    finally:
        db.close()
    await rt.chair_move(sid, "back")
    session = get(MeetppSession, sid)
    assert session.live_section_id == ids["Approval of the minutes"]
    assert get(MeetppSection, ids["Approval of the minutes"]).status == "live"
    assert get(MeetppSection, ids["Community garden"]).status == "pending"
    minute = query(lambda db: db.query(MeetppMinute).filter_by(session_id=sid, section_id=ids["Approval of the minutes"]).first())
    assert minute.status == "notes" and minute.narrative_md == "Composed text."

    # Move into a sub-point, then on to the next point: the point and its
    # remaining sub-points are done and the point is composed.
    await rt.chair_move(sid, "move", ids["Rainwater tank"])
    assert get(MeetppSection, ids["Rainwater tank"]).status == "live"
    await rt.chair_move(sid, "move", ids["Budget 2027"])
    assert get(MeetppSection, ids["Rainwater tank"]).status == "done"
    assert get(MeetppSection, ids["Municipal connection"]).status == "done"
    assert get(MeetppSection, ids["Community garden"]).status == "done"
    assert (sid, ids["Community garden"], {"delay": 0}) in fakes.scheduled
    await drain()


async def test_elapsed_accumulates_across_reopen(fakes):
    sid = make_session(agenda=AGENDA)
    ids = sids(sid)
    await start(sid)
    db = SessionLocal()
    try:
        db.get(MeetppSection, ids["Opening"]).started_at = util.now() - timedelta(seconds=30)
        db.commit()
    finally:
        db.close()
    await rt.chair_move(sid, "next")
    opening = get(MeetppSection, ids["Opening"])
    assert 29 <= opening.elapsed_seconds <= 40
    first_run_end = opening.ended_at
    await rt.chair_move(sid, "back")
    opening = get(MeetppSection, ids["Opening"])
    assert opening.status == "live" and opening.ended_at is None
    # started_at is the start of the current live run; elapsed = finished runs.
    assert util.aware(opening.started_at) >= util.aware(first_run_end)
    db = SessionLocal()
    try:
        from app.meetpp import ops

        dto = next(s for s in ops.build_state(db, db.get(MeetppSession, sid))["sections"] if s["id"] == ids["Opening"])
    finally:
        db.close()
    assert 29 <= dto["elapsed_seconds"] <= 40
    # Pause stops the run (started_at null); resume starts a new run.
    meta = await rt.pause_session(sid, True)
    paused = get(MeetppSection, ids["Opening"])
    assert meta["status"] == "paused" and paused.started_at is None and paused.status == "live"
    await rt.pause_session(sid, False)
    assert get(MeetppSection, ids["Opening"]).started_at is not None
    await drain()


async def test_ai_move_undo_and_suppression(fakes):
    sid = make_session(agenda=AGENDA)
    ids = sids(sid)
    await start(sid)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        o = outline.load(db, sid)
        res = outline.move(db, session, o.by_id[ids["Approval of the minutes"]], by="ai")
        version = await rt._after_move(db, session, res, by="ai", compose_delay=11)
    finally:
        db.close()
    pos = fakes.bus.last("position")
    assert pos["by"] == "ai" and pos["undo_until"] and pos["version"] == version
    session = get(MeetppSession, sid)
    assert util.loads(session.undo_json, {})["from"] == ids["Opening"]
    # AI moves do not suppress further AI moves; the chair's undo does.
    assert not outline.ai_moves_suppressed(session)
    res = await rt.undo_move(sid)
    assert res["live_section_id"] == ids["Opening"]
    session = get(MeetppSession, sid)
    assert session.undo_json is None
    assert outline.ai_moves_suppressed(session)
    assert get(MeetppSection, ids["Opening"]).status == "live"
    assert get(MeetppSection, ids["Approval of the minutes"]).status == "pending"
    with pytest.raises(rt.PositionError) as err:
        await rt.undo_move(sid)
    assert err.value.status == 409
    await drain()


async def test_undo_window_expires(fakes):
    sid = make_session(agenda=AGENDA)
    ids = sids(sid)
    await start(sid)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        o = outline.load(db, sid)
        outline.move(db, session, o.by_id[ids["Approval of the minutes"]], by="ai")
        u = util.loads(session.undo_json, {})
        u["until"] = util.iso(util.now() - timedelta(seconds=1))
        session.undo_json = util.dumps(u)
        db.commit()
    finally:
        db.close()
    assert outline.undo_info(get(MeetppSession, sid)) is None
    with pytest.raises(rt.PositionError):
        await rt.undo_move(sid)
    await drain()


async def test_timebox_overrun_nudges_once(fakes):
    sid = make_session(agenda=AGENDA)
    ids = sids(sid)
    await start(sid)
    await rt.chair_move(sid, "move", ids["Budget 2027"])
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        db.get(MeetppSection, ids["Budget 2027"]).started_at = util.now() - timedelta(minutes=16)
        db.commit()
        assert outline.timebox_overrun(db, session) is not None
        assert outline.timebox_overrun(db, session) is None
    finally:
        db.close()
    await drain()

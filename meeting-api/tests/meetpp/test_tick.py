"""Interpretation ticks: ops, notes, topic hysteresis, advance, activations,
deltas, evidence/duplicate/locked handling and robustness (FDD §8)."""
from __future__ import annotations

from datetime import timedelta

from app.db import SessionLocal
from app.meetpp import compose, llm, outline, runtime as rt, util
from app.meetpp.models import (
    MeetppAction,
    MeetppActionReport,
    MeetppDecision,
    MeetppMinute,
    MeetppOp,
    MeetppSection,
    MeetppSegment,
    MeetppSession,
    MeetppVote,
)

from .conftest import drain, get, join, make_session, query, say, sids, start

AGENDA = [
    {"title": "Approval of the minutes"},
    {"title": "Community garden", "subpoints": [{"title": "Rainwater tank"}, {"title": "Municipal connection"}]},
    {"title": "Budget 2027"},
]


async def running(fakes, **kw) -> str:
    sid = make_session(agenda=kw.pop("agenda", AGENDA), **kw)
    await start(sid)
    await join(sid, "42", "Alice Moreau")
    await join(sid, "43", "Ben Hartley")
    return sid


def pid(sid: str, title: str) -> str:
    ids = sids(sid)
    target = ids[title]
    return next(k for k, v in ids.items() if k.startswith("S") and k[1:].isdigit() and v == target)


def ops_rows(sid: str, status: str | None = None) -> list[MeetppOp]:
    def q(db):
        rows = db.query(MeetppOp).filter_by(session_id=sid)
        if status:
            rows = rows.filter_by(status=status)
        return rows.all()

    return query(q)


async def test_tick_applies_ops_notes_topic_and_broadcasts_delta(fakes):
    sid = await running(fakes)
    ids = sids(sid)
    s1 = await say(sid, "42", "Alice Moreau", "Let's take the minutes of the last meeting.")
    s2 = await say(sid, "43", "Ben Hartley", "I move that they are approved. All in favour? Agreed.")
    assert fakes.bus.last("caption")["seq"] == s2 and fakes.bus.last("caption")["tier"] == 1
    S = pid(sid, "Approval of the minutes")
    fakes.llm.add("tick", {
        "topic": {"section": S, "confidence": 0.9},
        "advance": None,
        "ops": [
            {"op": "decision.add", "section": S, "title": "Approve the minutes of meeting #8",
             "resolution": "that the minutes of meeting #8 are approved", "how_taken": "Taken with the assent of both directors present.",
             "status": "adopted", "decided_at_seq": s2,
             "vote": {"method": "assent", "for": 2, "against": 0, "abstain": 0}, "evidence": [s1, s2], "unknown_field": 1},
            {"op": "action.add", "section": S, "title": "File the approved minutes", "assignees": ["Alice"], "due": "2026-10-20", "evidence": [s2]},
        ],
        "notes": [{"section": S, "text": "The chair put the minutes of meeting #8 to the meeting.", "evidence": [s1]}],
    })
    res = await rt.tick(sid)
    assert res["applied"] == 2 and res["notes"] == 1 and not res["rejected"]
    session = get(MeetppSession, sid)
    assert session.transcript_cursor == s2
    assert session.topic_section_id == ids["Approval of the minutes"]
    d = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).one())
    assert d.ref.startswith("D-") and d.status == "adopted" and d.section_id == ids["Approval of the minutes"]
    assert d.decided_at is not None and d.origin == "ai" and not d.confirmed
    vote = query(lambda db: db.query(MeetppVote).filter_by(decision_id=d.id).one())
    assert (vote.tally_for, vote.tally_against, vote.result) == (2, 0, "adopted")
    a = query(lambda db: db.query(MeetppAction).filter_by(session_id=sid).one())
    assert a.status == "proposed" and util.loads(a.assignees_json, [])[0] == {"name": "Alice Moreau", "person_key": "sub:42"}
    assert a.due_date == "2026-10-20"
    m = query(lambda db: db.query(MeetppMinute).filter_by(session_id=sid, section_id=ids["Approval of the minutes"]).one())
    assert util.loads(m.notes_json, [])[0]["evidence"] == [s1]
    # Segments of the window are tagged with the topic section.
    assert query(lambda db: db.query(MeetppSegment).filter_by(session_id=sid, seq=s2).one()).section_id == ids["Approval of the minutes"]
    msg = fakes.bus.last("state")
    assert msg["version"] == session.state_version
    assert [a["kind"] for a in msg["activations"]] == ["decision", "action", "topic"]
    assert msg["activations"][0] == {"kind": "decision", "tab": "decisions", "section_id": ids["Approval of the minutes"], "item_id": d.id, "prio": 5}
    assert {"session", "decisions", "actions", "minutes", "sections"} <= set(msg["delta"])
    assert msg["delta"]["decisions"][0]["vote"]["for"] == 2
    prompt = fakes.llm.prompts("tick")[0]
    assert f"[{s1}]" in prompt and "Alice Moreau" in prompt and "@" not in prompt.split("NEW")[0].split("MEMBERS")[1][:200]


async def test_evidence_duplicates_and_locked_items(fakes):
    sid = await running(fakes)
    old = await say(sid, "42", "Alice Moreau", "Earlier remark.", seconds_ago=30)
    fakes.llm.add("tick", {"topic": None, "ops": [], "notes": []})
    await rt.tick(sid)
    s1 = await say(sid, "42", "Alice Moreau", "We agree to buy the tank.")
    S = pid(sid, "Community garden")
    fakes.llm.add("tick", {
        "topic": {"section": S, "confidence": 0.9},
        "ops": [
            {"op": "decision.add", "section": S, "title": "Buy the rainwater tank", "status": "adopted", "evidence": [old]},
            {"op": "decision.add", "section": S, "title": "Buy the rainwater tank", "status": "adopted", "evidence": [9999]},
            {"op": "decision.add", "section": S, "title": "Buy the rainwater tank", "status": "adopted", "evidence": [old, s1]},
            {"op": "decision.add", "section": S, "title": "buy the Rainwater tank!", "status": "adopted", "evidence": [s1]},
            {"op": "frobnicate", "evidence": [s1]},
            {"op": "action.add", "section": "S99", "title": "Order the tank", "assignees": "Ben Hartley and Somebody New", "evidence": [s1]},
        ],
        "notes": [{"text": "No evidence note", "evidence": []}],
    })
    res = await rt.tick(sid)
    reasons = [r["reason"] for r in res["rejected"]]
    assert res["applied"] == 2
    assert any("new transcript line" in r for r in reasons)
    assert any("duplicate of D-" in r for r in reasons)
    assert any("unknown op" in r for r in reasons)
    rejected = ops_rows(sid, "rejected")
    assert any(r.op_type == "frobnicate" for r in rejected)
    assert all(r.payload_json and r.payload_json != "{}" for r in rejected)
    # Unknown section falls back to the topic section; unknown assignee kept as a guest.
    a = query(lambda db: db.query(MeetppAction).filter_by(session_id=sid).one())
    assert a.section_id == sids(sid)["Community garden"]
    assert [x["name"] for x in util.loads(a.assignees_json, [])] == ["Ben Hartley", "Somebody New"]
    assert util.loads(a.assignees_json, [])[1]["person_key"] is None

    # Locked items only receive suggestions.
    d = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).one())
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        from app.meetpp import ops

        ctx = ops.ApplyContext(db=db, session=session, actor="user:user-42")
        ops.apply_ops(ctx, [{"op": "decision.update", "id": d.id, "title": "Buy one 3,000 litre tank"}])
        db.commit()
    finally:
        db.close()
    assert get(MeetppDecision, d.id).locked
    s2 = await say(sid, "43", "Ben Hartley", "Actually let's say two tanks.")
    fakes.llm.add("tick", {"topic": {"section": S, "confidence": 0.9}, "ops": [
        {"op": "decision.update", "ref": d.ref, "title": "Buy two tanks", "evidence": [s2]}]})
    res = await rt.tick(sid)
    assert get(MeetppDecision, d.id).title == "Buy one 3,000 litre tank"
    assert any(r.status == "suggested" for r in ops_rows(sid))


async def test_long_text_truncated_not_rejected(fakes):
    sid = await running(fakes)
    s1 = await say(sid, "42", "Alice Moreau", "A long resolution follows.")
    fakes.llm.add("tick", {"ops": [{"op": "decision.add", "title": "T" * 500, "resolution": "that " + "x" * 2000, "status": "proposed", "evidence": [s1]}]})
    res = await rt.tick(sid)
    assert res["applied"] == 1
    d = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).one())
    assert len(d.title) <= 300 and len(d.resolution) <= 1200


async def test_topic_hysteresis(fakes):
    sid = await running(fakes)
    ids = sids(sid)
    S = pid(sid, "Budget 2027")
    s1 = await say(sid, "42", "Alice Moreau", "About the budget...")
    fakes.llm.add("tick", {"topic": {"section": S, "confidence": 0.7}})
    await rt.tick(sid)
    assert get(MeetppSession, sid).topic_section_id == ids["Opening"]
    await say(sid, "43", "Ben Hartley", "Yes the budget.")
    fakes.llm.add("tick", {"topic": {"section": S, "confidence": 0.7}})
    await rt.tick(sid)
    assert get(MeetppSession, sid).topic_section_id == ids["Budget 2027"]
    act = fakes.bus.last("state")["activations"]
    assert act == [{"kind": "topic", "tab": "agenda", "section_id": ids["Budget 2027"], "prio": 3}]
    # Below 0.6 never changes the topic.
    await say(sid, "43", "Ben Hartley", "Hmm.")
    fakes.llm.add("tick", {"topic": {"section": pid(sid, "Community garden"), "confidence": 0.5}})
    await rt.tick(sid)
    assert get(MeetppSession, sid).topic_section_id == ids["Budget 2027"]
    _ = s1


async def test_lead_mode_advance_moves_with_undo(fakes):
    sid = await running(fakes)
    ids = sids(sid)
    await say(sid, "42", "Alice Moreau", "Then on to item one, the minutes.")
    fakes.llm.add("tick", {"topic": {"section": pid(sid, "Approval of the minutes"), "confidence": 0.9},
                           "advance": {"to": pid(sid, "Approval of the minutes"), "confidence": 0.92, "reason": "Chair: on to item one"}})
    res = await rt.tick(sid)
    assert res["advance"] == ids["Approval of the minutes"]
    session = get(MeetppSession, sid)
    assert session.live_section_id == ids["Approval of the minutes"]
    pos = fakes.bus.last("position")
    assert pos["by"] == "ai" and pos["undo_until"]
    assert get(MeetppSection, ids["Opening"]).status == "done"
    # Composition waits for the undo window.
    assert (sid, ids["Opening"], {"delay": outline.AI_UNDO_SECONDS + 1}) in fakes.scheduled
    await drain()
    assert fakes.bus.last("announce")["title"] == "1 · Approval of the minutes"
    # Never backwards.
    await say(sid, "42", "Alice Moreau", "Back to the opening remarks.")
    fakes.llm.add("tick", {"topic": {"section": pid(sid, "Opening"), "confidence": 0.95},
                           "advance": {"to": pid(sid, "Opening"), "confidence": 0.95}})
    await rt.tick(sid)
    assert get(MeetppSession, sid).live_section_id == ids["Approval of the minutes"]


async def test_sure_cue_skips_only_points_already_discussed(fakes):
    # "Number two, the garden" straight from the opening: the minutes point
    # (no talk yet) may not be skipped by the AI — a proposal for the chair.
    sid = await running(fakes)
    ids = sids(sid)
    garden, minutes = pid(sid, "Community garden"), pid(sid, "Approval of the minutes")
    await say(sid, "42", "Alice Moreau", "Number two, the community garden.")
    fakes.llm.add("tick", {"topic": {"section": garden, "confidence": 0.9},
                           "advance": {"to": garden, "confidence": 0.9, "reason": "Number two"}})
    await rt.tick(sid)
    assert get(MeetppSession, sid).live_section_id == ids["Opening"]
    assert util.loads(get(MeetppSession, sid).proposal_json, {}).get("to") == ids["Community garden"]
    # Once the minutes were talked about (without being announced), the cue moves.
    await say(sid, "42", "Alice Moreau", "The minutes are approved, version two.")
    fakes.llm.add("tick", {"topic": {"section": minutes, "confidence": 0.9}})
    await rt.tick(sid)
    await say(sid, "43", "Ben Hartley", "Then the garden: the tank.")
    fakes.llm.add("tick", {"topic": {"section": garden, "confidence": 0.9},
                           "advance": {"to": garden, "confidence": 0.9, "reason": "Then the garden"}})
    await rt.tick(sid)
    assert get(MeetppSession, sid).live_section_id == ids["Community garden"]
    await drain()


async def test_advance_ignored_while_suppressed_after_chair_move(fakes):
    sid = await running(fakes)
    ids = sids(sid)
    await rt.chair_move(sid, "next")
    await say(sid, "43", "Ben Hartley", "Shall we take the garden now?")
    fakes.llm.add("tick", {"topic": {"section": pid(sid, "Community garden"), "confidence": 0.9},
                           "advance": {"to": pid(sid, "Community garden"), "confidence": 0.95}})
    await rt.tick(sid)
    assert get(MeetppSession, sid).live_section_id == ids["Approval of the minutes"]
    await drain()


async def test_topic_on_later_section_two_ticks_moves(fakes):
    sid = await running(fakes)
    ids = sids(sid)
    S = pid(sid, "Approval of the minutes")
    # A cue was heard but not sure enough to move on its own ("the minutes…").
    for i, text in enumerate(("Then the minutes, version two.", "Any comments on the minutes?")):
        await say(sid, "42", "Alice Moreau", text)
        cue = {"advance": {"to": S, "confidence": 0.55, "reason": "the minutes"}} if i == 0 else {}
        fakes.llm.add("tick", {"topic": {"section": S, "confidence": 0.9}, **cue})
        await rt.tick(sid)
    assert get(MeetppSession, sid).live_section_id == ids["Approval of the minutes"]
    assert fakes.bus.last("position")["by"] == "ai"
    await drain()


async def test_topic_on_later_section_steadily_moves_without_a_sure_tick(fakes):
    # "On to the garden" can mean two points: no tick is sure, but the
    # talk keeps landing on the same later point.
    sid = await running(fakes)
    ids = sids(sid)
    G = pid(sid, "Community garden")
    for i, conf in enumerate((0.7, 0.75, 0.7)):
        await say(sid, "42", "Alice Moreau", f"About the garden, part {i}.")
        cue = {"advance": {"to": G, "confidence": 0.5, "reason": "on to the garden"}} if i == 0 else {}
        fakes.llm.add("tick", {"topic": {"section": G, "confidence": conf}, **cue})
        await rt.tick(sid)
        moved = get(MeetppSession, sid).live_section_id == ids["Community garden"]
        assert moved == (i == 2)
    assert fakes.bus.last("position")["by"] == "ai"
    await drain()


async def test_talk_drifting_to_a_later_point_without_a_cue_moves_only_after_minutes(fakes):
    # A director previews the next point's subject; nobody says "next item".
    sid = await running(fakes)
    ids = sids(sid)
    live = get(MeetppSession, sid).live_section_id
    S = pid(sid, "Approval of the minutes")
    for i in range(rt.SILENT_MOVE_TICKS):
        await say(sid, "42", "Alice Moreau", f"Still about the minutes, remark {i}.")
        fakes.llm.add("tick", {"topic": {"section": S, "confidence": 0.9}})
        await rt.tick(sid)
        moved = get(MeetppSession, sid).live_section_id == ids["Approval of the minutes"]
        assert moved == (i == rt.SILENT_MOVE_TICKS - 1), i
    assert live != ids["Approval of the minutes"]
    await drain()


async def test_scattered_later_topics_do_not_move(fakes):
    sid = await running(fakes)
    ids = sids(sid)
    live = get(MeetppSession, sid).live_section_id
    for title in ("Community garden", "Budget 2027", "Community garden", "Budget 2027"):
        await say(sid, "42", "Alice Moreau", f"A word on {title}.")
        fakes.llm.add("tick", {"topic": {"section": pid(sid, title), "confidence": 0.7}})
        await rt.tick(sid)
    assert get(MeetppSession, sid).live_section_id == live != ids["Budget 2027"]


async def test_assist_mode_proposes_to_chairs_and_not_now(fakes):
    sid = await running(fakes, mode="assist")
    ids = sids(sid)
    await say(sid, "43", "Ben Hartley", "Next item, perhaps?")
    S = pid(sid, "Approval of the minutes")
    fakes.llm.add("tick", {"topic": {"section": S, "confidence": 0.9}, "advance": {"to": S, "confidence": 0.9, "reason": "next item"}})
    await rt.tick(sid)
    session = get(MeetppSession, sid)
    assert session.live_section_id == ids["Opening"]
    room, msg, dest = next((r, m, d) for r, m, d in fakes.bus.sent if m["type"] == "proposal")
    assert msg["to_section_id"] == ids["Approval of the minutes"] and dest == ["user-42"]
    proposal = util.loads(session.proposal_json, {})
    await rt.answer_proposal(sid, proposal["pid"], False)
    session = get(MeetppSession, sid)
    assert session.proposal_json is None
    # "Not now" suppresses the same target.
    n = len(fakes.bus.of("proposal"))
    await say(sid, "42", "Alice Moreau", "Next item really.")
    fakes.llm.add("tick", {"topic": {"section": S, "confidence": 0.9}, "advance": {"to": S, "confidence": 0.9}})
    await rt.tick(sid)
    assert len(fakes.bus.of("proposal")) == n


async def test_accepting_a_proposal_moves(fakes):
    sid = await running(fakes, mode="assist")
    ids = sids(sid)
    await say(sid, "43", "Ben Hartley", "Next item, perhaps?")
    S = pid(sid, "Approval of the minutes")
    fakes.llm.add("tick", {"advance": {"to": S, "confidence": 0.7}})
    await rt.tick(sid)
    proposal = util.loads(get(MeetppSession, sid).proposal_json, {})
    await rt.answer_proposal(sid, proposal["pid"], True)
    assert get(MeetppSession, sid).live_section_id == ids["Approval of the minutes"]
    await drain()


async def test_invalid_json_repaired_once_then_gap(fakes):
    sid = await running(fakes)
    s1 = await say(sid, "42", "Alice Moreau", "Some speech.")
    fakes.llm.add("tick", "not json", {"ops": [], "notes": [{"text": "A repaired note.", "evidence": [s1]}]})
    res = await rt.tick(sid)
    assert res["notes"] == 1
    s2 = await say(sid, "42", "Alice Moreau", "More speech.")
    fakes.llm.add("tick", "garbage", "{still not json")
    res = await rt.tick(sid)
    assert "gap" in res
    session = get(MeetppSession, sid)
    assert session.transcript_cursor == s2
    gap = [r for r in ops_rows(sid) if r.op_type == "interpretation.gap"]
    assert gap and util.loads(gap[0].payload_json, {})["to_seq"] == s2


async def test_llm_down_keeps_segments_then_catches_up(fakes):
    sid = await running(fakes)
    s1 = await say(sid, "42", "Alice Moreau", "Old speech.", seconds_ago=400)
    fakes.llm.add("tick", llm.LLMError("down"))
    runner = rt.SessionRunner(sid)
    res = await rt.tick(sid, runner=runner)
    assert "error" in res and runner.llm_down
    assert get(MeetppSession, sid).transcript_cursor < s1
    assert llm.ai_status(sid) == "paused"
    s2 = await say(sid, "42", "Alice Moreau", "Fresh speech.")
    fakes.llm.add("tick", {"ops": []})
    res = await rt.tick(sid, runner=runner)
    assert not runner.llm_down and llm.ai_status(sid) == "ok"
    # The backlog older than 3 minutes was skipped with a gap record.
    assert any(r.op_type == "interpretation.gap" for r in ops_rows(sid))
    assert get(MeetppSession, sid).transcript_cursor == s2
    prompt = fakes.llm.prompts("tick")[-1]
    assert "Fresh speech" in prompt and "Old speech" not in prompt.split("NEW (cite")[1]


async def test_backlog_is_split_into_two_minute_windows(fakes):
    sid = await running(fakes)
    seqs = []
    for i, ago in enumerate((280, 220, 150, 60, 10)):
        seqs.append(await say(sid, "42", "Alice Moreau", f"Line {i}.", seconds_ago=ago))
    fakes.llm.add("tick", {"ops": []}, {"ops": []}, {"ops": []})
    res = await rt.tick(sid)
    assert res["more"] is True
    first = get(MeetppSession, sid).transcript_cursor
    assert first in seqs[1:3]
    res = await rt.tick(sid)
    await rt.tick(sid)
    assert get(MeetppSession, sid).transcript_cursor == seqs[-1]
    # Context lines are marked as such in the next prompt.
    assert "CONTEXT (already processed" in fakes.llm.prompts("tick")[1]


async def test_pending_decision_is_merged_not_duplicated(fakes):
    sid = make_session(agenda=AGENDA)
    ids = sids(sid)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        db.add(MeetppDecision(id=util.ulid(), session_id=sid, series_id=session.series_id, ref="D-50",
                              section_id=ids["Rainwater tank"], title="Buy a rainwater tank for the garden",
                              resolution="that the association buys a rainwater tank for the garden", status="pending", origin="pdf"))
        db.commit()
    finally:
        db.close()
    await start(sid)
    await join(sid, "42", "Alice Moreau")
    await rt.chair_move(sid, "move", ids["Community garden"])
    s1 = await say(sid, "42", "Alice Moreau", "Agreed, we buy the tank.")
    fakes.llm.add("tick", {"topic": {"section": pid(sid, "Community garden"), "sub": "a", "confidence": 0.9},
                           "ops": [{"op": "decision.add", "section": pid(sid, "Community garden"), "title": "Buy a rainwater tank for the garden",
                                    "resolution": "that a rainwater tank is bought", "status": "adopted", "evidence": [s1]}]})
    res = await rt.tick(sid)
    assert res["applied"] == 1
    rows = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).all())
    assert len(rows) == 1 and rows[0].ref == "D-50" and rows[0].status == "adopted"
    assert rows[0].resolution == "that a rainwater tank is bought"
    # The prompt lists the pending decision with its ref.
    assert "D-50" in fakes.llm.prompts("tick")[0].split("DECISIONS TO TAKE")[1].split("DECISIONS RECORDED")[0]
    # topic.sub = (a): no earlier sibling; moving the topic to (b) closes (a).
    s2 = await say(sid, "42", "Alice Moreau", "Now the municipal connection.")
    fakes.llm.add("tick", {"topic": {"section": pid(sid, "Community garden"), "sub": "b", "confidence": 0.9}, "ops": []})
    await rt.tick(sid)
    assert get(MeetppSection, ids["Rainwater tank"]).status == "done"
    assert get(MeetppSection, ids["Municipal connection"]).status == "pending"
    _ = s2
    await drain()


async def test_previous_action_report_via_update(fakes):
    sid = await running(fakes)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        db.add(MeetppAction(id=util.ulid(), series_id=session.series_id, session_id=sid, ref="A-7", title="Clean the shed", status="open", origin="pdf"))
        db.commit()
    finally:
        db.close()
    s1 = await say(sid, "43", "Ben Hartley", "The shed was cleaned last week.")
    fakes.llm.add("tick", {"ops": [{"op": "action.update", "ref": "A-7", "status": "done", "report_note": "Cleaned last week.",
                                    "completion_note": "Shed cleaned.", "evidence": [s1]}]})
    res = await rt.tick(sid)
    assert res["applied"] == 1
    a = query(lambda db: db.query(MeetppAction).filter_by(ref="A-7", session_id=sid).one())
    assert a.status == "done" and a.completed_at is not None and a.completion_note == "Shed cleaned."
    r = query(lambda db: db.query(MeetppActionReport).filter_by(action_id=a.id, session_id=sid).one())
    assert r.note == "Cleaned last week." and r.status_at_report == "done"
    act = fakes.bus.last("state")["activations"][0]
    assert act["kind"] == "action" and act["item_id"] == a.id


async def test_repeated_action_update_is_not_reapplied_and_report_is_shown(fakes):
    sid = await running(fakes)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        db.add(MeetppAction(id=util.ulid(), series_id=session.series_id, session_id=sid, ref="A-7", title="Clean the shed",
                            description="The shed behind the hall, before the winter.", status="open", origin="pdf"))
        db.commit()
    finally:
        db.close()
    s1 = await say(sid, "43", "Ben Hartley", "The shed is half done.")
    op = {"op": "action.update", "ref": "A-7", "status": "in_progress", "report_note": "Half done.", "evidence": [s1]}
    fakes.llm.add("tick", {"ops": [op]})
    assert (await rt.tick(sid))["applied"] == 1
    prompt = fakes.llm.prompts("tick")[0]
    assert "A-7 · Clean the shed · open · TO REVIEW — The shed behind the hall" in prompt
    s2 = await say(sid, "43", "Ben Hartley", "As I said, half done.")
    fakes.llm.add("tick", {"ops": [dict(op, evidence=[s2])]})
    states = len(fakes.bus.of("state"))
    res = await rt.tick(sid)
    assert res["applied"] == 0 and res["rejected"][0]["reason"] == "no change"
    assert not any(m.get("activations") for m in fakes.bus.of("state")[states:])
    assert "A-7 · Clean the shed · in_progress · reported: Half done." in fakes.llm.prompts("tick")[1]


async def test_prompt_shows_next_point_text(fakes):
    sid = make_session(agenda=[{"title": "Approval of the minutes", "body": "Version 2 of the minutes."},
                               {"title": "Community garden", "body": "Water supply: tank or municipal connection."},
                               {"title": "Budget 2027", "body": "Draft budget."}])
    await start(sid)
    await join(sid, "42", "Alice Moreau")
    await rt.chair_move(sid, "move", sids(sid)["Approval of the minutes"])
    await say(sid, "42", "Alice Moreau", "The minutes.")
    fakes.llm.add("tick", {})
    await rt.tick(sid)
    prompt = fakes.llm.prompts("tick")[0]
    assert "Version 2 of the minutes." in prompt and "Water supply: tank or municipal connection." in prompt
    assert "← NEXT" in prompt and "Draft budget." not in prompt


async def test_adopted_without_a_count_is_recorded_as_assent_in_a_board_meeting(fakes):
    sid = await running(fakes, meeting_type="board")
    s0 = await say(sid, "42", "Alice Moreau", "So we keep the hall booking. Agreed?")
    s1 = await say(sid, "43", "Ben Hartley", "Yes, fine by me.")
    fakes.llm.add("tick", {"ops": [{"op": "decision.add", "section": pid(sid, "Approval of the minutes"),
                                    "title": "Hall booking kept", "resolution": "that the hall booking is kept",
                                    "status": "adopted", "evidence": [s0, s1]}]})
    assert (await rt.tick(sid))["applied"] == 1
    d = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).one())
    v = query(lambda db: db.query(MeetppVote).filter_by(decision_id=d.id).one())
    assert v.method == "assent" and v.tally_for == 2 and v.present_count == 2
    # An assent sent with zero counts is counted from the ballots; a count
    # above the voting members present is kept as heard but decides nothing
    # (the chair checks it in review).
    s2 = await say(sid, "43", "Ben Hartley", "And the shed and the fence: all in favour? Yes. Agreed.")
    fakes.llm.add("tick", {"ops": [
        {"op": "decision.add", "section": pid(sid, "Approval of the minutes"), "title": "Shed repainted",
         "resolution": "that the shed is repainted", "status": "adopted", "evidence": [s2],
         "vote": {"method": "assent", "for": 0, "against": 0, "abstain": 0}},
        {"op": "decision.add", "section": pid(sid, "Approval of the minutes"), "title": "Fence mended",
         "resolution": "that the fence is mended", "status": "adopted", "evidence": [s2],
         "vote": {"method": "voice", "for": 3, "against": 0, "abstain": 0}},
    ]})
    assert (await rt.tick(sid))["applied"] == 2
    tallies = query(lambda db: {d.title: db.query(MeetppVote).filter_by(decision_id=d.id).one().tally_for
                                for d in db.query(MeetppDecision).filter_by(session_id=sid).all()})
    assert tallies == {"Hall booking kept": 2, "Shed repainted": 2, "Fence mended": 3}
    fence = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid, title="Fence mended").one())
    s3 = await say(sid, "42", "Alice Moreau", "On the fence, one for and four against, I think.")
    fakes.llm.add("tick", {"ops": [{"op": "decision.update", "ref": fence.ref, "evidence": [s3],
                                    "vote": {"method": "voice", "for": 1, "against": 4, "abstain": 0}}]})
    assert (await rt.tick(sid))["applied"] == 1
    fence = get(MeetppDecision, fence.id)
    v = query(lambda db: db.query(MeetppVote).filter_by(decision_id=fence.id).one())
    assert fence.status == "adopted" and (v.tally_for, v.tally_against) == (1, 4) and not v.confirmed
    checks = query(lambda db: compose._count_checks(db, db.get(MeetppSession, sid)))
    assert len(checks) == 1 and checks[0].startswith(f"{fence.ref} ") and "more than the 2 voting members present" in checks[0]


async def test_session_start_and_end_move_the_board_on_and_off_the_stage(fakes):
    sid = await running(fakes)
    await drain()
    assert [b for _r, _p, b in fakes.bus.room_meetpp] == ["start"]
    await rt.end_session(sid)
    await drain()
    assert [b for _r, _p, b in fakes.bus.room_meetpp][-1] == "end"


async def test_notes_deduplicated(fakes):
    sid = await running(fakes)
    s1 = await say(sid, "42", "Alice Moreau", "The hall is booked for Tuesdays.")
    note = {"text": "The hall was booked for Tuesday evenings.", "evidence": [s1]}
    fakes.llm.add("tick", {"notes": [note, note]})
    res = await rt.tick(sid)
    assert res["notes"] == 1


async def test_cue_phrase_pokes_runner(fakes):
    sid = await running(fakes)
    runner = rt.runtime.get(sid)
    try:
        await say(sid, "42", "Alice Moreau", "All in favour? Carried.")
        assert runner.trigger == "cue"
    finally:
        rt.runtime.runners.pop(sid, None)


def test_tick_due_interval_and_budget(fakes, monkeypatch):
    import asyncio

    sid = make_session(agenda=AGENDA)
    asyncio.get_event_loop_policy()
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        session.status = "running"
        db.add(MeetppSegment(session_id=sid, seq=1, identity="user-42", text="hi", t_start=util.now(), t_end=util.now()))
        session.last_tick_at = util.now() - timedelta(seconds=15)
        db.commit()
        runner = rt.SessionRunner(sid)
        assert runner._tick_due(db, session)
        monkeypatch.setattr(llm, "over_budget", lambda db, sid: True)
        assert not runner._tick_due(db, session)
        runner.trigger = "cue"
        assert runner._tick_due(db, session)
    finally:
        db.close()


async def test_prompt_ids_stay_pinned_when_a_section_is_added(fakes):
    sid = await running(fakes)
    ids = sids(sid)
    S_garden, S_budget = pid(sid, "Community garden"), pid(sid, "Budget 2027")
    s1 = await say(sid, "42", "Alice Moreau", "A new sub-point on fencing; and the budget is agreed.")
    fakes.llm.add("tick", {"ops": [
        {"op": "section.add", "kind": "subpoint", "parent": S_garden, "title": "Fencing"},
        {"op": "decision.add", "section": S_budget, "title": "Adopt the 2027 budget", "status": "adopted", "evidence": [s1]},
    ]})
    res = await rt.tick(sid)
    assert res["applied"] == 2
    d = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).one())
    assert d.section_id == ids["Budget 2027"]
    assert "Fencing" in sids(sid)
    msg = fakes.bus.last("state")
    assert any(s["title"] == "Fencing" and s["number"] == "2.3" for s in msg["delta"]["sections"])


async def test_runner_loop_ticks_on_cue_and_finalises(fakes, monkeypatch):
    import asyncio

    from app.config import settings

    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")  # no Redis: single process
    sid = await running(fakes)
    fakes.agent.finalize_ok = False
    runner = rt.runtime.get(sid)
    try:
        runner.start()
        await asyncio.sleep(0.05)
        s1 = await say(sid, "42", "Alice Moreau", "All in favour? Agreed.")
        fakes.llm.add("tick", {"notes": [{"text": "The proposal was agreed.", "evidence": [s1]}]})
        for _ in range(100):
            if get(MeetppSession, sid).transcript_cursor >= s1:
                break
            await asyncio.sleep(0.05)
        assert get(MeetppSession, sid).transcript_cursor == s1
        assert fakes.agent.named("start"), "the runner starts the agent session"
        fakes.llm.add("compose_section", *[{"markdown": "The meeting agreed the proposal after a short discussion of the options."}] * 3)
        fakes.llm.add("compose_final", {"opening": "Opened.", "adjournment": "Closed."})
        await rt.end_session(sid)
        for _ in range(100):
            if get(MeetppSession, sid).status == "review":
                break
            await asyncio.sleep(0.05)
        assert get(MeetppSession, sid).status == "review"
    finally:
        await rt.runtime.drop(sid)


async def test_ai_adopts_only_on_agreement_in_the_cited_lines(fakes):
    sid = await running(fakes, meeting_type="board")
    sec = pid(sid, "Community garden")
    s1 = await say(sid, "42", "Alice Moreau", "My view is that we should stop renting the hall altogether.")
    s2 = await say(sid, "42", "Alice Moreau", "It costs far too much for what we get from it.")
    fakes.llm.add("tick", {"ops": [{"op": "decision.add", "section": sec, "title": "Stop renting the hall",
                                    "status": "adopted", "evidence": [s1, s2],
                                    "vote": {"method": "assent", "for": 2, "against": 0, "abstain": 0}}]})
    assert (await rt.tick(sid))["applied"] == 1
    d = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).one())
    # An opinion, nobody agreed: proposed, and the model's vote is not taken.
    assert d.status == "proposed" and d.decided_at is None
    assert query(lambda db: db.query(MeetppVote).filter_by(decision_id=d.id).count()) == 0
    # The other director's plain "yes" is agreement: the next cite adopts it.
    s3 = await say(sid, "43", "Ben Hartley", "Yes, fine.")
    fakes.llm.add("tick", {"ops": [{"op": "decision.update", "ref": d.ref, "status": "adopted", "evidence": [s1, s3]}]})
    assert (await rt.tick(sid))["applied"] == 1
    d = get(MeetppDecision, d.id)
    assert d.status == "adopted" and d.decided_seq == s3
    assert query(lambda db: db.query(MeetppVote).filter_by(decision_id=d.id).one()).method == "assent"
    # Agreement to something else does not adopt it.
    s5 = await say(sid, "42", "Alice Moreau", "Shall we move the plant sale to May?")
    s6 = await say(sid, "43", "Ben Hartley", "Yes, that's a good idea, let's do that.")
    fakes.llm.add("tick", {"ops": [{"op": "decision.add", "section": sec, "title": "Replace the shed roof",
                                    "status": "adopted", "evidence": [s5, s6]}]})
    await rt.tick(sid)
    roof = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid, title="Replace the shed roof").one())
    assert roof.status == "proposed"
    # A suggestion nobody answered stays proposed too.
    s4 = await say(sid, "42", "Alice Moreau", "We could also paint the gate.")
    rows = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).count())
    fakes.llm.add("tick", {"ops": [{"op": "decision.add", "section": sec, "title": "Paint the gate",
                                    "status": "adopted", "evidence": [s4]}]})
    await rt.tick(sid)
    gate = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid, title="Paint the gate").one())
    assert gate.status == "proposed" and rows == 2

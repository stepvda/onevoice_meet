"""Section composition, final composition, the minutes document and the
finalisation jobs (FDD §8.7, §8.8)."""
from __future__ import annotations

import asyncio

from app.config import settings
from app.db import SessionLocal
from app.meetpp import compose, outline, runtime as rt, util
from app.meetpp.models import MeetppAttendee, MeetppMinute, MeetppSection, MeetppSession

from .conftest import add_roster, drain, get, join, make_session, query, say, sids, start

AGENDA = [{"title": "Approval of the minutes"}, {"title": "Rainwater tank", "subpoints": [{"title": "Quotes"}]}, {"title": "Budget"}]

SECTION_MD = (
    "The chair put the minutes of the previous meeting to the board. Both directors present agreed that the minutes were "
    "an accurate record and no amendment was proposed by anyone present at the meeting.\n\n"
    "> **RESOLVED:** that the minutes of meeting #8 are approved."
)


def _minute(sid, section_id=None, kind="section"):
    def q(db):
        rows = db.query(MeetppMinute).filter_by(session_id=sid, kind=kind)
        rows = rows.filter_by(section_id=section_id) if section_id else rows.filter(MeetppMinute.section_id.is_(None))
        return rows.first()

    return query(q)


async def _meeting_with_decision(fakes):
    sid = make_session(meeting_type="board", agenda=AGENDA)
    series_id = get(MeetppSession, sid).series_id
    for key, name in (("sub:42", "Alice Moreau"), ("sub:43", "Ben Hartley"), ("sub:44", "Chloe Varga")):
        add_roster(series_id, key, name)
    await start(sid)
    await join(sid, "42", "Alice Moreau")
    await join(sid, "43", "Ben Hartley")
    await rt.chair_move(sid, "next")
    s1 = await say(sid, "42", "Alice Moreau", "Shall we approve the minutes of meeting 8? Agreed by both.")
    fakes.llm.add("tick", {"topic": {"section": "S3", "confidence": 0.9}, "ops": [
        {"op": "decision.add", "section": "S3", "title": "Approve the minutes of meeting #8",
         "resolution": "that the minutes of meeting #8 are approved", "status": "adopted", "vote": {"method": "assent"}, "evidence": [s1]}],
        "notes": [{"section": "S3", "text": "The minutes of meeting #8 were approved.", "evidence": [s1]}]})
    await rt.tick(sid)
    await rt.chair_move(sid, "next")
    await drain()
    return sid


async def test_section_composition_validates_and_appends(fakes):
    sid = await _meeting_with_decision(fakes)
    ids = sids(sid)
    # First draft adds a RESOLVED block for something that was not adopted → repair.
    bad = SECTION_MD + "\n\n> **RESOLVED:** that the budget is doubled."
    fakes.llm.add("compose_section", {"markdown": bad, "verify": []}, {"markdown": "The chair put the minutes to the board; both directors agreed.", "verify": ["meeting #8"]})
    status = await compose.compose_section(sid, ids["Approval of the minutes"])
    assert status == "composed"
    m = _minute(sid, ids["Approval of the minutes"])
    assert m.version == 1 and m.source_tier == "live" and m.composed_at is not None
    # The missing RESOLVED block for the adopted decision was appended.
    assert m.narrative_md.endswith("> **RESOLVED:** that the minutes of meeting #8 are approved")
    assert util.loads(m.verify_json, []) == ["meeting #8"]
    assert "budget is doubled" not in m.narrative_md
    prompt = fakes.llm.prompts("compose_section")[0]
    assert "[ADOPTED]" in prompt and "### n.m" not in prompt
    # Length target: a third of the words spoken, never below the floor.
    assert f"LENGTH: about {compose.TARGET_MIN} words" in prompt
    assert compose.validate_section("word " * 2000, [], True) is None
    assert "too long" in compose.validate_section("word " * (compose.MAX_WORDS + 1), [], True)
    # No decision → "No resolution was put." is appended.
    assert compose.finish_markdown("Discussion only.", []) == "Discussion only.\n\nNo resolution was put."


async def test_overlong_draft_is_condensed_or_kept(fakes):
    sid = await _meeting_with_decision(fakes)
    ids = sids(sid)
    long_md = "The directors discussed the minutes at length. " * 60 + "\n\n" + SECTION_MD
    short_md = "The chair put the minutes of meeting #8 to the board, and both directors present agreed. " * 3
    fakes.llm.add("compose_section", {"markdown": long_md, "verify": ["meeting #8"]})
    # Key points first, then minutes written from them alone.
    fakes.llm.add("compose_condense", {"points": ["The minutes of meeting #8 were approved."]}, {"markdown": short_md})
    assert await compose.compose_section(sid, ids["Approval of the minutes"]) == "composed"
    m = _minute(sid, ids["Approval of the minutes"])
    assert m.narrative_md.startswith("The chair put the minutes") and util.loads(m.verify_json, []) == ["meeting #8"]
    # The RESOLVED block is added back for the adopted decision.
    assert m.narrative_md.endswith("> **RESOLVED:** that the minutes of meeting #8 are approved")
    prompts_ = fakes.llm.prompts("compose_condense")
    assert "RESOLVED" in prompts_[0] and "KEY POINTS:\n- The minutes of meeting #8 were approved." in prompts_[1]
    # Minutes cut too far (twice) are refused: the long draft stands.
    fakes.llm.add("compose_section", {"markdown": long_md, "verify": []})
    fakes.llm.add("compose_condense", {"points": ["Approved."]}, {"markdown": "Approved."}, {"markdown": "Approved again."})
    assert await compose.compose_section(sid, ids["Approval of the minutes"], force=True) == "composed"
    assert util.word_count(_minute(sid, ids["Approval of the minutes"]).narrative_md) > 300


async def test_composition_failure_keeps_notes_and_reopen_discards(fakes):
    sid = await _meeting_with_decision(fakes)
    ids = sids(sid)
    fakes.llm.add("compose_section", "nope", "still nope")
    status = await compose.compose_section(sid, ids["Approval of the minutes"])
    assert status == "failed"
    m = _minute(sid, ids["Approval of the minutes"])
    assert m.error and util.loads(m.notes_json, [])
    # Reopened while composing: the result stays a draft.
    await rt.chair_move(sid, "move", ids["Approval of the minutes"])
    fakes.llm.add("compose_section", {"markdown": SECTION_MD})
    status = await compose.compose_section(sid, ids["Approval of the minutes"])
    assert status == "notes"
    await drain()


async def test_locked_minutes_are_not_overwritten(fakes):
    sid = await _meeting_with_decision(fakes)
    ids = sids(sid)
    db = SessionLocal()
    try:
        from app.meetpp import ops

        session = db.get(MeetppSession, sid)
        ctx = ops.ApplyContext(db=db, session=session, actor="user:user-42")
        ops.apply_ops(ctx, [{"op": "minutes.edit", "section_id": ids["Approval of the minutes"], "narrative_md": "Edited by the chair."}])
        db.commit()
    finally:
        db.close()
    m = _minute(sid, ids["Approval of the minutes"])
    assert m.locked and m.status == "edited" and m.version == 1
    fakes.llm.add("compose_section", {"markdown": SECTION_MD})
    await compose.compose_section(sid, ids["Approval of the minutes"])
    assert _minute(sid, ids["Approval of the minutes"]).narrative_md == "Edited by the chair."
    # Regenerate (force) replaces it with a new version.
    await compose.compose_section(sid, ids["Approval of the minutes"], force=True)
    m = _minute(sid, ids["Approval of the minutes"])
    assert m.narrative_md.startswith("The chair put the minutes") and not m.locked and m.version == 2


async def test_finalisation_jobs_reach_review(fakes, monkeypatch):
    sid = await _meeting_with_decision(fakes)
    ids = sids(sid)
    fakes.agent.finalize_ok = False  # tier 2 unavailable → skipped
    res = await rt.end_session(sid)
    assert res == {"status": "finalising"}
    session = get(MeetppSession, sid)
    assert session.status == "finalising" and session.ended_at is not None
    assert get(MeetppSection, ids["Approval of the minutes"]).status == "done"
    assert fakes.bus.last("session")["state"] == "finalising"
    fakes.llm.add("compose_section", {"markdown": SECTION_MD, "verify": []})
    fakes.llm.add("compose_final", {"opening": "The chair opened the meeting with two of the three directors present.",
                                    "adjournment": "The chair closed the meeting.", "summary": ["Minutes approved."],
                                    "next_agenda": [{"title": "Rainwater tank", "body": "Quotes"}], "verify": ["EUR 1,300"]})
    jobs = await rt.run_finalisation(sid)
    assert {k: v["status"] for k, v in jobs.items()} == {"tier2": "skipped", "compose": "done", "final": "done", "render": "done"}
    session = get(MeetppSession, sid)
    assert session.status == "review" and session.finalised_at is not None
    assert fakes.bus.last("session")["state"] == "review"
    # Absent roster member.
    c = query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid, person_key="sub:44").one())
    assert c.status == "absent"
    for kind in ("opening", "adjournment", "voting_record", "provenance"):
        assert _minute(sid, kind=kind).narrative_md, kind
    assert "| # | Resolution | Rule | Result |" in _minute(sid, kind="voting_record").narrative_md
    assert "live-quality transcript" in _minute(sid, kind="provenance").narrative_md
    final = util.loads(session.final_json, {})
    assert final["summary"] == ["Minutes approved."] and final["verify"] == ["EUR 1,300"]
    md = compose.minutes_markdown(SessionLocal(), session)
    lines = md.splitlines()
    assert lines[0] == "# Minutes — Board meeting #9"
    assert "**Present:** Alice Moreau (chair), Ben Hartley." in md
    assert "**Absent:** Chloe Varga." in md and "**Quorum:** met (2 of 3 directors present or represented; quorum 2)." in md
    assert "**Opening.** The chair opened the meeting" in md
    assert md.index("## 1. Approval of the minutes") < md.index("## Adjournment") < md.index("### Record of voting") < md.index("### Provenance")
    assert "## 3. Budget\n\n_Not discussed._" in md
    assert "> **RESOLVED:** that the minutes of meeting #8 are approved." in md


async def test_tier2_final_pass_and_recompose_on_improved_source(fakes, monkeypatch):
    monkeypatch.setattr(settings, "meetpp_final_pass_timeout_seconds", 5)
    sid = await _meeting_with_decision(fakes)
    ids = sids(sid)
    fakes.llm.add("compose_section", {"markdown": SECTION_MD})
    await compose.compose_section(sid, ids["Approval of the minutes"])
    assert _minute(sid, ids["Approval of the minutes"]).source_tier == "live"
    await rt.end_session(sid)

    async def agent_finishes():
        await asyncio.sleep(0.1)
        seg = query(lambda db: db.query(__import__("app.meetpp.models", fromlist=["MeetppSegment"]).MeetppSegment).filter_by(session_id=sid, is_gap=False).first())
        await rt.ingest(sid, {"refinements": [{"utterance_id": seg.utterance_id, "text": "Shall we approve the minutes of meeting #8? Agreed.", "final": True}]})
        await rt.agent_status(sid, {"status": "offline", "final_pass": "done", "tier2": "up"})

    fakes.llm.add("compose_section", {"markdown": SECTION_MD}, {"markdown": SECTION_MD})
    fakes.llm.add("compose_final", {"opening": "Opened.", "adjournment": "Closed."})
    task = asyncio.get_running_loop().create_task(agent_finishes())
    jobs = await rt.run_finalisation(sid)
    await task
    assert jobs["tier2"]["status"] == "done"
    assert ("finalize", sid) in fakes.agent.calls and ("stop", sid) in fakes.agent.calls
    m = _minute(sid, ids["Approval of the minutes"])
    assert m.source_tier == "refined" and m.version == 2
    assert "refined" in _minute(sid, kind="provenance").narrative_md


async def test_tier2_skipped_by_agent_and_compose_retry(fakes, monkeypatch):
    monkeypatch.setattr(settings, "meetpp_final_pass_timeout_seconds", 5)
    sid = await _meeting_with_decision(fakes)
    await rt.agent_status(sid, {"status": "listening", "tier2": "up", "final_pass": "skipped"})
    await rt.end_session(sid)
    fakes.llm.add("compose_section", "bad", "bad")
    fakes.llm.add("compose_final", {"opening": "Opened.", "adjournment": "Closed."})
    jobs = await rt.run_finalisation(sid)
    assert jobs["tier2"]["status"] == "skipped"
    assert jobs["compose"]["status"] == "failed" and jobs["final"]["status"] == "done"
    assert get(MeetppSession, sid).status == "review"
    # Retry re-runs the failed job only.
    fakes.llm.add("compose_section", {"markdown": SECTION_MD})
    jobs = await rt.run_finalisation(sid, retry=True)
    assert jobs["compose"]["status"] == "done" and jobs["tier2"]["status"] == "skipped"
    assert get(MeetppSession, sid).status == "review"


async def test_finalisation_without_llm_never_sticks(fakes, monkeypatch):
    sid = await _meeting_with_decision(fakes)
    monkeypatch.setattr(settings, "llm_api_key", "")
    fakes.agent.finalize_ok = False
    await rt.end_session(sid)
    jobs = await rt.run_finalisation(sid)
    assert get(MeetppSession, sid).status == "review"
    assert jobs["final"]["status"] == "done"
    # Without the LLM, the opening/adjournment fall back to the facts.
    assert _minute(sid, kind="opening").narrative_md.startswith("The meeting opened at")
    md = compose.minutes_markdown(SessionLocal(), get(MeetppSession, sid))
    # Failed sections show their running notes and RESOLVED blocks.
    assert "- The minutes of meeting #8 were approved." in md
    assert "> **RESOLVED:** that the minutes of meeting #8 are approved" in md


def test_resolved_blocks_parsing():
    md = "Text.\n\n> **RESOLVED:** that A is done,\n> and B too.\n\nMore.\n\n> **RESOLVED:** that C."
    assert compose.resolved_blocks(md) == ["that A is done, and B too.", "that C."]
    assert compose.validate_section("", [], False) == "the markdown is empty"
    assert "too short" in compose.validate_section("Short.", [], True)
    _ = outline


def test_markdown_of_reads_a_garbled_key():
    assert compose.markdown_of({"markdown": "Text."}) == "Text."
    long = "### 7.1 Separate documents\n\nThe directors agreed to separate the documents."
    assert compose.markdown_of({": ": long}) == long
    assert compose.markdown_of({"a": long, "b": long}) == ""


async def test_reconcile_reports_previous_actions_only_from_quoted_minutes(fakes):
    from app.meetpp.models import MeetppAction, MeetppActionReport

    sid = await _meeting_with_decision(fakes)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        for ref, title in (("A-1", "Ask two suppliers for a tank quote"), ("A-2", "Repaint the shed")):
            db.add(MeetppAction(id=util.ulid(), series_id=session.series_id, session_id=sid, ref=ref, title=title,
                                status="open", origin="pdf"))
        minute = outline.minute_for(db, session, sids(sid)["Approval of the minutes"])
        minute.narrative_md = "The chair reported that both tank quotes had been received and the order was placed."
        db.commit()
    finally:
        db.close()
    fakes.llm.add("compose_actions", {"reports": [
        {"ref": "A-1", "status": "done", "note": "Both quotes were received; the tank was ordered.",
         "quote": "The chair reported that both tank quotes had been received and the order was placed."},
        # Not in the minutes: dropped.
        {"ref": "A-2", "status": "done", "note": "The shed was repainted.", "quote": "The shed was repainted last week."},
    ]})
    assert await compose.reconcile_previous_actions(sid) == 1
    rows = {a.ref: a for a in query(lambda db: db.query(MeetppAction).filter_by(session_id=sid).all())}
    assert rows["A-1"].status == "done" and rows["A-2"].status == "open"
    note = query(lambda db: db.query(MeetppActionReport).filter_by(action_id=rows["A-1"].id).one().note)
    assert note == "Both quotes were received; the tank was ordered."
    assert "ACTIONS:\nA-1 · Ask two suppliers" in fakes.llm.prompts("compose_actions")[0]


async def test_closures_not_confirmed_by_the_transcript_are_reopened(fakes):
    from app.meetpp.models import MeetppAction, MeetppActionReport

    sid = await _meeting_with_decision(fakes)
    s1 = await say(sid, "42", "Alice Moreau", "The tank quotes are in, so that action is finished.")
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        for ref, title in (("A-1", "Collect two tank quotes"), ("A-2", "Prove the full restore")):
            db.add(MeetppAction(id=util.ulid(), series_id=session.series_id, session_id=sid, ref=ref, title=title,
                                status="done", completed_at=util.now(), origin="pdf"))
        db.add(MeetppAction(id=util.ulid(), series_id=session.series_id, session_id=sid, ref="A-3", title="Paint the shed",
                            status="done", completed_at=util.now(), origin="pdf", locked=True))
        db.commit()
    finally:
        db.close()
    fakes.llm.add("compose_actions", {"confirmed": [
        {"ref": "A-1", "quote": "The tank quotes are in, so that action is finished."},
        {"ref": "A-2", "quote": "The restore is complete."},  # not in the transcript
    ]})
    assert await compose.verify_closed_previous_actions(sid) == 1
    rows = {a.ref: a for a in query(lambda db: db.query(MeetppAction).filter_by(session_id=sid).all())}
    assert rows["A-1"].status == "done" and rows["A-2"].status == "open" and rows["A-2"].completed_at is None
    assert rows["A-3"].status == "done"  # the chair's own change stands

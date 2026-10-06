"""Votes and quorum, action reports and carried-forward actions, the state
snapshot DTOs (contract §3) and the export shape (contract §7)."""
from __future__ import annotations

from app.db import SessionLocal
from app.meetpp import export as export_mod, governance, ops, runtime as rt, util
from app.meetpp.models import (
    MeetppAction,
    MeetppActionReport,
    MeetppAttendee,
    MeetppDecision,
    MeetppSession,
    MeetppVote,
)

from .conftest import add_roster, drain, get, join, make_session, query, say, sids, start

SESSION_META_KEYS = {
    "id", "status", "template", "mode", "language", "goal", "meeting_type", "majority_rule", "series_id", "series_title",
    "meeting_id", "room", "live_section_id", "topic_section_id", "started_at", "ended_at", "published_at", "settings",
    "editors", "undo", "proposal", "agent", "ai", "jobs", "quorum_required", "voting_body",
}
SECTION_KEYS = {"id", "kind", "parent_id", "position", "number", "title", "body", "presenter", "timebox_minutes", "status",
                "started_at", "ended_at", "elapsed_seconds", "source", "locked", "counts", "previous_actions_home"}
DECISION_KEYS = {"id", "ref", "section_id", "title", "resolution", "how_taken", "status", "decided_at", "origin", "confirmed",
                 "locked", "evidence", "previous", "vote"}
VOTE_KEYS = {"method", "for", "against", "abstain", "eligible", "present", "quorum_required", "quorum_met", "result",
             "outcome_note", "confirmed", "ballots"}
ACTION_KEYS = {"id", "ref", "section_id", "title", "description", "assignees", "due", "status", "decision_ref", "completed_at",
               "completion_note", "progress_notes", "origin", "locked", "evidence", "previous", "carried_forward", "report"}
MINUTE_KEYS = {"id", "kind", "section_id", "notes", "narrative_md", "version", "status", "source_tier", "composed_at", "locked", "error"}
ATTENDEE_KEYS = {"id", "person_key", "name", "username", "email", "status", "online", "voting", "represented_by", "mandate_ref",
                 "opted_out", "required_next", "required_reason", "talk_seconds"}
ATTACHMENT_KEYS = {"id", "section_id", "kind", "filename", "caption", "author", "created_at", "url"}
SEGMENT_KEYS = {"seq", "identity", "name", "person_key", "t_start", "t_end", "text", "text_refined", "tier", "is_gap", "gap_reason"}

AGENDA = [{"title": "Approval of the minutes"}, {"title": "Rainwater tank", "subpoints": [{"title": "Quotes"}]}]


def test_compute_result_and_outcome_sentence():
    assert governance.compute_result("ordinary", 2, 0, 0) == "adopted"
    assert governance.compute_result("ordinary", 1, 1, 3) == "rejected"
    assert governance.compute_result("unanimous", 3, 1, 0) == "rejected"
    assert governance.compute_result("two_thirds", 2, 1, 0) == "adopted"
    assert governance.compute_result("four_fifths", 3, 1, 0) == "rejected"
    assert governance.compute_result("ordinary", None, None, None) is None
    sentence = governance.outcome_sentence(
        result="adopted", meeting_type="board", rule="ordinary", n_for=2, n_against=0, n_abstain=0,
        eligible=3, present=2, quorum_required=2, quorum_met=True,
    )
    assert sentence == (
        "Adopted by the board on an ordinary majority of the votes cast. 2 votes in favour, 0 against, 0 abstentions. "
        "3 directors entitled to vote, 2 present or represented (quorum 2)."
    )
    assert governance.outcome_sentence(
        result="rejected", meeting_type="general_assembly", rule="two_thirds", n_for=1, n_against=1, n_abstain=1,
        eligible=5, present=2, quorum_required=3, quorum_met=False,
    ) == (
        "Rejected by the general assembly: a two-thirds majority of the votes cast was not reached. 1 vote in favour, 1 against, "
        "1 abstention. 5 members entitled to vote, 2 present or represented (quorum 3). The quorum was not met."
    )


async def _board(fakes):
    sid = make_session(meeting_type="board", agenda=AGENDA)
    series_id = get(MeetppSession, sid).series_id
    add_roster(series_id, "sub:42", "Alice Moreau")
    add_roster(series_id, "sub:43", "Ben Hartley")
    add_roster(series_id, "sub:44", "Chloe Varga")
    await start(sid)
    await join(sid, "42", "Alice Moreau")
    await join(sid, "43", "Ben Hartley")
    return sid


async def test_quorum_and_assent_vote_ballots(fakes):
    sid = await _board(fakes)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        assert governance.quorum(db, session) == {"required": 2, "voting_present": 2, "voting_total": 3, "met": True}
        assert db.query(MeetppAttendee).filter_by(session_id=sid, person_key="sub:44").one().status == "not_registered"
    finally:
        db.close()
    s1 = await say(sid, "42", "Alice Moreau", "Both agree, the minutes are approved.")
    fakes.llm.add("tick", {"ops": [{"op": "decision.add", "section": "S3", "title": "Approve the minutes",
                                    "resolution": "that the minutes are approved", "status": "adopted",
                                    "vote": {"method": "assent"}, "evidence": [s1]}]})
    await rt.tick(sid)
    d = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).one())
    db = SessionLocal()
    try:
        state = ops.build_state(db, db.get(MeetppSession, sid))
    finally:
        db.close()
    vote = state["decisions"][0]["vote"]
    assert set(vote) == VOTE_KEYS
    assert (vote["for"], vote["against"], vote["abstain"]) == (2, 0, 0)
    assert (vote["eligible"], vote["present"], vote["quorum_required"], vote["quorum_met"]) == (3, 2, 2, True)
    assert vote["result"] == "adopted" and not vote["confirmed"]
    choices = {b["name"]: b["choice"] for b in vote["ballots"]}
    assert choices == {"Alice Moreau": "for", "Ben Hartley": "for", "Chloe Varga": "not_recorded"}
    assert vote["outcome_note"].startswith("Adopted by the board on an ordinary majority")
    assert state["quorum"] == {"required": 2, "voting_present": 2, "voting_total": 3, "met": True}

    # Chair edits the vote record (server recomputes eligibility and result).
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        row = db.get(MeetppDecision, d.id)
        v = governance.apply_vote(db, session, row, {"method": "show_of_hands", "for": 1, "against": 1, "abstain": 0}, confirmed=True)
        db.commit()
        assert (v.result, v.confirmed, v.eligible_count) == ("rejected", True, 3)
        series = session.series_id
    finally:
        db.close()
    # A custom quorum rule.
    db = SessionLocal()
    try:
        from app.meetpp.models import MeetppSeries

        s = db.get(MeetppSeries, series)
        s.quorum_required = 3
        db.commit()
        assert governance.quorum(db, db.get(MeetppSession, sid))["met"] is False
    finally:
        db.close()


async def test_action_reports_and_carried_forward_across_sessions(fakes):
    sid1 = make_session(agenda=AGENDA)
    series_id = get(MeetppSession, sid1).series_id
    await start(sid1)
    await join(sid1, "42", "Alice Moreau")
    s1 = await say(sid1, "42", "Alice Moreau", "I will get two quotes for the tank.")
    fakes.llm.add("tick", {"ops": [{"op": "action.add", "title": "Get two quotes for the tank", "assignees": ["Alice Moreau"], "evidence": [s1]}]})
    await rt.tick(sid1)
    a = query(lambda db: db.query(MeetppAction).filter_by(session_id=sid1).one())
    db = SessionLocal()
    try:
        s = db.get(MeetppSession, sid1)
        s.status, s.ended_at = "published", util.now()
        db.get(MeetppAction, a.id).status = "open"
        db.commit()
    finally:
        db.close()

    sid2 = make_session(agenda=AGENDA, series_id=series_id)
    ids2 = sids(sid2)
    assert get(MeetppSession, sid2).series_id == series_id
    # Open previous action → Previous actions applies in the new session.
    from app.meetpp.models import MeetppSection

    assert get(MeetppSection, ids2["Previous actions"]).status == "pending"
    await start(sid2)
    await join(sid2, "42", "Alice Moreau")
    s2 = await say(sid2, "42", "Alice Moreau", "One quote is in, the second comes next week.")
    fakes.llm.add("tick", {"ops": [{"op": "action.update", "ref": a.ref, "report_note": "One quote received.", "progress_note": "Second quote next week.", "evidence": [s2]}]})
    await rt.tick(sid2)
    r = query(lambda db: db.query(MeetppActionReport).filter_by(action_id=a.id, session_id=sid2).one())
    assert r.note == "One quote received."
    db = SessionLocal()
    try:
        session2 = db.get(MeetppSession, sid2)
        state = ops.build_state(db, session2)
        exp = export_mod.build_export(db, session2)
    finally:
        db.close()
    dto = next(x for x in state["actions"] if x["id"] == a.id)
    assert dto["previous"] and dto["carried_forward"] and dto["section_id"] == ids2["Previous actions"]
    assert dto["report"]["note"] == "One quote received." and dto["report"]["status"] == "open"
    assert "Second quote next week." in dto["progress_notes"]
    row = next(x for x in exp["actions"] if x["ref"] == a.ref)
    assert row["carried_forward"] and row["reported_note"] == "One quote received."
    assert row["also_on"] == [{"title": "Board meeting #9", "date_iso": util.iso(get(MeetppSession, sid1).started_at), "raised": True}]
    # Duplicate of a previous open action is refused.
    s3 = await say(sid2, "42", "Alice Moreau", "Get two quotes for the tank, yes.")
    fakes.llm.add("tick", {"ops": [{"op": "action.add", "title": "Get two quotes for the tank", "evidence": [s3]}]})
    res = await rt.tick(sid2)
    assert "use action.update" in res["rejected"][0]["reason"]


async def test_snapshot_dto_keys(fakes):
    sid = await _board(fakes)
    s1 = await say(sid, "42", "Alice Moreau", "We approve. Ben will file it.")
    fakes.llm.add("tick", {"ops": [
        {"op": "decision.add", "title": "Approve", "status": "adopted", "vote": {"method": "voice", "for": 2}, "evidence": [s1]},
        {"op": "action.add", "title": "File the minutes", "assignees": ["Ben Hartley"], "from_decision": "D-1", "evidence": [s1]},
    ], "notes": [{"text": "Approved.", "evidence": [s1]}]})
    await rt.tick(sid)
    db = SessionLocal()
    try:
        from app.meetpp.models import MeetppAttachment

        db.add(MeetppAttachment(id=util.ulid(), session_id=sid, kind="whiteboard", path="/tmp/x.png", filename="whiteboard.png", caption="Board"))
        db.commit()
        state = ops.build_state(db, db.get(MeetppSession, sid))
    finally:
        db.close()
    assert set(state) == {"v", "type", "sid", "version", "session", "sections", "decisions", "actions", "minutes",
                          "attendees", "attachments", "documents", "quorum"}
    assert state["v"] == 1 and state["type"] == "state"
    assert set(state["session"]) == SESSION_META_KEYS
    assert set(state["session"]["settings"]) == {"show_public", "in_recordings", "timebox_nudges", "speak"}
    assert set(state["session"]["agent"]) == {"status", "backlog_s", "speakers", "tier2"}
    assert set(state["session"]["ai"]) == {"status"}
    assert all(set(s) == SECTION_KEYS for s in state["sections"])
    assert all(set(s["counts"]) == {"decisions", "actions"} for s in state["sections"])
    assert all(set(d) == DECISION_KEYS for d in state["decisions"])
    assert all(set(a) == ACTION_KEYS for a in state["actions"])
    assert all(set(m) == MINUTE_KEYS for m in state["minutes"])
    assert all(set(n) == {"text", "evidence", "at"} for m in state["minutes"] for n in m["notes"])
    assert all(set(a) == ATTENDEE_KEYS for a in state["attendees"])
    assert all(set(a) == ATTACHMENT_KEYS for a in state["attachments"])
    assert set(state["quorum"]) == {"required", "voting_present", "voting_total", "met"}
    action = state["actions"][0]
    assert action["decision_ref"] == "D-1" or action["decision_ref"].startswith("D-")
    assert set(action["assignees"][0]) == {"name", "person_key"}
    seg = query(lambda db: db.query(__import__("app.meetpp.models", fromlist=["MeetppSegment"]).MeetppSegment).filter_by(session_id=sid).first())
    assert set(ops.segment_dto(seg)) == SEGMENT_KEYS


def _keys(d: dict) -> set[str]:
    return set(d)


async def test_export_shape_matches_contract(fakes):
    sid = await _board(fakes)
    ids = sids(sid)
    s1 = await say(sid, "42", "Alice Moreau", "The minutes are approved by both.")
    fakes.llm.add("tick", {"ops": [
        {"op": "decision.add", "section": "S3", "title": "Approve the minutes", "resolution": "that the minutes are approved",
         "status": "adopted", "vote": {"method": "voice", "for": 2, "against": 0, "abstain": 0,
                                        "ballots": [{"person": "Alice Moreau", "choice": "for"}, {"person": "Ben", "choice": "yes"}]},
         "evidence": [s1]},
        {"op": "action.add", "section": "S3", "title": "Sign the approved minutes", "assignees": ["Alice Moreau"], "due": "2026-10-30",
         "from_decision": "D-1", "evidence": [s1]},
    ]})
    await rt.tick(sid)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        db.add(MeetppDecision(id=util.ulid(), session_id=sid, series_id=session.series_id, ref="D-99", section_id=ids["Quotes"],
                              title="Choose a supplier", status="pending", origin="pdf"))
        db.commit()
        exp = export_mod.build_export(db, session)
    finally:
        db.close()
    assert _keys(exp) == {"org", "meeting", "generated_at_iso", "attendance", "agenda", "decisions", "actions", "papers",
                          "minutes", "next_agenda", "next_meeting"}
    assert _keys(exp["org"]) == {"name", "tagline", "seat", "enterprise", "rpr", "email", "website", "iban", "logo_path", "placeholders"}
    assert exp["org"]["placeholders"]
    assert _keys(exp["meeting"]) == {"title", "series_title", "date_iso", "location", "type_label", "status_label",
                                     "convened_at_iso", "adjourned_at_iso", "quorum_note", "formal"}
    assert exp["meeting"]["type_label"] == "Board meeting" and exp["meeting"]["formal"] is True
    assert exp["meeting"]["status_label"] == "Held" and exp["meeting"]["quorum_note"].startswith("Quorum met (2 of 3 directors")
    assert _keys(exp["attendance"]) == {"summary", "rows"}
    assert _keys(exp["attendance"]["summary"]) == {"present", "represented", "absent", "excused", "not_registered"}
    assert all(_keys(r) == {"name", "username", "status_label", "represented_by", "mandate"} for r in exp["attendance"]["rows"])
    assert exp["agenda"][1] == {"number": "2", "title": "Rainwater tank", "body": None,
                                "subpoints": [{"label": "a", "title": "Quotes", "body": None}]}
    # Pending decisions that were never taken are left out.
    assert [d["ref"] for d in exp["decisions"]] == ["D-1"]
    dec = exp["decisions"][0]
    assert _keys(dec) == {"number", "ref", "title", "resolution", "how_taken", "status_label", "decided_at_iso", "agenda_number", "vote"}
    assert dec["status_label"] == "Adopted" and dec["agenda_number"] == "1"
    vote = dec["vote"]
    assert _keys(vote) == {"question", "for", "against", "abstain", "result_label", "voting_body_label", "majority_label", "eligible",
                           "basis_label", "present_or_represented", "quorum_required", "quorum_met", "outcome_sentence", "ballots"}
    assert (vote["voting_body_label"], vote["majority_label"], vote["basis_label"]) == ("Board", "Simple majority", "Directors in office")
    assert vote["outcome_sentence"] == (
        "Adopted by the board on an ordinary majority of the votes cast. 2 votes in favour, 0 against, 0 abstentions. "
        "3 directors entitled to vote, 2 present or represented (quorum 2)."
    )
    assert vote["ballots"] == [
        {"name": "Alice Moreau", "vote_label": "For", "cast_by": "Alice Moreau", "proxy": False},
        {"name": "Ben Hartley", "vote_label": "For", "cast_by": "Ben Hartley", "proxy": False},
    ]
    act = exp["actions"][0]
    assert _keys(act) == {"number", "ref", "title", "carried_forward", "description", "status_label", "assignees", "due_iso",
                          "completed_iso", "reported_note", "progress_notes", "completion_note", "also_on", "from_decision"}
    assert act["status_label"] == "Proposed" and act["assignees"] == ["Alice Moreau"] and act["from_decision"] == "Approve the minutes"
    assert act["carried_forward"] is False and act["also_on"] == []
    assert _keys(exp["minutes"]) == {"saved_at_iso", "version", "markdown"}
    assert exp["minutes"]["markdown"].startswith("# Minutes — Board meeting #9")
    assert exp["next_meeting"] is None and exp["next_agenda"] == []
    await drain()


async def test_informal_export_has_no_votes_or_quorum(fakes):
    sid = make_session(agenda=AGENDA)
    await start(sid)
    await join(sid, "42", "Alice Moreau")
    s1 = await say(sid, "42", "Alice Moreau", "Agreed.")
    fakes.llm.add("tick", {"ops": [{"op": "decision.add", "title": "Agree", "status": "adopted", "vote": {"method": "voice", "for": 1}, "evidence": [s1]}]})
    await rt.tick(sid)
    db = SessionLocal()
    try:
        exp = export_mod.build_export(db, db.get(MeetppSession, sid))
        state = ops.build_state(db, db.get(MeetppSession, sid))
    finally:
        db.close()
    assert exp["meeting"]["formal"] is False and exp["meeting"]["quorum_note"] is None
    assert exp["decisions"][0]["vote"] is None
    assert state["quorum"] is None
    assert query(lambda db: db.query(MeetppVote).count()) >= 1


async def test_report_renders_from_export(fakes):
    sid = await _board(fakes)
    db = SessionLocal()
    try:
        exp = export_mod.build_export(db, db.get(MeetppSession, sid))
    finally:
        db.close()
    from app.meetpp import report

    pdf = report.render_meeting_report(exp)
    assert pdf.startswith(b"%PDF")
    exp["next_meeting"] = {"date_iso": "2026-11-01T17:00:00Z", "location": "https://meet.example/x"}
    exp["next_agenda"] = [{"number": "1", "title": "Tank installation", "body": None}]
    assert report.render_agenda(exp).startswith(b"%PDF")

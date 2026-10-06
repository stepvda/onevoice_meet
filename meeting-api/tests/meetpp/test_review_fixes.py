"""Review fixes: guest identity, tick races, the interpretation window, vote
counts, stale writes after model calls, composition in flight, list fields
from the model, attendance after the end, bus audience and in-memory state."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta

from app.db import SessionLocal
from app.meetpp import bus, commands, compose, governance, ingest, llm, ops, runtime as rt, util
from app.meetpp.models import (
    MeetppAction,
    MeetppActionReport,
    MeetppAttendee,
    MeetppBallot,
    MeetppConsent,
    MeetppDecision,
    MeetppDocument,
    MeetppMinute,
    MeetppSection,
    MeetppSegment,
    MeetppSession,
)

from .conftest import add_roster, drain, get, join, make_session, query, say, sids, start
from .test_finalise import _meeting_with_decision
from .test_tick import pid, running


def _seg(uid, identity, text, name="Guest", ago=0.0):
    t0 = util.now() - timedelta(seconds=ago)
    return {"utterance_id": uid, "identity": identity, "name": name, "t_start": util.iso(t0),
            "t_end": util.iso(t0 + timedelta(seconds=2)), "text": text}


def _set_status(sid: str, status: str) -> None:
    db = SessionLocal()
    try:
        db.get(MeetppSession, sid).status = status
        db.commit()
    finally:
        db.close()


# ─── 1. guest identity ──────────────────────────────────────────────────────


async def test_a_guest_key_claim_cannot_take_over_a_connected_guest(fakes):
    sid = make_session()
    await start(sid)
    key = "guest:7f1c2a9e-aaaa-4222-8333-444455556666"
    await rt.presence(sid, [{"identity": "anon-AAA", "name": "Gina", "kind": "standard", "event": "connected"}])
    await rt.set_consent(sid, "anon-AAA", "opt_out", key, "Gina")
    # Another guest posts Gina's key while she is still connected.
    await rt.presence(sid, [{"identity": "anon-EVE", "name": "Eve", "kind": "standard", "event": "connected"}])
    res = await rt.set_consent(sid, "anon-EVE", "accept", key, "Eve")
    assert res["person_key"] == "id:anon-EVE"
    consents = {c.person_key: (c.identity, c.decision) for c in query(lambda db: db.query(MeetppConsent).filter_by(session_id=sid).all())}
    assert consents[key] == ("anon-AAA", "opt_out")
    gina = query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid, person_key=key).one())
    assert gina.opted_out and util.loads(gina.identities_json, []) == ["anon-AAA"]
    # Eve's speech is Eve's; Gina's opt-out still holds.
    r = await rt.ingest(sid, {"segments": [_seg("e1", "anon-EVE", "Hello, I am Eve."), _seg("g1", "anon-AAA", "I opted out.")]})
    assert r["seqs"] == {"e1": 1}
    assert query(lambda db: db.query(MeetppSegment).filter_by(session_id=sid, seq=1).one()).person_key == "id:anon-EVE"
    assert "anon-AAA" not in fakes.agent.named("patch")[-1]["accepted_identities"]


async def test_roster_members_cannot_be_claimed_by_key_or_by_a_typed_name(fakes):
    sid = make_session(meeting_type="board")
    series_id = get(MeetppSession, sid).series_id
    add_roster(series_id, "guest:roster-member-0001", "Roger Voter")
    add_roster(series_id, "name:david stone", "David Stone")
    await start(sid)
    await rt.presence(sid, [{"identity": "anon-X", "name": "David Stone", "kind": "standard", "event": "connected"}])
    res = await rt.set_consent(sid, "anon-X", "accept", "guest:roster-member-0001", "David Stone")
    assert res["person_key"] == "id:anon-X"
    await rt.set_consent(sid, "anon-Y", "accept", "guest:fresh-key-0001", "David Stone")
    rows = {a.person_key: a for a in query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid).all())}
    assert rows["guest:roster-member-0001"].status == "not_registered" and rows["guest:roster-member-0001"].identities_json == "[]"
    assert rows["name:david stone"].status == "not_registered"
    assert not rows["id:anon-X"].voting and not rows["guest:fresh-key-0001"].voting
    assert governance.quorum(SessionLocal(), get(MeetppSession, sid))["voting_present"] == 0


async def test_guest_keys_are_shown_by_an_alias_and_ballots_map_back(fakes):
    sid = make_session(meeting_type="board")
    await start(sid)
    key = "guest:7f1c2a9e-bbbb-4222-8333-444455556666"
    await rt.set_consent(sid, "anon-AAA", "accept", key, "Gina Guest")
    await join(sid, "42", "Alice Moreau")
    await rt.ingest(sid, {"segments": [_seg("g1", "anon-AAA", "Hello from Gina.", "Gina Guest")]})
    alias = util.public_person_key(sid, key)
    assert alias.startswith("guest:~") and not util.valid_guest_key(alias)
    assert util.public_person_key(make_session(), key) != alias  # per session
    assert fakes.bus.last("caption")["person_key"] == alias
    assert key not in json.dumps([m for _r, m, _d in fakes.bus.sent])
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        state = ops.build_state(db, session)
        assert {a["person_key"] for a in state["attendees"]} == {alias, "sub:42"}
        seg = db.query(MeetppSegment).filter_by(session_id=sid, seq=1).one()
        assert seg.person_key == key and ops.segment_dto(seg)["person_key"] == alias
        # The chair's vote form sends the alias back: stored under the real key.
        d = MeetppDecision(id=util.ulid(), session_id=sid, series_id=session.series_id, ref="D-1", title="Hall",
                           status="proposed", origin="user", evidence_json="[]")
        db.add(d)
        db.flush()
        vote = governance.apply_vote(db, session, d, {"method": "roll_call", "ballots": [
            {"name": "Gina Guest", "person_key": alias, "choice": "for"},
            {"name": "Alice Moreau", "person_key": "guest:someone-else-01", "choice": "against"},
        ]})
        db.commit()
        ballots = {b.name: b.person_key for b in db.query(MeetppBallot).filter_by(vote_id=vote.id).all()}
        assert ballots == {"Gina Guest": key, "Alice Moreau": "sub:42"}
        assert {b["person_key"] for b in ops.decision_dto(d, vote, db.query(MeetppBallot).filter_by(vote_id=vote.id).all())["vote"]["ballots"]} == {alias, "sub:42"}
    finally:
        db.close()


# ─── 13. attendance is fixed once the meeting ended ──────────────────────────


async def test_presence_and_consent_after_the_end_change_nothing(fakes):
    sid = make_session()
    db = SessionLocal()
    try:
        assert db.get(MeetppSession, sid).status == "setup"
    finally:
        db.close()
    await rt.set_consent(sid, "user-42", "accept", None, "Alice Moreau")  # setup: recorded
    await start(sid)
    await rt.end_session(sid)
    await drain()
    await rt.presence(sid, [{"identity": "user-50", "name": "Late Larry", "kind": "standard", "event": "connected"}])
    try:
        await rt.set_consent(sid, "user-51", "accept", None, "Later Lou")
    except rt.ConsentError as exc:
        assert exc.status == 409
    else:
        raise AssertionError("consent after the end must be refused")
    keys = {a.person_key for a in query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid).all())}
    assert keys == {"sub:42"}


# ─── 2. tick races ──────────────────────────────────────────────────────────


async def test_a_tick_in_flight_when_the_meeting_ends_applies_nothing(fakes):
    sid = await running(fakes)
    ids = sids(sid)
    s1 = await say(sid, "42", "Alice Moreau", "On to item one. I move that we approve the minutes. Agreed.")
    S = pid(sid, "Approval of the minutes")

    def ended_meanwhile(_messages):
        _set_status(sid, "finalising")
        return {"topic": {"section": S, "confidence": 0.95}, "advance": {"to": S, "confidence": 0.95},
                "ops": [{"op": "decision.add", "section": S, "title": "Approve the minutes", "status": "adopted", "evidence": [s1]}]}

    fakes.llm.add("tick", ended_meanwhile)
    before = len(fakes.bus.sent)
    assert await rt.tick(sid) == {"skipped": "status"}
    await drain()
    assert get(MeetppSession, sid).live_section_id == ids["Opening"]
    assert query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).count()) == 0
    assert [m["type"] for _r, m, _d in fakes.bus.sent[before:]] == []


async def _tick_with(fakes, monkeypatch, sid: str, during_broadcast) -> None:
    """Run a tick whose AI move is decided, and call `during_broadcast()` while
    the tick's state message is being sent."""
    S = pid(sid, "Approval of the minutes")
    await say(sid, "42", "Alice Moreau", "Then on to item one, the minutes.")
    fakes.llm.add("tick", {"topic": {"section": S, "confidence": 0.9}, "advance": {"to": S, "confidence": 0.92, "reason": "item one"}})
    real_send, fired = bus.send, []

    async def send(room, msg, destination_identities=None):
        await real_send(room, msg, destination_identities)
        if msg.get("type") == "state" and fakes.llm.calls and not fired:
            fired.append(True)
            await during_broadcast()

    monkeypatch.setattr(bus, "send", send)
    await rt.tick(sid)
    await drain()
    assert fired


async def test_a_chair_move_during_the_tick_broadcast_wins(fakes, monkeypatch):
    sid = await running(fakes)
    ids = sids(sid)
    await _tick_with(fakes, monkeypatch, sid, lambda: rt.chair_move(sid, "move", ids["Budget 2027"]))
    assert get(MeetppSession, sid).live_section_id == ids["Budget 2027"]
    live = query(lambda db: db.query(MeetppSection).filter_by(session_id=sid, status="live").all())
    assert [s.id for s in live] == [ids["Budget 2027"]]
    assert [m["by"] for m in fakes.bus.of("position")] == ["chair"]


async def test_an_end_during_the_tick_broadcast_stops_the_ai_move(fakes, monkeypatch):
    sid = await running(fakes)
    await _tick_with(fakes, monkeypatch, sid, lambda: rt.end_session(sid))
    session = get(MeetppSession, sid)
    assert session.status == "finalising"
    assert query(lambda db: db.query(MeetppSection).filter_by(session_id=sid, status="live").count()) == 0
    assert fakes.bus.of("position") == []
    assert [m for m in fakes.bus.of("announce") if m["kind"] == "position"] == []


async def test_finalisation_does_not_start_while_a_tick_is_in_flight(fakes, monkeypatch):
    sid = await running(fakes)
    runner = rt.SessionRunner(sid)
    runner._last_renew = time.monotonic()
    runner.tick_task = asyncio.get_running_loop().create_task(asyncio.Event().wait())
    seen = []

    async def final(s, **_kw):
        seen.append(runner.tick_task.done())

    monkeypatch.setattr(rt, "run_finalisation", final)
    _set_status(sid, "finalising")
    assert await runner._iteration()
    await runner.final_task
    assert seen == [True] and runner.tick_task.cancelled()


# ─── 6. the interpretation window ───────────────────────────────────────────


async def test_the_window_never_leaves_an_earlier_seq_behind(fakes):
    sid = await running(fakes)
    a = await say(sid, "42", "Alice Moreau", "First point, early on.", seconds_ago=200)
    await say(sid, "43", "Ben Hartley", "A remark well after the first window.", seconds_ago=0)
    c = await say(sid, "42", "Alice Moreau", "A late line that was spoken early.", seconds_ago=150)
    fakes.llm.add("tick", {}, {})
    res = await rt.tick(sid)
    assert res["more"] and get(MeetppSession, sid).transcript_cursor == a
    await rt.tick(sid)
    assert get(MeetppSession, sid).transcript_cursor == c
    first, second = fakes.llm.prompts("tick")
    assert "A remark well after" not in first and "A remark well after" in second


# ─── 3. vote counts ─────────────────────────────────────────────────────────


async def test_a_count_entered_by_a_person_is_never_rewritten(fakes):
    sid = await running(fakes, meeting_type="board")  # two voting members present
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        d = MeetppDecision(id=util.ulid(), session_id=sid, series_id=session.series_id, ref="D-1", title="Hall",
                           status="proposed", origin="user", evidence_json="[]")
        db.add(d)
        db.flush()
        vote = governance.apply_vote(db, session, d, {"method": "show_of_hands", "for": 5, "against": 1, "abstain": 0})
        assert (vote.tally_for, vote.present_count, vote.result) == (5, 2, "adopted")
        db.commit()
    finally:
        db.close()


# ─── 5. stale writes after a model call ─────────────────────────────────────


async def test_reconcile_keeps_what_the_chair_changed_while_the_model_read(fakes):
    sid = await _meeting_with_decision(fakes)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        for ref, title in (("A-1", "Ask two suppliers for a tank quote"), ("A-2", "Order the tank")):
            db.add(MeetppAction(id=util.ulid(), series_id=session.series_id, session_id=sid, ref=ref, title=title,
                                status="open", origin="pdf"))
        minute = compose.outline_mod.minute_for(db, session, sids(sid)["Approval of the minutes"])
        minute.narrative_md = "The chair reported that both tank quotes had been received and the tank was ordered."
        db.commit()
    finally:
        db.close()
    quote = "The chair reported that both tank quotes had been received and the tank was ordered."

    def chair_confirms_a1(_messages):
        db = SessionLocal()
        try:
            a = db.query(MeetppAction).filter_by(session_id=sid, ref="A-1").one()
            a.status, a.locked = "in_progress", True
            db.commit()
        finally:
            db.close()
        return {"reports": [{"ref": "A-1", "status": "done", "note": "Received.", "quote": quote},
                            {"ref": "A-2", "status": "done", "note": "Ordered.", "quote": quote}]}

    fakes.llm.add("compose_actions", chair_confirms_a1)
    assert await compose.reconcile_previous_actions(sid) == 1
    rows = {a.ref: a for a in query(lambda db: db.query(MeetppAction).filter_by(session_id=sid).all())}
    assert (rows["A-1"].status, rows["A-1"].locked) == ("in_progress", True) and rows["A-2"].status == "done"
    assert query(lambda db: db.query(MeetppActionReport).filter_by(action_id=rows["A-1"].id).count()) == 0


async def test_verify_closed_keeps_an_action_the_chair_confirmed_meanwhile(fakes):
    sid = await _meeting_with_decision(fakes)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        db.add(MeetppAction(id=util.ulid(), series_id=session.series_id, session_id=sid, ref="A-1", title="Prove the restore",
                            status="done", completed_at=util.now(), origin="pdf"))
        db.commit()
    finally:
        db.close()

    def chair_confirms(_messages):
        db = SessionLocal()
        try:
            db.query(MeetppAction).filter_by(session_id=sid, ref="A-1").one().locked = True
            db.commit()
        finally:
            db.close()
        return {"confirmed": []}

    fakes.llm.add("compose_actions", chair_confirms)
    assert await compose.verify_closed_previous_actions(sid) == 0
    a = query(lambda db: db.query(MeetppAction).filter_by(session_id=sid, ref="A-1").one())
    assert a.status == "done" and a.locked


async def test_an_agenda_read_after_the_start_does_not_replace_the_outline(fakes):
    sid = make_session()
    before = [s.title for s in query(lambda db: db.query(MeetppSection).filter_by(session_id=sid).all())]

    def started_meanwhile(_messages):
        _set_status(sid, "running")
        return {"points": [{"title": "Something else entirely"}]}

    fakes.llm.add("parse_agenda", started_meanwhile)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        doc = MeetppDocument(id=util.ulid(), session_id=sid, kind="agenda", filename="a.pdf", status="parsing")
        db.add(doc)
        db.commit()
        # Plain text without numbered points: the model structures it.
        await ingest.import_text(db, session, doc, "Agenda\nSomething to talk about at some point")
        db.commit()
        structured = util.loads(doc.structured_json, {})
    finally:
        db.close()
    assert structured.get("not_applied")
    after = [s.title for s in query(lambda db: db.query(MeetppSection).filter_by(session_id=sid).all())]
    assert after == before


# ─── 9. compositions in flight ──────────────────────────────────────────────


async def test_the_final_compose_waits_for_a_composition_in_flight(fakes, monkeypatch):
    gate, calls = asyncio.Event(), []

    async def slow(session_id, section_id, *, force):
        calls.append(section_id)
        if len(calls) == 1:
            await gate.wait()
        return "composed"

    monkeypatch.setattr(compose, "_compose_section", slow)
    live = asyncio.get_running_loop().create_task(compose.compose_section("S", "X"))
    await asyncio.sleep(0)
    assert await compose.compose_section("S", "X") == "composing"
    final = asyncio.get_running_loop().create_task(compose.compose_section("S", "X", wait=True))
    await asyncio.sleep(0.01)
    assert not final.done() and calls == ["X"]
    gate.set()
    assert await final == "composed" and await live == "composed"
    assert calls == ["X", "X"] and ("S", "X") not in compose._in_flight


async def test_the_compose_job_waits_for_compositions_in_flight(fakes, monkeypatch):
    sid = make_session()
    seen = []

    async def fake(session_id, section_id, *, force=False, wait=False):
        seen.append(wait)
        return "composed"

    monkeypatch.setattr(compose, "sections_to_compose", lambda db, session: ["X", "Y"])
    monkeypatch.setattr(compose, "compose_section", fake)
    await rt._job_compose(sid)
    assert seen == [True, True]


# ─── 10. list fields from the model ─────────────────────────────────────────


def test_as_list():
    assert util.as_list(["a"]) == ["a"] and util.as_list("Check the date") == ["Check the date"]
    assert util.as_list("  ") == [] and util.as_list({"a": 1}) == [] and util.as_list(None) == [] and util.as_list(3) == []


async def test_a_string_where_a_list_is_expected_is_one_item(fakes):
    from .test_finalise import SECTION_MD

    sid = await _meeting_with_decision(fakes)
    ids = sids(sid)
    fakes.llm.add("compose_section", {"markdown": SECTION_MD, "verify": "the meeting number"})
    assert await compose.compose_section(sid, ids["Approval of the minutes"]) == "composed"
    m = query(lambda db: db.query(MeetppMinute).filter_by(session_id=sid, section_id=ids["Approval of the minutes"]).one())
    assert util.loads(m.verify_json, []) == ["the meeting number"]
    fakes.llm.add("compose_final", {"opening": "Opened.", "summary": "One line.", "next_agenda": "Tank installation", "verify": 7})
    final = await compose.compose_final(sid)
    assert final["summary"] == ["One line."] and final["next_agenda"][0]["title"] == "Tank installation"


async def test_a_composition_that_raises_is_stored_as_failed(fakes, monkeypatch):
    sid = await _meeting_with_decision(fakes)
    ids = sids(sid)

    async def boom(*_a, **_kw):
        raise TypeError("'int' object is not iterable")

    monkeypatch.setattr(compose, "_compose_body", boom)
    assert await compose.compose_section(sid, ids["Approval of the minutes"]) == "failed"
    m = query(lambda db: db.query(MeetppMinute).filter_by(session_id=sid, section_id=ids["Approval of the minutes"]).one())
    assert m.status == "failed" and "not iterable" in m.error


# ─── 15. in-memory state of finished sessions ───────────────────────────────


async def test_a_finished_session_leaves_no_runner_or_state(fakes, monkeypatch):
    sid = make_session()
    _set_status(sid, "review")

    async def ok(*_a):
        return True

    monkeypatch.setattr(rt._Lease, "acquire", ok)
    monkeypatch.setattr(rt._Lease, "release", ok)
    runner = rt.runtime.get(sid)
    rt._last_announce[sid] = ("x", time.monotonic())
    commands._last[(sid, "next")] = time.monotonic()
    llm.mark_tick(sid, False)
    runner._last_renew = time.monotonic()
    runner.wake.set()
    await runner.run()
    assert sid not in rt.runtime.runners and sid not in rt._last_announce
    assert (sid, "next") not in commands._last and sid not in llm._tick_failing
    # Deleting a session forgets it too.
    rt._last_announce[sid] = ("x", time.monotonic())
    await rt.runtime.drop(sid)
    assert sid not in rt._last_announce


# ─── 7. bus audience ────────────────────────────────────────────────────────


class _FakeRoomApi:
    def __init__(self, identities, fail=False):
        self.identities, self.fail = identities, fail
        self.sent: list = []
        self.lists = 0

    async def list_participants(self, req):
        self.lists += 1
        if self.fail:
            raise RuntimeError("livekit down")

        class P:
            def __init__(self, identity):
                self.identity = identity

        class R:
            participants = [P(i) for i in self.identities]

        return R()

    async def send_data(self, req):
        self.sent.append(list(req.destination_identities))


async def test_room_messages_skip_viewers_unless_the_session_is_public(monkeypatch):
    room_api = _FakeRoomApi(["user-42", "anon-1", "viewer-9"])

    class LK:
        room = room_api

    monkeypatch.setattr(bus, "_lk", lambda: LK())
    monkeypatch.setattr(bus, "_participants", {})
    monkeypatch.setattr(bus, "_public", {"r1": False, "r2": False, "r3": False})
    assert await bus.send("r1", {"type": "caption"})
    assert await bus.send("r1", {"type": "caption"})
    assert room_api.sent == [["user-42", "anon-1"], ["user-42", "anon-1"]] and room_api.lists == 1  # cached
    # Explicit destinations are kept as they are.
    await bus.send("r1", {"type": "proposal"}, ["user-42"])
    assert room_api.sent[-1] == ["user-42"]
    # Public: everyone (no destination list).
    bus._public["r1"] = True
    await bus.send("r1", {"type": "caption"})
    assert room_api.sent[-1] == []
    # No viewer in the room: a plain broadcast.
    room_api.identities = ["user-42"]
    await bus.send("r2", {"type": "caption"})
    assert room_api.sent[-1] == []
    # Listing fails with nothing cached: broadcast (logged).
    room_api.fail = True
    await bus.send("r3", {"type": "caption"})
    assert room_api.sent[-1] == []


async def test_the_public_flag_follows_the_room_metadata(monkeypatch):
    import app.room_metadata as room_metadata

    async def patch(_lk, _room, change, require_room=False):
        md: dict = {}
        change(md)
        return md

    monkeypatch.setattr(room_metadata, "patch_room_metadata", patch)
    monkeypatch.setattr(bus, "_public", {})
    monkeypatch.setattr(bus, "_lk", lambda: None)
    await bus.set_room_meetpp("r9", {"sid": "S", "public": True})
    assert bus._public == {"r9": True}
    await bus.set_room_meetpp("r9", {"sid": "S", "public": False})
    assert bus._public == {"r9": False}

"""Agent ingest: seq assignment, refinements, gaps, consent per person across
identity changes, echo guard, presence and the roster (FDD §7.5, §8.6)."""
from __future__ import annotations

import time
from datetime import timedelta

from app.db import SessionLocal
from app.meetpp import runtime as rt, util
from app.meetpp.models import MeetppAttendee, MeetppConsent, MeetppRoster, MeetppSegment, MeetppSession

from .conftest import add_roster, get, join, make_session, query, start


def _seg(uid, identity, text, name="Guest Gina", ago=0.0):
    t0 = util.now() - timedelta(seconds=ago)
    return {"utterance_id": uid, "identity": identity, "name": name, "t_start": util.iso(t0), "t_end": util.iso(t0 + timedelta(seconds=2)), "text": text}


async def test_seq_assignment_refinement_and_gap(fakes):
    sid = make_session()
    await start(sid)
    await join(sid, "42", "Alice Moreau")
    res = await rt.ingest(sid, {"segments": [_seg("u1", "user-42", "Hello all.", "Alice Moreau"), _seg("u2", "user-42", "Second line.", "Alice Moreau")]})
    assert res["ok"] and res["seqs"] == {"u1": 1, "u2": 2}
    # Retried batch (agent retry buffer) is idempotent.
    res = await rt.ingest(sid, {"segments": [_seg("u2", "user-42", "Second line.", "Alice Moreau")]})
    assert res["seqs"] == {"u2": 2}
    assert query(lambda db: db.query(MeetppSegment).filter_by(session_id=sid).count()) == 2
    cap = fakes.bus.of("caption")
    assert [c["seq"] for c in cap] == [1, 2]
    assert set(cap[0]) == {"v", "type", "sid", "seq", "identity", "name", "person_key", "t_start", "text", "tier"}
    assert cap[0]["person_key"] == "sub:42"
    res = await rt.ingest(sid, {"refinements": [{"utterance_id": "u1", "text": "Hello, all.", "final": False}],
                                "gaps": [{"identity": "user-42", "name": "Alice Moreau", "t_from": util.iso(util.now()), "t_to": util.iso(util.now()), "reason": "agent_reconnect"}]})
    seg = query(lambda db: db.query(MeetppSegment).filter_by(session_id=sid, seq=1).one())
    assert seg.text_refined == "Hello, all." and seg.tier == 2 and seg.best_text == "Hello, all."
    upd = fakes.bus.last("caption-update")
    assert upd == {"v": 1, "type": "caption-update", "sid": sid, "seq": 1, "text": "Hello, all.", "tier": 2}
    gap = fakes.bus.last("gap")
    assert gap["seq"] == 3 and gap["reason"] == "agent_reconnect" and gap["name"] == "Alice Moreau"
    gap_row = query(lambda db: db.query(MeetppSegment).filter_by(session_id=sid, seq=3).one())
    assert gap_row.is_gap and gap_row.gap_reason == "agent_reconnect"
    a = query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid, person_key="sub:42").one())
    assert a.talk_seconds >= 4
    # Tier 2 first: the Mac Studio's text arrives as the live line itself.
    res = await rt.ingest(sid, {"segments": [dict(_seg("u3", "user-42", "Third line.", "Alice Moreau"), tier=2)]})
    seg = query(lambda db: db.query(MeetppSegment).filter_by(session_id=sid, utterance_id="u3").one())
    assert seg.tier == 2 and seg.text_refined == "Third line." and seg.best_text == "Third line."
    assert fakes.bus.last("caption")["tier"] == 2


async def test_default_deny_and_opt_out(fakes):
    sid = make_session()
    await start(sid)
    await rt.presence(sid, [{"identity": "user-50", "name": "No Consent", "kind": "standard", "event": "connected"}])
    res = await rt.ingest(sid, {"segments": [_seg("x1", "user-50", "I have not consented.")]})
    assert res["seqs"] == {}
    await join(sid, "51", "Opting Out", consent="opt_out")
    res = await rt.ingest(sid, {"segments": [_seg("x2", "user-51", "I opted out.")]})
    assert res["seqs"] == {}
    a = query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid, person_key="sub:51").one())
    assert a.opted_out


async def test_guest_consent_survives_reconnect_with_new_identity(fakes):
    sid = make_session()
    await start(sid)
    key = "guest:7f1c2a9e-1111-4222-8333-444455556666"
    await rt.presence(sid, [{"identity": "anon-AAA", "name": "Guest Gina", "kind": "standard", "event": "connected"}])
    await rt.set_consent(sid, "anon-AAA", "accept", key, "Guest Gina")
    res = await rt.ingest(sid, {"segments": [_seg("g1", "anon-AAA", "Hi from the guest.")]})
    assert res["seqs"] == {"g1": 1}
    # Reload: the old connection leaves, a new LiveKit identity posts the same
    # guest key (rebinding needs the previous connection gone).
    await rt.presence(sid, [{"identity": "anon-AAA", "name": "Guest Gina", "kind": "standard", "event": "disconnected"}])
    await rt.presence(sid, [{"identity": "anon-BBB", "name": "Guest Gina", "kind": "standard", "event": "connected"}])
    await rt.set_consent(sid, "anon-BBB", "accept", key, "Guest Gina")
    res = await rt.ingest(sid, {"segments": [_seg("g2", "anon-BBB", "Back again.")]})
    assert res["seqs"] == {"g2": 2}
    attendees = query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid).all())
    assert len(attendees) == 1
    a = attendees[0]
    assert a.person_key == key and set(util.loads(a.identities_json, [])) == {"anon-AAA", "anon-BBB"}
    assert query(lambda db: db.query(MeetppConsent).filter_by(session_id=sid).count()) == 1
    seg = query(lambda db: db.query(MeetppSegment).filter_by(session_id=sid, seq=2).one())
    assert seg.person_key == key
    # The agent receives the accepted identities of the person.
    patch = fakes.agent.named("patch")[-1]
    assert {"anon-AAA", "anon-BBB"} <= set(patch["accepted_identities"])
    # Guests are not voting members by default; roster keeps the guest key.
    assert not a.voting
    r = query(lambda db: db.query(MeetppRoster).filter_by(person_key=key).one())
    assert not r.voting


async def test_invalid_guest_key_rejected(fakes):
    sid = make_session()
    await start(sid)
    try:
        await rt.set_consent(sid, "anon-CCC", "accept", "sub:42", "Impostor")
    except rt.ConsentError:
        pass
    else:
        raise AssertionError("an anonymous identity cannot claim a sub key")


async def test_signed_in_key_comes_from_identity_not_client(fakes):
    sid = make_session()
    await start(sid)
    await rt.set_consent(sid, "user-77", "accept", "guest:aaaaaaaaaaaa", "Dana")
    assert query(lambda db: db.query(MeetppConsent).filter_by(session_id=sid).one()).person_key == "sub:77"


async def test_echo_guard_drops_announcement_speech(fakes):
    sid = make_session()
    await start(sid)
    await join(sid, "42", "Alice Moreau")
    rt._last_announce[sid] = ("Moving on to item 4: Lawyers.", time.monotonic())
    res = await rt.ingest(sid, {"segments": [_seg("e1", "user-42", "moving on to item 4 lawyers", "Alice Moreau")]})
    assert res["seqs"] == {}
    res = await rt.ingest(sid, {"segments": [_seg("e2", "user-42", "Thank you, I have a question about the lawyers.", "Alice Moreau")]})
    assert res["seqs"] == {"e2": 1}


async def test_presence_roster_and_seeded_member_matching(fakes):
    sid = make_session(meeting_type="board")
    series_id = get(MeetppSession, sid).series_id
    add_roster(series_id, "name:robin hale", "Robin Hale")
    db = SessionLocal()
    try:
        rt.expected_attendees(db, db.get(MeetppSession, sid))
        db.commit()
    finally:
        db.close()
    a = query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid, person_key="name:robin hale").one())
    assert a.status == "not_registered" and a.voting
    await start(sid)
    await rt.presence(sid, [{"identity": "user-90", "name": "Robin Hale", "kind": "standard", "event": "connected"},
                            {"identity": "EG_123", "name": "egress", "kind": "egress", "event": "connected"},
                            {"identity": "user-91", "name": "Sam Lee", "kind": "standard", "event": "connected"}])
    rows = {x.person_key: x for x in query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid).all())}
    assert set(rows) == {"sub:90", "sub:91"}
    assert rows["sub:90"].id == a.id and rows["sub:90"].status == "present" and rows["sub:90"].online
    roster = {r.person_key: r for r in query(lambda db: db.query(MeetppRoster).filter_by(series_id=series_id).all())}
    # The series already has a voting member (seeded director), so the
    # newcomer starts non-voting until the chair toggles them (FDD §8.6).
    assert set(roster) == {"sub:90", "sub:91"} and roster["sub:90"].voting and not roster["sub:91"].voting
    state = fakes.bus.last("state")
    assert "attendees" in state["delta"] and state["delta"]["quorum"] == {"required": 1, "voting_present": 1, "voting_total": 1, "met": True}
    assert all(x["email"] is None for x in state["delta"]["attendees"])
    await rt.presence(sid, [{"identity": "user-91", "name": "Sam Lee", "kind": "standard", "event": "disconnected"}])
    assert not query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid, person_key="sub:91").one()).online


async def test_agent_status_and_reconcile(fakes):
    sid = make_session()
    await start(sid)
    await join(sid, "42", "Alice Moreau")
    runner = rt.SessionRunner(sid)
    await runner.reconcile_agent()
    started = fakes.agent.named("start")[-1]
    assert set(started) == {"session_id", "room", "ws_url", "token", "language", "glossary", "accepted_identities", "paused"}
    assert started["accepted_identities"] == ["user-42"] and started["language"] == "en" and started["paused"] is False
    await runner.reconcile_agent()
    assert fakes.agent.named("patch")[-1] == {"accepted_identities": ["user-42"], "paused": False}
    # Missing from /health → restarted with an agent_restart gap.
    fakes.agent.health_sessions = []
    await runner.reconcile_agent()
    assert len(fakes.agent.named("start")) == 2
    assert fakes.bus.last("gap")["reason"] == "agent_restart"
    await rt.agent_status(sid, {"status": "behind", "backlog_s": 12.5, "speakers": [{"identity": "user-42", "name": "Alice Moreau", "ok": False}], "tier2": "up"})
    msg = fakes.bus.last("agent")
    assert msg["status"] == "behind" and msg["speakers"] == [{"name": "Alice Moreau", "ok": False}] and msg["tier2"] == "up"
    from app.meetpp import ops

    db = SessionLocal()
    try:
        meta = ops.session_meta(db, db.get(MeetppSession, sid))
    finally:
        db.close()
    assert meta["agent"] == {"status": "behind", "backlog_s": 12.5, "speakers": [{"name": "Alice Moreau", "ok": False}], "tier2": "up"}


async def test_pause_resume(fakes):
    sid = make_session()
    await start(sid)
    meta = await rt.pause_session(sid, True)
    assert meta["status"] == "paused"
    assert fakes.agent.named("patch")[-1]["paused"] is True
    assert fakes.bus.last("session")["state"] == "paused"
    meta = await rt.pause_session(sid, False)
    assert meta["status"] == "running" and fakes.bus.last("session")["state"] == "resumed"


async def test_first_meeting_signed_in_participants_vote_by_default(fakes):
    sid = make_session(meeting_type="board")
    series_id = get(MeetppSession, sid).series_id
    await start(sid)
    await rt.presence(sid, [{"identity": "user-70", "name": "Ann Example", "kind": "standard", "event": "connected"},
                            {"identity": "anon-01ABC", "name": "Guest", "kind": "standard", "event": "connected"}])
    await rt.set_consent(sid, "anon-01ABC", "accept", "guest:abc12345", "Guest")
    roster = {r.person_key: r for r in query(lambda db: db.query(MeetppRoster).filter_by(series_id=series_id).all())}
    assert roster["sub:70"].voting
    assert not roster["guest:abc12345"].voting

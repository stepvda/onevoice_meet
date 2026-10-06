"""Review fixes through the REST API: public-page viewers, chair rights from
the meeting (not the token), internal HMAC v2 with replay protection,
publishing (transactions, the new room) and series rules."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

from app.config import settings
from app.db import SessionLocal, engine
from app.meetpp import governance, invites, runtime as rt, util
from app.meetpp.auth import internal_signature
from app.meetpp.models import MeetppDecision, MeetppSession, MeetppVote
from app.models import Meeting

from .conftest import add_roster, get, make_session, query
from .test_api import _internal, _jwt, _room

TARGET = "/api/v1/internal/meetpp/sessions/01HZZZZZZZZZZZZZZZZZZZZZZZ/segments?x=1"


def test_internal_signature_v2_test_vector(monkeypatch):
    monkeypatch.setattr(settings, "meetpp_internal_secret", "test-secret")
    assert internal_signature("1700000000", "POST", TARGET, b'{"a":1}') == (
        "572404eb590905ef095aea5d1c8bfafbcd49bf92e158be425640ccdd3a868582"
    )


def test_internal_requests_are_bound_to_path_and_never_replayed(api, fakes):
    sid = make_session()
    asyncio.run(rt.start_session(sid))
    path = f"/api/v1/internal/meetpp/sessions/{sid}/agent-status"
    raw, headers = _internal(path, {"status": "listening"})
    assert api.post(path, content=raw, headers=headers).status_code == 200
    # The same request again (a replay) is refused.
    assert api.post(path, content=raw, headers=headers).status_code == 401
    # Signed for another endpoint.
    raw, headers = _internal(path, {"status": "listening", "n": 1})
    assert api.post(f"/api/v1/internal/meetpp/sessions/{sid}/presence", content=raw, headers=headers).status_code == 401
    # The query string is signed too.
    raw, headers = _internal(path + "?x=1", {"status": "listening", "n": 2})
    assert api.post(path + "?x=2", content=raw, headers=headers).status_code == 401
    assert api.post(path + "?x=1", content=raw, headers=headers).status_code == 200
    # Old (v1) signatures are no longer accepted.
    raw = json.dumps({"status": "listening", "n": 3}).encode()
    ts = str(int(time.time()))
    import hashlib
    import hmac

    v1 = hmac.new(settings.meetpp_internal_secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    headers = {"X-Meetpp-Timestamp": ts, "X-Meetpp-Signature": v1, "Content-Type": "application/json"}
    assert api.post(path, content=raw, headers=headers).status_code == 401


@pytest.fixture(autouse=True)
def _uncapped(monkeypatch):
    # Other tests leave sessions running in the shared database.
    monkeypatch.setattr(settings, "meetpp_max_active_sessions", 1000)


def _running_session(api, meeting) -> str:
    chair = _jwt("42")
    sid = api.post(f"/api/v1/meetings/{meeting.id}/meetpp/sessions", json={"meeting_type": "board"}, headers=chair).json()["session"]["id"]
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/start", headers=chair).status_code == 200
    return sid


def test_public_page_viewers_see_nothing_unless_public_and_never_the_transcript(api, fakes, meeting):
    chair = _jwt("42")
    sid = _running_session(api, meeting)
    guest = _room(meeting.room_name, "anon-01ABC", "Guest Gina")
    key = "guest:abcdef123456"
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/consent", headers=guest, json={"decision": "accept", "person_key": key}).status_code == 200
    viewer = _room(meeting.room_name, "viewer-01XYZ", "Viewer")
    for url in (f"/api/v1/meetpp/sessions/{sid}/state", f"/api/v1/meetpp/sessions/{sid}/transcript"):
        assert api.get(url, headers=viewer).status_code == 404
    # Room participants see guests by an alias only.
    state = api.get(f"/api/v1/meetpp/sessions/{sid}/state", headers=guest).json()
    assert util.public_person_key(sid, key) in {a["person_key"] for a in state["attendees"]}
    assert key not in json.dumps(state)
    # Shown publicly: the board, without person keys or transcript.
    assert api.patch(f"/api/v1/meetpp/sessions/{sid}", headers=chair, json={"settings": {"show_public": True}}).status_code == 200
    r = api.get(f"/api/v1/meetpp/sessions/{sid}/state", headers=viewer)
    assert r.status_code == 200 and r.json()["attendees"]
    assert {a["person_key"] for a in r.json()["attendees"]} == {None}
    assert api.get(f"/api/v1/meetpp/sessions/{sid}/transcript", headers=viewer).status_code == 404
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/consent", headers=viewer, json={"decision": "accept", "person_key": key}).status_code == 403


def test_chair_rights_follow_the_meeting_not_the_token(api, fakes, meeting):
    sid = _running_session(api, meeting)
    # A room_admin token (e.g. minted while user 77 was a co-host) gives no
    # chair rights once 77 is not a co-host.
    former = _room(meeting.room_name, "user-77", "Former Co-host", owner=True)
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/position", headers=former, json={"action": "next"}).status_code == 404
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/ops", headers=former, json={"ops": []}).status_code == 404
    cohost = _room(meeting.room_name, "user-43", "Ben Hartley")  # a co-host without room_admin in the token
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/position", headers=cohost, json={"action": "next"}).status_code == 200


def test_consent_after_the_end_is_refused(api, fakes, meeting):
    chair = _jwt("42")
    sid = _running_session(api, meeting)
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/end", headers=chair).status_code == 200
    late = _room(meeting.room_name, "user-55", "Late Lou")
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/consent", headers=late, json={"decision": "accept"}).status_code == 409


def _writable() -> bool:
    """Whether another connection could write right now (no writer holds the
    database)."""
    con = sqlite3.connect(engine.url.database, timeout=0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def test_publish_holds_no_write_lock_and_invites_to_one_new_room(api, fakes, meeting, monkeypatch):
    from app.meetpp import report

    chair = _jwt("42")
    sid = make_session()
    _set_review(sid)
    s = get(MeetppSession, sid)
    room = query(lambda db: db.get(Meeting, s.meeting_id).room_name)
    checks, mails = [], []
    real_render = report.render_meeting_report

    def render(data):
        checks.append(("render", _writable()))
        return real_render(data)

    async def send_email(**kw):
        checks.append(("send", _writable()))
        mails.append(kw)
        return True

    monkeypatch.setattr(report, "render_meeting_report", render)
    monkeypatch.setattr(invites, "send_email", send_email)
    review = {
        "next_meeting": {"date_iso": "2026-11-01T17:00:00Z", "duration_min": 90, "room": "new"},
        "recipients": {"report": [{"name": "Ben", "email": "ben@example.org"}], "invite": [{"name": "Ben", "email": "ben@example.org"}]},
        "distribution": {"send_report": True, "send_invites": True},
    }
    assert api.put(f"/api/v1/meetpp/sessions/{sid}/review", headers=chair, json=review).status_code == 200
    first = api.post(f"/api/v1/meetpp/sessions/{sid}/publish", headers=chair)
    assert first.status_code == 200, first.text
    assert checks and all(ok for _what, ok in checks)
    new_id = first.json()["next_meeting_id"]
    new_room = query(lambda db: db.get(Meeting, new_id).room_name)
    assert new_room != room
    invite = next(m for m in mails if m["subject"].startswith("Invitation"))
    assert f"{settings.public_url}/{new_room}" in invite["html"] and f"/{room}'" not in invite["html"]
    exp = api.get(f"/api/v1/meetpp/sessions/{sid}/export.json", headers=chair).json()
    assert exp["next_meeting"]["location"] == f"{settings.public_url}/{new_room}"
    # Re-publishing (after another review edit) reuses that meeting.
    review["next_meeting"]["duration_min"] = 60
    assert api.put(f"/api/v1/meetpp/sessions/{sid}/review", headers=chair, json=review).status_code == 200
    again = api.post(f"/api/v1/meetpp/sessions/{sid}/publish", headers=chair)
    assert again.status_code == 200 and again.json()["next_meeting_id"] == new_id
    assert query(lambda db: db.query(Meeting).filter(Meeting.room_name.like(f"{room}-%")).count()) == 1
    assert query(lambda db: db.get(Meeting, new_id).duration_minutes) == 60


def _set_review(sid: str) -> None:
    db = SessionLocal()
    try:
        s = db.get(MeetppSession, sid)
        s.status, s.started_at, s.ended_at = "review", util.now(), util.now()
        db.commit()
    finally:
        db.close()


def test_a_rules_change_keeps_decision_and_vote_result_aligned(api, fakes):
    sid = make_session(meeting_type="board")
    series_id = get(MeetppSession, sid).series_id
    for key, name in (("sub:42", "Alice Moreau"), ("sub:43", "Ben Hartley"), ("sub:44", "Chloe Varga")):
        add_roster(series_id, key, name)
    _set_review(sid)
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        rt.expected_attendees(db, session)
        d = MeetppDecision(id=util.ulid(), session_id=sid, series_id=series_id, ref="D-1", title="Repaint the shed",
                           status="adopted", origin="user", evidence_json="[]", locked=True, confirmed=True)
        db.add(d)
        db.flush()
        vote = governance.apply_vote(db, session, d, {"method": "show_of_hands", "for": 2, "against": 1, "abstain": 0}, confirmed=True)
        assert vote.result == "adopted"
        db.commit()
        did = d.id
    finally:
        db.close()
    r = api.put(f"/api/v1/meetpp/series/{series_id}/rules", headers=_jwt("42"), json={"majority_rule": "unanimous"})
    assert r.status_code == 200
    d = get(MeetppDecision, did)
    v = query(lambda db: db.query(MeetppVote).filter_by(decision_id=did).one())
    assert (v.result, d.status) == ("rejected", "rejected")

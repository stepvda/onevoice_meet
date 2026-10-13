"""Meet++ prepared from One Voice Mail (app/meetpp/external.py) and the Meet →
Mail webhook (app/services/mail_webhook.py)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid

import pytest
from jose import jwt

from app.config import settings
from app.db import SessionLocal
from app.meetpp import util
from app.meetpp.models import (
    MeetppAction,
    MeetppAttendee,
    MeetppDecision,
    MeetppSeries,
    MeetppSession,
    MeetppVote,
)
from app.models import Meeting
from app.services import mail_webhook

from .conftest import get, query


@pytest.fixture
def chair(mail, monkeypatch):
    # Sessions other tests left running must not use up the AI capacity.
    monkeypatch.setattr(settings, "meetpp_max_active_sessions", 10_000)
    return mail.headers(str(uuid.uuid4()), name="Chair Person", email=f"chair-{uuid.uuid4().hex[:6]}@example.org")


@pytest.fixture
def mail_meeting(api, chair):
    r = api.post("/api/v1/meetings", headers=chair, json={
        "display_title": "Board meeting", "allow_anonymous": True, "integration": {"source": "mail", "ref": "meeting:s1"},
    })
    assert r.status_code == 201, r.text
    return r.json()["meeting"]


@pytest.fixture
def hooks(monkeypatch):
    """Webhook on, delivered synchronously, HTTP send recorded."""
    sent: list[dict] = []

    def _post(url, raw, headers):
        sent.append({"url": url, "raw": raw, "headers": headers, "body": json.loads(raw)})
        return 202

    monkeypatch.setattr(settings, "mail_webhook_url", "https://mail.example.test/api/meet/webhook")
    monkeypatch.setattr(settings, "mail_webhook_secret", "s3cret")
    monkeypatch.setattr(mail_webhook, "_start", lambda fn, *args: fn(*args))
    monkeypatch.setattr(mail_webhook, "_post", _post)
    return sent


def payload(**over) -> dict:
    body = {
        "occurrence_ref": "occ-1",
        "series_ref": "series-1",
        "title": "Board meeting",
        "starts_at": "2026-10-20T17:00:00Z",
        "duration_minutes": 120,
        "meeting_type": "board",
        "open_url": "https://mail.example.test/meetings/m/occ-1",
        "agenda": [
            {"ref": "ag-1", "title": "Approval of the minutes", "body": "Version 2"},
            {"ref": "ag-2", "title": "Rainwater tank", "body": "Quotes received"},
        ],
        "decisions": [
            {"ref": "dec-1", "agenda_ref": "ag-2", "title": "Buy the tank", "resolution": "that the board buys the tank", "status": "pending"},
            {"ref": "dec-2", "agenda_ref": None, "title": "Budget 2026", "resolution": "that the budget is approved", "status": "adopted"},
        ],
        "actions": [
            {"ref": "act-1", "title": "Collect three quotes", "description": "For the tank",
             "assignees": [{"name": "Robin Hale", "email": "robin@example.org"}], "due": "2026-10-30", "status": "open"},
            {"ref": "act-2", "title": "Old finished job", "description": "", "assignees": [], "due": None, "status": "done"},
        ],
        "roster": [
            {"name": "Robin Hale", "email": "robin@example.org", "voting": True},
            {"name": "Sam Lee", "email": "sam@example.org", "voting": False},
        ],
        "next_occurrence": {"ref": "occ-2", "starts_at": "2026-10-27T17:00:00Z"},
    }
    body.update(over)
    return body


def push(api, meeting, chair, **over):
    return api.put(f"/api/v1/meetings/{meeting['id']}/meetpp/external", headers=chair, json=payload(**over))


def state(api, sid, chair) -> dict:
    r = api.get(f"/api/v1/meetpp/sessions/{sid}/state", headers=chair)
    assert r.status_code == 200, r.text
    return r.json()


def agenda(st: dict) -> list[tuple[str | None, str]]:
    return [(s["number"], s["title"]) for s in st["sections"] if s["kind"] == "agenda" and not s["parent_id"]]


def set_status(sid: str, status: str) -> None:
    db = SessionLocal()
    try:
        db.get(MeetppSession, sid).status = status
        db.commit()
    finally:
        db.close()


# ─── PUT /meetings/{mid}/meetpp/external ────────────────────────────────────


def test_setup_prepares_the_sitting(api, fakes, mail_meeting, chair):
    r = push(api, mail_meeting, chair)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"session_id", "status", "series_id"} and out["status"] == "setup"
    sid = out["session_id"]

    series = get(MeetppSeries, out["series_id"])
    assert series.external_ref == "mail:series-1" and series.meeting_type == "board" and series.title == "Board meeting"
    assert get(Meeting, mail_meeting["id"]).meetpp_series_id == series.id
    session = get(MeetppSession, sid)
    assert session.external_ref == "occ-1" and session.template == "agenda" and session.mode == "lead"

    st = state(api, sid, chair)
    assert agenda(st) == [("1", "Approval of the minutes"), ("2", "Rainwater tank")]
    assert {s["source"] for s in st["sections"] if s["kind"] == "agenda"} == {"mail"}
    by_title = {d["title"]: d for d in st["decisions"]}
    tank = by_title["Buy the tank"]
    assert tank["status"] == "pending" and tank["origin"] == "mail"
    assert tank["section_id"] == next(s["id"] for s in st["sections"] if s["title"] == "Rainwater tank")
    assert by_title["Budget 2026"]["status"] == "adopted" and by_title["Budget 2026"]["section_id"] is None

    # Mail actions are previous actions of the series ("to review"); done ones are not imported.
    assert [a["title"] for a in st["actions"]] == ["Collect three quotes"]
    action = st["actions"][0]
    assert action["previous"] is True and action["origin"] == "mail" and action["status"] == "open"
    assert action["due"] == "2026-10-30"
    assert action["assignees"] == [{"name": "Robin Hale", "person_key": "name:robin hale"}]  # the roster member
    assert action["section_id"] == next(s["id"] for s in st["sections"] if s["kind"] == "previous_actions")
    assert next(s for s in st["sections"] if s["kind"] == "previous_actions")["status"] == "pending"

    # The roster is expected: voting as Mail says, e-mails kept for the chair.
    att = {a["name"]: a for a in st["attendees"]}
    assert att["Robin Hale"]["status"] == "not_registered" and att["Robin Hale"]["voting"] is True
    assert att["Sam Lee"]["voting"] is False and att["Sam Lee"]["email"] == "sam@example.org"

    ext = st["session"]["external"]
    assert ext == {
        "source": "mail", "occurrence_ref": "occ-1", "open_url": "https://mail.example.test/meetings/m/occ-1",
        "next_occurrence": {"ref": "occ-2", "starts_at": "2026-10-27T17:00:00Z"}, "next_agenda_sent": False,
    }
    review = api.get(f"/api/v1/meetpp/sessions/{sid}/review", headers=chair).json()
    assert review["external"] == ext and review["session"]["external"] == ext
    listing = api.get(f"/api/v1/meetings/{mail_meeting['id']}/meetpp/sessions", headers=chair).json()
    assert listing["sessions"][0]["external_ref"] == "occ-1"


def test_setup_push_replaces_and_upserts_by_ref(api, fakes, mail_meeting, chair):
    sid = push(api, mail_meeting, chair).json()["session_id"]
    before = state(api, sid, chair)
    ids = {s["title"]: s["id"] for s in before["sections"]}
    dec = {d["title"]: d["id"] for d in before["decisions"]}
    act = before["actions"][0]["id"]

    r = push(
        api, mail_meeting, chair,
        agenda=[
            {"ref": "ag-3", "title": "Garden", "body": None},
            {"ref": "ag-1", "title": "Approval of the minutes of 13 October", "body": "Version 3"},
        ],
        decisions=[{"ref": "dec-1", "agenda_ref": "ag-1", "title": "Buy the big tank", "resolution": "that …", "status": "pending"}],
        actions=[{"ref": "act-1", "title": "Collect four quotes", "description": "", "assignees": [], "due": None, "status": "in_progress"}],
        roster=[{"name": "Robin Hale", "email": "ROBIN@example.org", "voting": False}],
    )
    assert r.status_code == 200 and r.json()["session_id"] == sid
    st = state(api, sid, chair)
    assert agenda(st) == [("1", "Garden"), ("2", "Approval of the minutes of 13 October")]
    point = next(s for s in st["sections"] if s["title"].startswith("Approval"))
    assert point["id"] == ids["Approval of the minutes"] and point["body"] == "Version 3"
    assert ids["Rainwater tank"] not in {s["id"] for s in st["sections"]}
    # dec-1 kept its record (same id) and moved; dec-2 is no longer listed in Mail.
    assert [(d["id"], d["title"], d["section_id"]) for d in st["decisions"]] == [(dec["Buy the tank"], "Buy the big tank", point["id"])]
    assert st["actions"][0]["id"] == act and st["actions"][0]["title"] == "Collect four quotes"
    assert st["actions"][0]["status"] == "in_progress"
    robin = next(a for a in st["attendees"] if a["name"] == "Robin Hale")
    assert robin["voting"] is False  # upserted by e-mail, not duplicated
    assert sum(1 for a in st["attendees"] if a["name"] == "Robin Hale") == 1

    # The open Mail action Mail no longer lists leaves the sitting.
    push(api, mail_meeting, chair, actions=[])
    st = state(api, sid, chair)
    assert st["actions"] == [] and get(MeetppAction, act) is None


def test_setup_keeps_what_the_chair_edited_in_meet(api, fakes, mail_meeting, chair):
    sid = push(api, mail_meeting, chair).json()["session_id"]
    st = state(api, sid, chair)
    point = next(s for s in st["sections"] if s["title"] == "Rainwater tank")
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/ops", headers=chair, json={"ops": [
        {"op": "section.update", "id": point["id"], "title": "Rainwater tank (chair's wording)"},
    ]})
    assert r.json()["applied"] == 1
    push(api, mail_meeting, chair, agenda=[{"ref": "ag-2", "title": "Tank from Mail", "body": "x"}])
    st = state(api, sid, chair)
    assert agenda(st) == [("1", "Rainwater tank (chair's wording)")]


def test_running_session_is_only_added_to(api, fakes, mail_meeting, chair):
    sid = push(api, mail_meeting, chair).json()["session_id"]
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/start", headers=chair)
    assert r.status_code == 200, r.text
    st = state(api, sid, chair)
    tank = next(d for d in st["decisions"] if d["title"] == "Buy the tank")
    budget = next(d for d in st["decisions"] if d["title"] == "Budget 2026")
    # The meeting adopts the tank with its own wording.
    api.post(f"/api/v1/meetpp/sessions/{sid}/ops", headers=chair, json={"ops": [
        {"op": "decision.update", "id": tank["id"], "status": "adopted", "resolution": "that the board buys the 5,000 l tank"},
    ]})

    r = push(
        api, mail_meeting, chair,
        agenda=[
            {"ref": "ag-1", "title": "Approval of the minutes (v3)", "body": "Version 3"},
            {"ref": "ag-4", "title": "Any other business", "body": None},
        ],
        decisions=[
            {"ref": "dec-1", "agenda_ref": "ag-2", "title": "Buy a tank", "resolution": "that nothing", "status": "pending"},
            {"ref": "dec-3", "agenda_ref": "ag-4", "title": "Next date", "resolution": None, "status": "pending"},
        ],
        actions=[{"ref": "act-3", "title": "Book the hall", "description": "", "assignees": [], "due": None, "status": "open"}],
        roster=[{"name": "New Member", "email": "new@example.org", "voting": True}],
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    st = state(api, sid, chair)
    titles = [t for _n, t in agenda(st)]
    # Nothing removed; unlocked titles follow Mail; new points are added.
    assert titles == ["Approval of the minutes (v3)", "Rainwater tank", "Any other business"]
    decisions = {d["id"]: d for d in st["decisions"]}
    assert decisions[tank["id"]]["resolution"] == "that the board buys the 5,000 l tank"  # taken: left alone
    assert decisions[tank["id"]]["status"] == "adopted"
    assert budget["id"] in decisions  # not listed any more, still there
    assert any(d["title"] == "Next date" and d["status"] == "pending" for d in st["decisions"])
    assert {a["title"] for a in st["actions"]} == {"Collect three quotes", "Book the hall"}
    assert any(a["name"] == "New Member" for a in st["attendees"])


def test_closed_sitting_is_refused(api, fakes, mail_meeting, chair):
    sid = push(api, mail_meeting, chair).json()["session_id"]
    api.post(f"/api/v1/meetpp/sessions/{sid}/start", headers=chair)
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/end", headers=chair).json() == {"status": "finalising"}
    for status in ("finalising", "review", "published", "aborted"):
        set_status(sid, status)
        r = push(api, mail_meeting, chair)
        assert r.status_code == 409 and r.json()["detail"] == "meetpp_closed", status


def test_another_live_sitting_is_busy(api, fakes, mail_meeting, chair):
    sid = push(api, mail_meeting, chair).json()["session_id"]
    api.post(f"/api/v1/meetpp/sessions/{sid}/start", headers=chair)
    r = push(api, mail_meeting, chair, occurrence_ref="occ-2")
    assert r.status_code == 409 and r.json()["detail"] == "meetpp_busy"
    set_status(sid, "review")
    r = push(api, mail_meeting, chair, occurrence_ref="occ-2")
    assert r.status_code == 200 and r.json()["session_id"] != sid


def test_other_sittings_setup_session_is_replaced(api, fakes, mail_meeting, chair):
    first = push(api, mail_meeting, chair).json()
    second = push(api, mail_meeting, chair, occurrence_ref="occ-2").json()
    assert second["session_id"] != first["session_id"] and second["series_id"] == first["series_id"]
    assert get(MeetppSession, first["session_id"]) is None
    listing = api.get(f"/api/v1/meetings/{mail_meeting['id']}/meetpp/sessions", headers=chair).json()["sessions"]
    assert [(s["id"], s["external_ref"]) for s in listing] == [(second["session_id"], "occ-2")]
    # The Mail action was imported again for the new sitting (same series).
    st = state(api, second["session_id"], chair)
    assert [a["title"] for a in st["actions"]] == ["Collect three quotes"]


def test_previous_sittings_actions_carry_over_by_ref(api, fakes, mail_meeting, chair):
    first = push(api, mail_meeting, chair).json()["session_id"]
    api.post(f"/api/v1/meetpp/sessions/{first}/start", headers=chair)
    set_status(first, "published")
    series_id = get(MeetppSession, first).series_id
    act = query(lambda db: db.query(MeetppAction).filter_by(series_id=series_id, external_ref="act-1").one().id)
    second = push(api, mail_meeting, chair, occurrence_ref="occ-2").json()["session_id"]
    st = state(api, second, chair)
    assert [a["id"] for a in st["actions"]] == [act]  # the same series action, not a copy
    # Mail stops listing it: it leaves this sitting but keeps its record.
    push(api, mail_meeting, chair, occurrence_ref="occ-2", actions=[])
    assert state(api, second, chair)["actions"] == []
    assert get(MeetppAction, act).status == "open"
    # …and comes back when Mail lists it again.
    push(api, mail_meeting, chair, occurrence_ref="occ-2")
    assert [a["id"] for a in state(api, second, chair)["actions"]] == [act]


def test_access_rules(api, fakes, mail_meeting, chair, mail, monkeypatch):
    stranger = mail.headers(str(uuid.uuid4()))
    assert push(api, mail_meeting, stranger).status_code == 404
    sid = push(api, mail_meeting, chair).json()["session_id"]
    assert api.get(f"/api/v1/meetpp/sessions/{sid}/external", headers=stranger).status_code == 404
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/external/next-agenda", headers=stranger, json={"agenda": [{"title": "x"}]}).status_code == 404
    monkeypatch.setattr(settings, "meetpp_enabled", False)
    assert push(api, mail_meeting, chair).status_code == 404


def test_unsafe_open_url_is_dropped(api, fakes, mail_meeting, chair):
    sid = push(api, mail_meeting, chair, open_url="javascript:alert(1)").json()["session_id"]
    assert state(api, sid, chair)["session"]["external"]["open_url"] is None


# ─── GET /meetpp/sessions/{sid}/external ────────────────────────────────────


def test_result_in_mails_terms(api, fakes, mail_meeting, chair):
    sid = push(api, mail_meeting, chair).json()["session_id"]
    early = api.get(f"/api/v1/meetpp/sessions/{sid}/external", headers=chair).json()
    assert early["minutes_markdown"] == "" and early["session"]["status"] == "setup"
    assert [d["ref"] for d in early["decisions"]] == ["dec-2"]  # pending decisions are left out

    api.post(f"/api/v1/meetpp/sessions/{sid}/start", headers=chair)
    st = state(api, sid, chair)
    tank = next(d for d in st["decisions"] if d["title"] == "Buy the tank")
    quotes = st["actions"][0]
    point = next(s for s in st["sections"] if s["title"] == "Rainwater tank")
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/ops", headers=chair, json={"ops": [
        {"op": "action.update", "id": quotes["id"], "status": "done", "report_note": "Three quotes in.", "completion_note": "Filed."},
        {"op": "decision.add", "section_id": point["id"], "title": "Hire a plumber", "resolution": "that a plumber is hired"},
    ]})
    assert r.json()["applied"] == 2, r.json()
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        d = db.get(MeetppDecision, tank["id"])
        d.status, d.decided_at, d.how_taken = "adopted", util.now(), "show of hands"
        db.add(MeetppVote(id=util.ulid(), decision_id=d.id, method="show_of_hands", tally_for=4, tally_against=1, tally_abstain=0, result="adopted"))
        # An AI action nobody confirmed yet, assigned to a roster member.
        db.add(MeetppAction(id=util.ulid(), series_id=session.series_id, session_id=sid, ref="A-99", section_id=point["id"],
                            title="Order the tank", assignees_json=util.dumps([{"name": "Robin Hale", "person_key": "name:robin hale"}]),
                            status="proposed", origin="ai", decision_id=d.id, evidence_json="[]"))
        session.status = "review"
        session.final_json = util.dumps({"next_agenda": [{"title": "Tank installation", "body": "Planning"}]})
        db.commit()
    finally:
        db.close()

    res = api.get(f"/api/v1/meetpp/sessions/{sid}/external", headers=chair).json()
    assert set(res) == {"session", "minutes_markdown", "decisions", "actions", "attendance", "next_agenda", "next_agenda_sent"}
    assert set(res["session"]) == {"id", "status", "occurrence_ref", "started_at", "ended_at", "published_at"}
    assert res["session"]["occurrence_ref"] == "occ-1" and res["session"]["status"] == "review"
    assert res["minutes_markdown"].startswith("# Minutes — Board meeting")

    decisions = {d["title"]: d for d in res["decisions"]}
    for d in res["decisions"]:
        assert set(d) == {"id", "ref", "agenda_ref", "title", "resolution", "how_taken", "status", "decided_at", "vote"}
    assert decisions["Buy the tank"]["ref"] == "dec-1" and decisions["Buy the tank"]["agenda_ref"] == "ag-2"
    assert decisions["Buy the tank"]["vote"] == {"for": 4, "against": 1, "abstain": 0, "result": "adopted"}
    assert decisions["Buy the tank"]["how_taken"] == "show of hands" and decisions["Buy the tank"]["decided_at"]
    assert decisions["Hire a plumber"]["ref"] is None and decisions["Hire a plumber"]["agenda_ref"] == "ag-2"

    actions = {a["title"]: a for a in res["actions"]}
    for a in res["actions"]:
        assert set(a) == {"id", "ref", "agenda_ref", "title", "description", "assignees", "due", "status",
                          "completion_note", "report_note", "decision_id"}
    q = actions["Collect three quotes"]
    assert q["ref"] == "act-1" and q["status"] == "done" and q["report_note"] == "Three quotes in."
    assert q["completion_note"] == "Filed." and q["assignees"] == [{"name": "Robin Hale", "email": "robin@example.org"}]
    order = actions["Order the tank"]
    assert order["ref"] is None and order["status"] == "open" and order["agenda_ref"] == "ag-2"
    assert order["decision_id"] == tank["id"]
    # The assignee's e-mail comes from the roster.
    assert order["assignees"] == [{"name": "Robin Hale", "email": "robin@example.org"}]

    attendance = {a["name"]: a for a in res["attendance"]}
    assert attendance["Robin Hale"] == {"name": "Robin Hale", "email": "robin@example.org", "status": "absent"}
    assert res["next_agenda"] == [{"title": "Tank installation", "body": "Planning"}]
    assert res["next_agenda_sent"] is False


# ─── POST /meetpp/sessions/{sid}/external/next-agenda ───────────────────────


def test_next_agenda_is_sent_to_mail(api, fakes, mail_meeting, chair, hooks):
    sid = push(api, mail_meeting, chair).json()["session_id"]
    hooks.clear()
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/external/next-agenda", headers=chair, json={"agenda": [
        {"title": "Tank installation", "body": "Planning"}, {"title": "  "}, {"title": "AOB"},
    ]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["external"]["next_agenda_sent"] is True
    assert out["next_agenda"] == [{"title": "Tank installation", "body": "Planning"}, {"title": "AOB", "body": None}]
    res = api.get(f"/api/v1/meetpp/sessions/{sid}/external", headers=chair).json()
    assert res["next_agenda_sent"] is True and [x["title"] for x in res["next_agenda"]] == ["Tank installation", "AOB"]
    review = api.get(f"/api/v1/meetpp/sessions/{sid}/review", headers=chair).json()
    assert [x["title"] for x in review["review"]["next_agenda"]] == ["Tank installation", "AOB"]
    assert [h["body"]["event"] for h in hooks] == ["meetpp.next_agenda"]
    body = hooks[0]["body"]
    assert body["session_id"] == sid and body["meeting_id"] == mail_meeting["id"] and body["occurrence_ref"] == "occ-1"
    assert body["status"] == "setup"
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/external/next-agenda", headers=chair, json={"agenda": []}).status_code == 400


def test_next_agenda_needs_a_prepared_session(api, fakes, chair):
    m = api.post("/api/v1/meetings", headers=chair, json={"display_title": "Plain"}).json()["meeting"]
    sid = api.post(f"/api/v1/meetings/{m['id']}/meetpp/sessions", headers=chair, json={}).json()["session"]["id"]
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/external/next-agenda", headers=chair, json={"agenda": [{"title": "x"}]})
    assert r.status_code == 409
    assert api.get(f"/api/v1/meetpp/sessions/{sid}/state", headers=chair).json()["session"]["external"] is None


# ─── Webhook ────────────────────────────────────────────────────────────────


def _verify(hook: dict, secret: str = "s3cret") -> bool:
    ts = hook["headers"]["X-Meet-Timestamp"]
    expected = "v1=" + hmac.new(secret.encode(), f"{ts}.{hook['raw']}".encode(), hashlib.sha256).hexdigest()
    return hook["headers"]["X-Meet-Signature"] == expected


def test_signature_matches_mails_check():
    # Mail: v1=<hex HMAC-SHA256(secret, `${timestamp}.${rawBody}`)>
    raw = '{"event":"meetpp.status"}'
    expected = hmac.new(b"secret", b'1700000000.{"event":"meetpp.status"}', hashlib.sha256).hexdigest()
    assert mail_webhook.sign("secret", 1700000000, raw) == f"v1={expected}"


def test_status_changes_are_sent(api, fakes, mail_meeting, chair, hooks):
    sid = push(api, mail_meeting, chair).json()["session_id"]
    assert hooks == []  # setup is not announced
    api.post(f"/api/v1/meetpp/sessions/{sid}/start", headers=chair)
    api.post(f"/api/v1/meetpp/sessions/{sid}/pause", headers=chair)
    api.post(f"/api/v1/meetpp/sessions/{sid}/resume", headers=chair)
    api.post(f"/api/v1/meetpp/sessions/{sid}/end", headers=chair)
    set_status(sid, "review")  # what the finalisation jobs end with
    events = [(h["body"]["event"], h["body"]["status"]) for h in hooks]
    # paused and finalising are internal to Meet; resuming is running again.
    assert events == [("meetpp.status", "running"), ("meetpp.status", "running"), ("meetpp.status", "review")]
    for h in hooks:
        assert h["url"] == "https://mail.example.test/api/meet/webhook" and _verify(h)
        assert h["headers"]["Content-Type"] == "application/json"
        assert set(h["body"]) == {"event", "meeting_id", "session_id", "occurrence_ref", "status", "at"}
        assert h["body"]["meeting_id"] == mail_meeting["id"] and h["body"]["session_id"] == sid
        assert h["body"]["occurrence_ref"] == "occ-1"


def test_publish_and_abort_are_sent(api, fakes, mail_meeting, chair, hooks):
    sid = push(api, mail_meeting, chair).json()["session_id"]
    set_status(sid, "published")
    other = push(api, mail_meeting, chair, occurrence_ref="occ-2").json()["session_id"]
    admin = jwt.encode({"sub": "1", "type": "access"}, os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    r = api.post(f"/api/v1/admin/meetpp/sessions/{other}/end", headers={"Authorization": f"Bearer {admin}"})
    assert r.status_code == 200, r.text
    assert [(h["body"]["session_id"], h["body"]["status"]) for h in hooks] == [(sid, "published"), (other, "aborted")]


def test_meetings_not_created_by_mail_send_nothing(api, fakes, chair, hooks):
    m = api.post("/api/v1/meetings", headers=chair, json={"display_title": "Plain"}).json()["meeting"]
    sid = api.post(f"/api/v1/meetings/{m['id']}/meetpp/sessions", headers=chair, json={}).json()["session"]["id"]
    api.post(f"/api/v1/meetpp/sessions/{sid}/start", headers=chair)
    assert hooks == []


def test_rolled_back_change_sends_nothing(api, fakes, mail_meeting, chair, hooks):
    sid = push(api, mail_meeting, chair).json()["session_id"]
    db = SessionLocal()
    try:
        db.get(MeetppSession, sid).status = "running"
        db.flush()
        db.rollback()
        db.commit()
    finally:
        db.close()
    assert hooks == []


def test_webhook_off_without_configuration(api, fakes, mail_meeting, chair, monkeypatch):
    started: list = []
    monkeypatch.setattr(mail_webhook, "_start", lambda fn, *args: started.append(args))
    sid = push(api, mail_meeting, chair).json()["session_id"]
    api.post(f"/api/v1/meetpp/sessions/{sid}/start", headers=chair)
    assert started == []


def test_delivery_retries_then_gives_up(monkeypatch):
    monkeypatch.setattr(settings, "mail_webhook_url", "https://mail.example.test/api/meet/webhook")
    monkeypatch.setattr(settings, "mail_webhook_secret", "s3cret")
    slept: list[float] = []
    monkeypatch.setattr(mail_webhook, "_sleep", slept.append)
    answers = iter([RuntimeError("connection refused"), 502, 204])

    def _post(url, raw, headers):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(mail_webhook, "_post", _post)
    event = mail_webhook.build_event("meetpp.status", meeting_id="m", session_id="s", occurrence_ref=None, status="review")
    assert mail_webhook.deliver(event) is True
    assert slept == [30, 300]

    slept.clear()
    monkeypatch.setattr(mail_webhook, "_post", lambda *a: 500)
    assert mail_webhook.deliver(event) is False
    assert slept == [30, 300]


def test_attendee_row_for_roster_member_matches_on_join_by_email(api, fakes, mail_meeting, chair, mail):
    """A Mail user joining the room takes over their roster row (matched by e-mail)."""
    from app.meetpp import runtime as rt

    sid = push(api, mail_meeting, chair).json()["session_id"]
    sub = str(uuid.uuid4())
    api.get("/api/v1/me", headers=mail.headers(sub, email="robin@example.org", name="Robin H."))
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        a = rt.ensure_attendee(db, session, key=f"sub:mail:{sub}", identity=f"user-mail:{sub}", name="Robin H.")
        db.commit()
        rows = db.query(MeetppAttendee).filter_by(session_id=sid).all()
        assert a.email == "robin@example.org" and a.voting is True
        assert not any(r.person_key.startswith("name:robin") for r in rows)
    finally:
        db.close()


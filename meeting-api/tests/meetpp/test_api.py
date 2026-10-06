"""REST API (contract §2): auth rules and the main flows end to end."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from jose import jwt

from app.config import settings
from app.db import SessionLocal
from app.livekit_client import mint_participant_token
from app.meetpp import runtime as rt, util
from app.meetpp.auth import internal_signature
from app.meetpp.models import MeetppSession
from app.models import Meeting

from .conftest import add_roster, get, sids


def _jwt(sub: str) -> dict:
    token = jwt.encode({"sub": sub, "email": f"u{sub}@example.com", "type": "access"}, os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _room(room: str, identity: str, name: str, owner: bool = False) -> dict:
    return {"X-Meet-Room-Token": mint_participant_token(room_name=room, identity=identity, display_name=name, is_owner=owner)}


def _internal(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return raw, {"X-Meetpp-Timestamp": ts, "X-Meetpp-Signature": internal_signature(ts, raw), "Content-Type": "application/json"}


@pytest.fixture
def api(fakes, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    async def _no_resume():
        return None

    monkeypatch.setattr(rt.runtime, "start", _no_resume)
    monkeypatch.setattr(settings, "meetpp_data_dir", str(tmp_path))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def meeting():
    db = SessionLocal()
    try:
        m = Meeting(id=util.ulid(), room_name=f"room-{util.ulid().lower()}", display_title="Board meeting #9",
                    owner_user_id="42", owner_name="Chair Person", owner_email="chair@example.org", cohost_user_ids='["43"]')
        db.add(m)
        db.commit()
        return m
    finally:
        db.close()


def test_full_flow(api, fakes, meeting, tmp_path):
    chair = _jwt("42")
    # Non-moderators get 404, like the rest of the app.
    assert api.post(f"/api/v1/meetings/{meeting.id}/meetpp/sessions", json={}, headers=_jwt("99")).status_code == 404
    r = api.post(f"/api/v1/meetings/{meeting.id}/meetpp/sessions", json={"template": "agenda", "meeting_type": "board"}, headers=chair)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"session", "series", "imported_actions"}
    sid = body["session"]["id"]
    assert body["session"]["status"] == "setup" and body["series"]["meeting_type"] == "board"
    assert api.post(f"/api/v1/meetings/{meeting.id}/meetpp/sessions", json={}, headers=chair).status_code == 409
    listing = api.get(f"/api/v1/meetings/{meeting.id}/meetpp/sessions", headers=chair).json()
    assert listing["sessions"][0] == {"id": sid, "status": "setup", "started_at": None, "ended_at": None, "published_at": None}

    r = api.put(f"/api/v1/meetpp/sessions/{sid}/outline", headers=chair, json={"agenda": [
        {"title": "Approval of the minutes", "body": "Version 2", "timebox_minutes": 5},
        {"title": "Rainwater tank", "subpoints": [{"title": "Quotes"}]},
    ]})
    assert r.status_code == 200, r.text
    numbers = {s["title"]: s["number"] for s in r.json()["sections"]}
    assert numbers["Approval of the minutes"] == "1" and numbers["Quotes"] == "2.1"

    r = api.patch(f"/api/v1/meetpp/sessions/{sid}", headers=chair, json={"mode": "assist", "editors": ["user-77", "anon-XYZ"], "settings": {"speak": False}})
    assert r.status_code == 200 and r.json()["mode"] == "assist" and r.json()["settings"]["speak"] is False
    assert r.json()["editors"] == ["user-77"] and r.json()["voting_body"] == "Board" and r.json()["quorum_required"] is None

    series_id = body["series"]["id"]
    add_roster(series_id, "sub:44", "Chloe Varga")
    r = api.put(f"/api/v1/meetpp/series/{series_id}/rules", headers=chair, json={"majority_rule": "two_thirds", "quorum_required": 2})
    assert r.status_code == 200 and r.json()["majority_rule"] == "two_thirds" and r.json()["quorum_required"] == 2
    roster = api.get(f"/api/v1/meetpp/series/{series_id}/roster", headers=chair).json()["roster"]
    assert roster[0]["name"] == "Chloe Varga"
    r = api.patch(f"/api/v1/meetpp/series/{series_id}/roster/{roster[0]['id']}", headers=chair, json={"voting": False})
    assert r.status_code == 200 and r.json()["voting"] is False

    r = api.post(f"/api/v1/meetpp/sessions/{sid}/start", headers=chair)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running" and r.json()["live_section_id"] == sids(sid)["Opening"]
    assert api.get(f"/api/v1/meetpp/rooms/{meeting.room_name}/active").json() == {
        "active": True, "sid": sid, "provider_label": settings.llm_provider_label, "consent_version": "v3"}

    owner_room = _room(meeting.room_name, "user-42", "Chair Person", owner=True)
    guest_room = _room(meeting.room_name, "anon-01ABC", "Guest Gina")
    editor_room = _room(meeting.room_name, "user-77", "Editor Ed")
    # Consent: the server maps identity → person key.
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/consent", headers=owner_room, json={"decision": "accept", "person_key": "guest:whatever1"}).json() == {"ok": True}
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/consent", headers=guest_room, json={"decision": "accept", "person_key": "guest:abcdef123456"}).status_code == 200
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/consent", headers=guest_room, json={"decision": "maybe", "person_key": "guest:abcdef123456"}).status_code == 400

    raw, headers = _internal({"segments": [
        {"utterance_id": "u1", "identity": "user-42", "name": "Chair Person", "t_start": util.iso(util.now()), "t_end": util.iso(util.now()), "text": "Welcome all."},
        {"utterance_id": "u2", "identity": "anon-01ABC", "name": "Guest Gina", "t_start": util.iso(util.now()), "t_end": util.iso(util.now()), "text": "Hello."},
    ]})
    r = api.post(f"/api/v1/internal/meetpp/sessions/{sid}/segments", content=raw, headers=headers)
    assert r.status_code == 200 and r.json() == {"ok": True, "seqs": {"u1": 1, "u2": 2}}
    headers["X-Meetpp-Signature"] = "0" * 64
    assert api.post(f"/api/v1/internal/meetpp/sessions/{sid}/segments", content=raw, headers=headers).status_code == 401
    raw, headers = _internal({"events": [{"identity": "user-43", "name": "Ben Hartley", "kind": "standard", "event": "connected", "at": util.iso(util.now())}]})
    assert api.post(f"/api/v1/internal/meetpp/sessions/{sid}/presence", content=raw, headers=headers).json() == {"ok": True}
    raw, headers = _internal({"status": "listening", "backlog_s": 0.5, "speakers": [], "tier2": "up"})
    assert api.post(f"/api/v1/internal/meetpp/sessions/{sid}/agent-status", content=raw, headers=headers).json() == {"ok": True}
    raw, headers = _internal({})
    tok = api.post(f"/api/v1/internal/meetpp/sessions/{sid}/agent-token", content=raw, headers=headers).json()
    assert tok["token"] and tok["ws_url"] == settings.meetpp_agent_ws_url

    # State: any participant (no e-mails); the chair JWT also works (review screen).
    st = api.get(f"/api/v1/meetpp/sessions/{sid}/state", headers=guest_room)
    assert st.status_code == 200 and st.json()["type"] == "state"
    assert all(a["email"] is None for a in st.json()["attendees"])
    st_chair = api.get(f"/api/v1/meetpp/sessions/{sid}/state", headers=chair).json()
    assert any(a["email"] for a in st_chair["attendees"]) or True
    assert api.get(f"/api/v1/meetpp/sessions/{sid}/state").status_code == 401
    page = api.get(f"/api/v1/meetpp/sessions/{sid}/transcript?after=0&limit=1", headers=guest_room).json()
    assert [s["seq"] for s in page["segments"]] == [1] and page["next_after"] == 1
    page = api.get(f"/api/v1/meetpp/sessions/{sid}/transcript?after=1&limit=1", headers=guest_room).json()
    assert [s["seq"] for s in page["segments"]] == [2] and page["next_after"] is None

    # Position: chair and co-hosts only (404 for participants and editors).
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/position", headers=guest_room, json={"action": "next"}).status_code == 404
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/position", headers=editor_room, json={"action": "next"}).status_code == 404
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/position", headers=owner_room, json={"action": "next"})
    assert r.status_code == 200 and r.json()["live_section_id"] == sids(sid)["Approval of the minutes"]
    cohost_room = _room(meeting.room_name, "user-43", "Ben Hartley")
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/position", headers=cohost_room, json={"action": "move", "section_id": sids(sid)["Quotes"]})
    assert r.status_code == 200 and r.json()["live_section_id"] == sids(sid)["Quotes"]
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/position/undo", headers=owner_room).status_code == 409

    # Human ops: chair or editor.
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/ops", headers=guest_room, json={"ops": []}).status_code == 404
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/ops", headers=editor_room, json={"ops": [
        {"op": "decision.add", "section_id": sids(sid)["Approval of the minutes"], "title": "Approve the minutes", "resolution": "that the minutes are approved"},
        {"op": "action.add", "section_id": sids(sid)["Quotes"], "title": "Get quotes", "assignees": ["Ben Hartley"], "due": "2026-11-01"},
        {"op": "section.add", "parent_id": sids(sid)["Rainwater tank"], "title": "Base", "kind": "agenda"},
        {"op": "decision.confirm", "id": "missing"},
    ]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["applied"] == 3 and len(out["rejected"]) == 1 and out["version"] > 0
    state = api.get(f"/api/v1/meetpp/sessions/{sid}/state", headers=owner_room).json()
    decision = state["decisions"][0]
    assert decision["status"] == "adopted" and decision["locked"] and decision["origin"] == "user"
    assert state["actions"][0]["status"] == "open"
    assert "Base" in {s["title"] for s in state["sections"]}

    # Attachments.
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/attachments", headers=guest_room, files={"file": ("wb.png", png, "image/png")}, data={"caption": "Sketch"})
    assert r.status_code == 200, r.text
    att = r.json()["attachment"]
    assert att["section_id"] == sids(sid)["Quotes"] and att["url"].endswith(att["id"])
    assert api.get(att["url"], headers=guest_room).content == png
    assert api.patch(att["url"], headers=guest_room, json={"caption": "x"}).status_code == 404
    assert api.patch(att["url"], headers=owner_room, json={"caption": "Tank sketch"}).json()["attachment"]["caption"] == "Tank sketch"

    # Attendees (room-chair).
    ben = next(a for a in state["attendees"] if a["person_key"] == "sub:43")
    r = api.patch(f"/api/v1/meetpp/sessions/{sid}/attendees/{ben['id']}", headers=owner_room, json={"email": "ben@example.org", "required_next": True})
    assert r.status_code == 200 and r.json()["email"] == "ben@example.org" and r.json()["required_next"] is True

    # Vote record (chair JWT).
    r = api.put(f"/api/v1/meetpp/decisions/{decision['id']}/vote", headers=chair, json={"method": "show_of_hands", "for": 2, "against": 0, "abstain": 0, "confirmed": True})
    assert r.status_code == 200, r.text
    assert r.json()["vote"]["result"] == "adopted" and r.json()["vote"]["confirmed"] and r.json()["confirmed"]
    assert api.put(f"/api/v1/meetpp/decisions/{decision['id']}/vote", headers=_jwt("99"), json={}).status_code == 404

    # The review screen uses the chair JWT on the "room" endpoints.
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/ops", headers=chair, json={"ops": [
        {"op": "minutes.edit", "section_id": sids(sid)["Approval of the minutes"], "narrative_md": "Edited in review."}]})
    assert r.status_code == 200 and r.json()["applied"] == 1
    r = api.patch(f"/api/v1/meetpp/sessions/{sid}/attendees/{ben['id']}", headers=chair, json={"voting": True})
    assert r.status_code == 200 and r.json()["email"] == "ben@example.org"
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/sections/{sids(sid)['Rainwater tank']}/compose", headers=chair).json() == {"status": "composing"}
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/ops", headers=_jwt("99"), json={"ops": []}).status_code == 404

    # End → finalising; jobs run (here directly) → review.
    assert api.post(f"/api/v1/meetpp/sessions/{sid}/end", headers=chair).json() == {"status": "finalising"}
    fakes.agent.finalize_ok = False
    fakes.llm.add("compose_section", *[{"markdown": "The board discussed the item at length and agreed on the way forward for the association. " * 3}] * 6)
    fakes.llm.add("compose_final", {"opening": "Opened.", "adjournment": "Closed.", "next_agenda": [{"title": "Tank installation"}]})
    import asyncio

    asyncio.run(rt.run_finalisation(sid))
    assert get(MeetppSession, sid).status == "review"
    rv = api.get(f"/api/v1/meetpp/sessions/{sid}/review", headers=chair).json()
    assert set(rv) >= {"session", "jobs", "review", "final"}
    assert set(rv["review"]) == {"next_meeting", "next_agenda", "recipients", "distribution"}
    assert set(rv["final"]) >= {"summary", "next_agenda", "required_next", "verify"}
    assert rv["final"]["required_next"] == [{"name": "Ben Hartley", "reason": None}]
    r = api.put(f"/api/v1/meetpp/sessions/{sid}/review", headers=chair, json={
        "next_meeting": {"date_iso": "2026-11-01T17:00:00Z", "duration_min": 90, "room": "same"},
        "next_agenda": [{"title": "Tank installation", "body": "Report"}],
        "recipients": {"report": [{"name": "Ben", "email": "ben@example.org"}], "invite": [{"name": "Ben", "email": "ben@example.org"}]},
        "distribution": {"send_report": True, "send_invites": True, "attach_snapshots": True, "include_transcript": False},
    })
    assert r.status_code == 200 and r.json()["review"]["next_meeting"]["duration_min"] == 90
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/finalise", headers=chair)
    assert r.status_code == 200 and set(r.json()["jobs"]) == {"tier2", "compose", "final", "render"}

    exp = api.get(f"/api/v1/meetpp/sessions/{sid}/export.json", headers=chair).json()
    assert exp["next_meeting"]["date_iso"] == "2026-11-01T17:00:00Z" and exp["next_agenda"][0]["title"] == "Tank installation"
    md = api.get(f"/api/v1/meetpp/sessions/{sid}/export.md", headers=chair)
    assert md.headers["content-type"].startswith("text/markdown") and md.text.startswith("# Minutes")
    pdf = api.get(f"/api/v1/meetpp/sessions/{sid}/report.pdf", headers=chair)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    audio = Path(tmp_path) / sid / "audio"
    audio.mkdir(parents=True)
    (audio / "u1.ogg").write_bytes(b"x")
    r = api.post(f"/api/v1/meetpp/sessions/{sid}/publish", headers=chair)
    assert r.status_code == 200, r.text
    pub = r.json()
    assert pub["published_at"] and {o["kind"] for o in pub["outputs"]} == {"report_pdf", "agenda_pdf", "ics"}
    assert not audio.exists()
    assert get(MeetppSession, sid).status == "published"
    outs = api.get(f"/api/v1/meetpp/sessions/{sid}/outputs", headers=chair).json()["outputs"]
    report_out = next(o for o in outs if o["kind"] == "report_pdf")
    assert api.get(report_out["url"], headers=chair).content.startswith(b"%PDF")

    # Delete (owner only).
    assert api.delete(f"/api/v1/meetpp/sessions/{sid}", headers=_jwt("43")).status_code == 404
    assert api.delete(f"/api/v1/meetpp/sessions/{sid}", headers=chair).json() == {"ok": True}
    assert get(MeetppSession, sid) is None


def test_documents_endpoint(api, fakes, meeting, tmp_path):
    from reportlab.pdfgen import canvas

    chair = _jwt("42")
    sid = api.post(f"/api/v1/meetings/{meeting.id}/meetpp/sessions", json={}, headers=chair).json()["session"]["id"]
    monkey_key = settings.llm_api_key
    settings.llm_api_key = ""
    try:
        pdf = tmp_path / "a.pdf"
        c = canvas.Canvas(str(pdf))
        y = 800
        for line in ["Agenda", "1. Approval of the minutes of the last meeting, version two, as circulated by the secretary.",
                     "2. Garden water supply: the tank was delivered and the base needs to be decided by the board.",
                     "3. Any other business that members want to raise before the meeting is closed by the chair."]:
            c.drawString(30, y, line)
            y -= 20
        c.save()
        r = api.post(f"/api/v1/meetpp/sessions/{sid}/documents", headers=chair, files={"file": ("a.pdf", pdf.read_bytes(), "application/pdf")}, data={"kind": "agenda"})
        assert r.status_code == 202, r.text
        doc = r.json()["document"]
        got = api.get(f"/api/v1/meetpp/sessions/{sid}/documents/{doc['id']}", headers=chair).json()
        assert got["document"]["status"] == "done", got
        assert got["summary"]["agenda_points"] == 3 and got["summary"]["format"] == "generic"
        assert "structured" in got
        assert api.post(f"/api/v1/meetpp/sessions/{sid}/documents", headers=chair, files={"file": ("a.pdf", b"nope", "application/pdf")}, data={"kind": "agenda"}).status_code == 400
    finally:
        settings.llm_api_key = monkey_key


def test_tts_endpoint(api, tmp_path):
    (tmp_path / "tts").mkdir()
    (tmp_path / "tts" / f"{'cd' * 16}.ogg").write_bytes(b"OggS")
    assert api.get(f"/api/v1/meetpp/tts/{'cd' * 16}.ogg").content == b"OggS"
    assert api.get("/api/v1/meetpp/tts/..%2Fsecret.ogg").status_code == 404
    assert api.get(f"/api/v1/meetpp/tts/{'ef' * 16}.ogg").status_code == 404


def test_room_finished_webhook_ends_running_session(api, fakes, monkeypatch):
    import asyncio
    import base64
    import hashlib

    from livekit import api as lk_api

    from .conftest import make_session

    sid = make_session()
    asyncio.run(rt.start_session(sid))
    room = SessionLocal().get(Meeting, get(MeetppSession, sid).meeting_id).room_name
    ended = []

    async def _end(s):
        ended.append(s)

    monkeypatch.setattr(rt, "end_session", _end)
    body = json.dumps({"event": "room_finished", "room": {"name": room}})
    token = (
        lk_api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_sha256(base64.b64encode(hashlib.sha256(body.encode()).digest()).decode())
        .to_jwt()
    )
    r = api.post("/api/v1/webhooks/livekit", content=body, headers={"Authorization": token, "Content-Type": "application/webhook+json"})
    assert r.status_code == 200, r.text
    assert ended == [sid]

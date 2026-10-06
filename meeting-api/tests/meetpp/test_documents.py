"""Setup documents (contract §1a): OM meeting-report and agenda PDFs prefill
the outline, the decisions to take, the previous actions and the roster.

The fixtures are synthetic (fictional association, names and topics) and
mimic pypdf's extraction of an OM report: one table cell per line and the
running header/footer repeated on every page.
"""
from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.meetpp import ingest, ops, util
from app.meetpp.models import (
    MeetppAction,
    MeetppAttendee,
    MeetppDecision,
    MeetppDocument,
    MeetppRoster,
    MeetppSection,
    MeetppSession,
)

from .conftest import get, make_session, query, sids

FURNITURE = """Riverside Commons vzw
Meeting report
Riverside board — 27/09/2026 17:00 UTC
Generated 27/09/2026 19:40 UTC
Page {n} of 3
info@riverside.example · https://riverside.example
Riverside Commons vzw · Rue de l'Exemple 12, 1000 Brussels · Enterprise number 0999.999.999 · RPR Brussels"""

PAGE1 = """Riverside Commons vzw
 Non-profit association under Belgian law (vzw)
 Enterprise number: 0999.999.999
Meeting report
Riverside board
Date and time
27/09/2026 17:00 UTC
Location
https://meet.example/blue-river
Type
Board meeting
Status
Held
Attendance
Present: 2 | Represented: 0 | Absent: 1 | Excused: 0 | Not registered: 0
Member
Username
Status
Represented by
Written mandate
Alice Moreau
@amoreau
Present
—
—
Ben Hartley
@bhartley
Present
—
—
Chloe Varga
@cvarga
Absent
—
—
Agenda
1. Approve the minutes of meeting #3
Approve the minutes of 30 August 2026, version 2.
2. Community garden: water supply
The garden relies on a borrowed hose.
(a) Rainwater tank: a 3,000 litre tank behind the shed, quote attached. (b) Municipal connection: quote
expected in October. (c) Question for the board: tank first, or wait for the municipal quote?
3. Volunteer handbook
Draft circulated last week. Points raised: (i) safety rules for the tool shed, (ii) emergency contacts, (iii)
plot allocation. We can vote on it once everyone has read it.
4. Website relaunch: move to a new host
Shall we move the website to a new host before the winter season?
5. Any other business
""" + FURNITURE.format(n=1)

PAGE2 = """Decisions
1. Approve the minutes of meeting #3 (version 2)
The minutes of 30 August 2026 are approved in version 2.
Status: Adopted — decided 27/09/2026 17:09 UTC
Vote
For
Against
Abstain
Result
Approve the minutes
 2
 0
 0
Adopted
2. Rainwater tank — buy and install a 3,000 litre tank behind the garden shed, within a
budget of EUR 1,300
The association buys the tank.
Status: Adopted — decided 27/09/2026 17:31 UTC
Follow-up actions
1. Ask the municipality for a written quote for a metered water connection — carried
forward
Ask the technical department for a written quote.
Status: Open — Assigned to: Ben Hartley — Due 20/09/2026
Reported at this meeting:
The request was filed on 12 September.
Also on: Riverside board (raised there) — 30/08/2026
""" + FURNITURE.format(n=2)

PAGE3 = """2. Draft the volunteer handbook — carried forward
A short handbook for new volunteers.
Status: In progress — Assigned to: Chloe Varga, Alice Moreau — Due 31/10/2026
Progress notes:
12/09/2026: outline agreed.
3. Renew the hall booking for the winter season
Renew the Tuesday booking.
Status: Done — Assigned to: Alice Moreau — Due 25/09/2026 — Completed 24/09/2026
Completion note:
Booking confirmed.
4. Order the rainwater tank from the cheapest supplier and arrange a Saturday
delivery
Order the tank.
Status: Open — Assigned to: Ben Hartley — Due 15/10/2026 — From decision: Rainwater tank — buy and install a
3,000 litre tank behind the garden shed, within a budget of EUR 1,300
Papers filed with this meeting
Paper
File
Agenda point
Agenda of meeting #4
agenda-v2.pdf
—
Minutes
Minutes last saved 27/09/2026 19:40 UTC — version 2
# Minutes — Riverside board
## 1. Approval
""" + FURNITURE.format(n=3)

REPORT = "\n\n".join([PAGE1, PAGE2, PAGE3])

AGENDA_PDF = """Riverside board meeting #5
Sunday, 25 October 2026, 7 pm
Agenda
1.Approve the minutes of meeting #4, version 1.
2.Garden: the tank was delivered and needs a base.
(a) Base: gravel or concrete slab, quotes from two suppliers.
(b) Suggestion: ask the neighbours to share the cost? This would halve it.
3.Insurance: three quotes received for the volunteer accident policy. Shall we take the cheapest quote that
covers tools?
Riverside board meeting #5 Page 1 of 1
"""


def test_om_report_detection_and_parsers():
    assert ingest.is_om_report(REPORT)
    parts = ingest.split_om(ingest.clean_lines(REPORT))
    assert {"Attendance", "Agenda", "Decisions", "Follow-up actions"} <= set(parts)
    assert not any("Page " in s or s.startswith("Generated") for v in parts.values() for s in v)
    att = ingest.parse_attendance(parts["Attendance"])
    assert [(r["name"], r["username"], r["status"]) for r in att] == [
        ("Alice Moreau", "amoreau", "present"), ("Ben Hartley", "bhartley", "present"), ("Chloe Varga", "cvarga", "absent")]
    pts = ingest.parse_agenda(parts["Agenda"], om=True)
    assert [p["title"] for p in pts] == [
        "Approve the minutes of meeting #3", "Community garden: water supply", "Volunteer handbook",
        "Website relaunch: move to a new host", "Any other business"]
    garden = pts[1]
    assert garden["body"] == "The garden relies on a borrowed hose."
    assert [s["label"] for s in garden["subpoints"]] == ["a", "b", "c"]
    assert garden["subpoints"][0]["title"] == "Rainwater tank"
    # Nested (i)(ii)(iii) stay in the body.
    assert pts[2]["subpoints"] == [] and "(ii) emergency contacts" in pts[2]["body"]
    decisions = ingest.parse_decisions(parts["Decisions"])
    assert [d["status"] for d in decisions] == ["adopted", "adopted"]
    assert decisions[1]["title"].endswith("within a budget of EUR 1,300")
    actions = ingest.parse_actions(parts["Follow-up actions"])
    assert [a["status"] for a in actions] == ["open", "in_progress", "done", "open"]
    a1, a2, a3, a4 = actions
    assert a1["title"] == "Ask the municipality for a written quote for a metered water connection" and a1["carried_forward"]
    assert a1["assignees"] == ["Ben Hartley"] and a1["due"] == "2026-09-20"
    assert a1["reported_note"] == "The request was filed on 12 September."
    assert a1["also_on"] == [{"title": "Riverside board", "raised": True, "date": "2026-08-30"}]
    assert a2["assignees"] == ["Chloe Varga", "Alice Moreau"] and a2["progress_notes"] == "12/09/2026: outline agreed."
    assert a3["completed"] == "2026-09-24" and a3["completion_note"] == "Booking confirmed."
    assert a4["title"] == "Order the rainwater tank from the cheapest supplier and arrange a Saturday delivery"
    assert a4["from_decision"].startswith("Rainwater tank — buy and install a 3,000 litre tank")
    assert ingest.om_meeting_title(parts["_head"]) == ("Riverside board", "2026-09-27")


def test_generic_agenda_and_decision_hints():
    assert not ingest.is_om_report(AGENDA_PDF)
    lines = ingest.clean_lines(AGENDA_PDF)
    idx = lines.index("Agenda")
    pts = ingest.parse_agenda(lines[idx + 1:], om=False)
    assert [p["title"] for p in pts] == ["Approve the minutes of meeting #4, version 1", "Garden", "Insurance"]
    assert [s["label"] for s in pts[1]["subpoints"]] == ["a", "b"]
    hints = ingest.decision_hints(pts)
    by_point = {h["point"]: h for h in hints}
    assert by_point["1"]["resolution"] == "that the minutes of meeting #4, version 1 be approved"
    assert by_point["3"]["resolution"].startswith("that we take the cheapest quote")
    assert by_point["2"]["sub"] == "b"


def test_split_subpoints_roman_lists():
    body, subs = ingest.split_subpoints(
        "Intro. (a) First. (b) Second with (i) one and (ii) two. (c) Third. (d) Fourth (e) Fifth (f) 6 (g) 7 (h) 8 (i) roman (ii) list"
    )
    assert body == "Intro."
    assert [label for label, _ in subs] == list("abcdefgh")
    assert subs[1][1] == "Second with (i) one and (ii) two."


async def test_import_previous_report_seeds_roster_actions_and_attendees(fakes):
    sid = make_session(meeting_type="board")
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        doc = MeetppDocument(id=util.ulid(), session_id=sid, kind="previous_notes", filename="report.pdf", status="parsing")
        db.add(doc)
        db.commit()
        changes = await ingest.import_text(db, session, doc, REPORT)
        db.commit()
        summary = util.loads(doc.summary_json, {})
        structured = util.loads(doc.structured_json, {})
        doc_dto = ops.document_dto(doc)
    finally:
        db.close()
    assert summary == {"format": "om_report", "agenda_points": 5, "subpoints": 0, "open_actions": 3, "decisions": 2,
                       "roster_members": 3, "decisions_to_take": 0}
    assert set(doc_dto) == {"id", "kind", "filename", "title", "status", "error", "page_count", "summary"}
    assert doc_dto["title"] == "Report of Riverside board (2026-09-27)"
    assert [d["title"][:20] for d in structured["decisions"]] == ["Approve the minutes ", "Rainwater tank — buy"]
    actions = query(lambda db: db.query(MeetppAction).filter_by(session_id=sid).order_by(MeetppAction.ref).all())
    # The report's status is kept ("In progress" stays in progress).
    assert len(actions) == 3 and all(a.status in ("open", "in_progress") and a.origin == "pdf" for a in actions)
    assert {a.title for a in actions} >= {"Draft the volunteer handbook"}
    roster = query(lambda db: db.query(MeetppRoster).filter_by(series_id=get(MeetppSession, sid).series_id).all())
    assert {r.person_key for r in roster} == {"name:alice moreau", "name:ben hartley", "name:chloe varga"}
    assert all(r.voting for r in roster)
    attendees = query(lambda db: db.query(MeetppAttendee).filter_by(session_id=sid).all())
    assert {a.status for a in attendees} == {"not_registered"} and len(attendees) == 3
    # Previous actions are now open → the Previous actions section applies.
    assert get(MeetppSection, sids(sid)["Previous actions"]).status == "pending"
    db = SessionLocal()
    try:
        state = ops.build_state(db, db.get(MeetppSession, sid))
    finally:
        db.close()
    prev = [a for a in state["actions"] if a["previous"]]
    assert len(prev) == 3 and all(a["report"] is None and a["carried_forward"] for a in prev)
    assert {a["section_id"] for a in prev} == {sids(sid)["Previous actions"]}
    assert state["quorum"] == {"required": 2, "voting_present": 0, "voting_total": 3, "met": False}
    # Re-import is idempotent for actions; the roster is only seeded when empty.
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        doc = MeetppDocument(id=util.ulid(), session_id=sid, kind="previous_notes", filename="again.pdf", status="parsing")
        db.add(doc)
        await ingest.import_text(db, session, doc, REPORT)
        db.commit()
    finally:
        db.close()
    assert query(lambda db: db.query(MeetppAction).filter_by(session_id=sid).count()) == 3
    _ = changes


async def test_import_agenda_prefills_outline_and_decisions_to_take(fakes):
    sid = make_session()
    fakes.llm.add("parse_agenda", {
        "points": [{"number": "2", "title": "Community garden: water supply", "subpoints": [{"label": "c", "title": "Tank or municipal quote"}]}],
        "decisions": [
            {"point": "2", "sub": "c", "title": "Choose the water supply", "resolution": "the association installs the rainwater tank first"},
            {"point": "4", "sub": None, "title": "Move the website", "resolution": "that the website moves to a new host before winter"},
            {"point": "99", "title": "Out of range"},
        ],
    })
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        doc = MeetppDocument(id=util.ulid(), session_id=sid, kind="agenda", filename="report.pdf", status="parsing")
        db.add(doc)
        db.commit()
        await ingest.import_text(db, session, doc, REPORT)
        db.commit()
        summary = util.loads(doc.summary_json, {})
    finally:
        db.close()
    # 2 from the LLM, plus the explicit cues it left out (points 1 and 3).
    assert summary["agenda_points"] == 5 and summary["subpoints"] == 3 and summary["decisions_to_take"] == 4
    ids = sids(sid)
    assert get(MeetppSection, ids["Tank or municipal quote"]).body.startswith("Question for the board")
    # The last point is an AOB point → the fixed AOB section is skipped.
    assert get(MeetppSection, ids["Any other business"]).status in ("skipped", "pending")
    rows = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).order_by(MeetppDecision.created_at).all())
    assert all((d.status, d.origin) == ("pending", "pdf") for d in rows)
    assert [d.section_id for d in rows] == [
        ids["Approve the minutes of meeting #3"], ids["Tank or municipal quote"],
        ids["Volunteer handbook"], ids["Website relaunch: move to a new host"],
    ]
    assert rows[0].resolution == "that the minutes of 30 August 2026, version 2 be approved"
    assert rows[1].resolution == "that the association installs the rainwater tank first"
    # The LLM's decision wins over the cue for the same point.
    assert rows[3].title == "Move the website"
    # Only the relevant part of the report went to the LLM.
    prompt = fakes.llm.prompts("parse_agenda")[0]
    assert "Follow-up actions" not in prompt and "Ask the municipality" not in prompt


async def test_import_agenda_without_llm_uses_hints(fakes, monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")
    sid = make_session()
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, sid)
        doc = MeetppDocument(id=util.ulid(), session_id=sid, kind="agenda", filename="agenda.pdf", status="parsing")
        db.add(doc)
        db.commit()
        await ingest.import_text(db, session, doc, AGENDA_PDF)
        db.commit()
        assert doc.status == "done"
    finally:
        db.close()
    rows = query(lambda db: db.query(MeetppDecision).filter_by(session_id=sid).all())
    assert len(rows) == 3 and all(d.status == "pending" for d in rows)
    assert any(d.resolution and d.resolution.startswith("that we take the cheapest quote") for d in rows)


async def test_process_document_from_a_real_pdf(fakes, tmp_path, monkeypatch):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "meetpp_data_dir", str(tmp_path))
    pdf = tmp_path / "agenda.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    y = 800
    for line in AGENDA_PDF.splitlines():
        c.drawString(40, y, line)
        y -= 16
    c.showPage()
    c.save()
    sid = make_session()
    db = SessionLocal()
    try:
        doc = MeetppDocument(id=util.ulid(), session_id=sid, kind="agenda", filename="agenda.pdf", path=str(pdf), status="uploaded")
        db.add(doc)
        db.commit()
        doc_id = doc.id
    finally:
        db.close()
    await ingest.process_document(doc_id)
    doc = get(MeetppDocument, doc_id)
    assert doc.status == "done", doc.error
    assert doc.page_count == 1
    assert util.loads(doc.summary_json, {})["agenda_points"] == 3
    assert "Insurance" in sids(sid)
    assert fakes.bus.last("state")["delta"]["documents"][0]["status"] == "done"


def test_pdf_byte_checks():
    assert ingest.check_pdf_bytes(b"not a pdf") == (False, "notPdf")
    assert ingest.check_pdf_bytes(b"%PDF-1.7 /Encrypt") == (False, "encrypted")
    assert ingest.validate_extracted({"page_count": 2, "text": "x" * 100}, 50) == (False, "noTextLayer")
    assert ingest.validate_extracted({"error": "encrypted"}, 50) == (False, "encrypted")
    _ = Path

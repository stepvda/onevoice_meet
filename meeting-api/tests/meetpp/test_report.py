"""Meeting report renderer (app.meetpp.report): a pure function of the export.

The fixture is a fictional association and meeting; it exercises every section
of the report, a carried-forward action, block-quoted resolutions and a GFM
"Record of voting" table in the minutes.
"""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.platypus import Paragraph, Table

from app.meetpp.report import (
    build_styles, minutes_to_flowables, render_agenda, render_meeting_report,
)

FIXTURE = Path(__file__).parent / "fixtures" / "report_export.json"

SECTION_HEADINGS = (
    "Meeting report",
    "Attendance",
    "Agenda",
    "Decisions",
    "Follow-up actions",
    "Papers filed with this meeting",
    "Minutes",
)


@pytest.fixture
def export() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _reader(pdf: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(pdf))


def _text(pdf: bytes) -> str:
    """All page text with whitespace collapsed, so wrapped cells still match."""
    raw = "\n".join(page.extract_text() or "" for page in _reader(pdf).pages)
    return " ".join(raw.split())


def _informal(export: dict) -> dict:
    ex = copy.deepcopy(export)
    ex["meeting"]["formal"] = False
    ex["meeting"]["type_label"] = "Meeting"
    for decision in ex["decisions"]:
        decision["vote"] = None
    return ex


# ---------------------------------------------------------------------------
# Meeting report
# ---------------------------------------------------------------------------

def test_report_is_a_multi_page_pdf(export):
    pdf = render_meeting_report(export)
    assert isinstance(pdf, bytes)
    assert pdf.startswith(b"%PDF-")
    assert len(_reader(pdf).pages) > 1


def test_report_contains_every_section_in_order(export):
    text = _text(render_meeting_report(export))
    positions = []
    for heading in SECTION_HEADINGS:
        assert heading in text, heading
        positions.append(text.index(heading))
    # "Agenda" and "Minutes" also occur earlier as words; check the order of the
    # distinctive section headings.
    order = [text.index(h) for h in ("Attendance", "Decisions", "Follow-up actions",
                                     "Papers filed with this meeting")]
    assert order == sorted(order)


def test_report_chrome_and_identity(export):
    pdf = render_meeting_report(export)
    text = _text(pdf)
    pages = len(_reader(pdf).pages)
    assert "Riverside Commons vzw" in text
    assert "Board meeting #4 — 27/09/2026 17:00 UTC" in text
    assert "Generated 28/09/2026 09:15 UTC" in text
    assert f"Page 1 of {pages}" in text and f"Page {pages} of {pages}" in text
    assert "Enterprise number 0999.999.999" in text
    assert "RPR Enterprise Court of Brussels" in text
    assert "Registered seat: Rue de l'Exemple 12, 1000 Brussels, Belgium" in text
    # The facts table and the quorum line.
    for fact in ("Date and time 27/09/2026 17:00 UTC", "Type Board meeting",
                 "Status Held", "Convened at 27/09/2026 17:06 UTC",
                 "Quorum: Three of the four directors present"):
        assert fact in text, fact


def test_report_attendance_register(export):
    text = _text(render_meeting_report(export))
    assert ("Present: 4 | Represented: 1 | Absent: 1 | Excused: 1 | "
            "Not registered: 0") in text
    assert "Member Username Status Represented by Written mandate" in text
    assert "Dana Okafor @dokafor Represented Ben Hartley Written proxy of 25/09/2026" in text
    # Excused members are named under the summary, not listed as attendees.
    assert "Excused: Farah Idris." in text
    assert "@fidris" not in text


def test_report_agenda_points_and_subpoints(export):
    text = _text(render_meeting_report(export))
    assert "2. Community garden: water supply" in text
    assert "(a) Rainwater tank" in text
    assert "(b) Municipal connection" in text


def test_report_condenses_long_agenda_text(export):
    data = copy.deepcopy(export)
    point = data["agenda"][0]
    point["body"] = "The garden committee reported on the season. " * 40 + "TAIL-MARKER at the end."
    text = _text(render_meeting_report(data))
    assert "(full text in the agenda)" in text and "TAIL-MARKER" not in text


def test_report_decision_vote_register(export):
    text = _text(render_meeting_report(export))
    assert "Vote For Against Abstain Result" in text
    assert ("Buy and install a 3,000 litre rainwater tank (budget EUR 1,300) "
            "3 0 1 Adopted") in text
    assert "Status: Adopted — decided 27/09/2026 17:31 UTC" in text
    for row in ("Voting body Board", "Majority required Two-thirds majority",
                "Entitled to vote 4", "Basis Directors in office",
                "Present or represented 4", "Quorum required 3", "Quorum met Yes"):
        assert row in text, row
    assert "Member Vote Cast by Proxy" in text
    assert "Dana Okafor For Ben Hartley Yes" in text
    assert "Adopted by the board on an ordinary majority of the votes cast." in text
    # A formal meeting says so when a decision had no vote.
    assert "No vote was held." in text


def test_report_actions(export):
    text = _text(render_meeting_report(export))
    assert ("1. Ask the municipality for a water connection quote — carried forward"
            in text)
    assert ("Status: Open — Assigned to: Ben Hartley — Due 20/09/2026" in text)
    assert "Reported at this meeting:" in text
    assert "Progress notes:" in text
    assert "Completion note:" in text
    assert "Completed 24/09/2026" in text
    # Raised-there inferred from the earliest sitting, or taken from the flag.
    assert "Also on: Board meeting #3 (raised there) — 30/08/2026" in text
    assert ("Also on: Board meeting #2 (raised there) — 26/07/2026; "
            "Board meeting #3 — 30/08/2026") in text
    assert "From decision: Install a rainwater tank in the community garden" in text
    # New actions are not marked as carried forward.
    assert "4. Order the rainwater tank — carried" not in text


def test_report_papers_and_minutes_header(export):
    text = _text(render_meeting_report(export))
    assert "Paper File Agenda point" in text
    assert "Rainwater tank quotes tank-quotes-2026.pdf 2. Community garden: water supply" in text
    assert "Agenda of Board meeting #4 (v2) board-4-agenda-v2.pdf —" in text
    assert "Minutes last saved 27/09/2026 19:40 UTC — version 2" in text
    assert "Approved by the General Assembly" in text


def test_report_minutes_markdown_is_rendered(export):
    text = _text(render_meeting_report(export))
    assert "RESOLVED: that the minutes of Board meeting #3" in text
    # The GFM table becomes a real table: header and cells, no pipes or rules.
    assert "Decision For Against Abstain Result" in text
    assert "D-2 Rainwater tank 3 0 1 Adopted" in text
    assert "D-4 Website to a new host 1 2 1 Rejected" in text
    # The attendance summary uses "|" as a separator; the minutes must not.
    minutes = text[text.index("Minutes last saved"):]
    assert "|" not in minutes
    assert ":---" not in minutes and "---:" not in minutes
    # Emphasis and heading markers are consumed.
    assert "**" not in minutes
    assert "## " not in minutes and "# Minutes" not in minutes
    assert "> RESOLVED" not in minutes
    assert "R&D-style" in text
    assert "<grant>" in text
    assert "Chloé Varga" in text


def test_report_provisional_banner_follows_placeholders(export):
    assert "PROVISIONAL — NOT FOR FILING" in _text(render_meeting_report(export))

    export["org"]["placeholders"] = ["enterprise", "iban"]
    text = _text(render_meeting_report(export))
    assert "placeholder identification data (enterprise number, bank account number)" in text

    export["org"]["placeholders"] = False
    assert "PROVISIONAL" not in _text(render_meeting_report(export))


def test_informal_meeting_has_no_vote_tables(export):
    text = _text(render_meeting_report(_informal(export)))
    assert "Decisions" in text
    assert "2. Install a rainwater tank in the community garden" in text
    assert "Status: Adopted — decided 27/09/2026 17:31 UTC" in text
    for absent in ("Vote For Against Abstain Result", "Voting body", "Entitled to vote",
                   "Cast by", "No vote was held.", "Approved by the General Assembly"):
        assert absent not in text, absent


def test_general_assembly_title(export):
    export["meeting"]["type_label"] = "General assembly"
    text = _text(render_meeting_report(export))
    assert "General Assembly — report" in text


def test_report_is_deterministic(export):
    assert render_meeting_report(export) == render_meeting_report(copy.deepcopy(export))


def test_report_does_not_mutate_its_input(export):
    before = copy.deepcopy(export)
    render_meeting_report(export)
    render_agenda(export)
    assert export == before


@pytest.mark.parametrize("payload", [
    {},
    {"org": None, "meeting": None, "attendance": None, "agenda": None,
     "decisions": None, "actions": None, "papers": None, "minutes": None,
     "next_agenda": None, "next_meeting": None, "generated_at_iso": None},
    {"org": {}, "meeting": {}, "attendance": {"summary": {}, "rows": []}, "agenda": [],
     "decisions": [], "actions": [], "papers": [], "minutes": {},
     "next_agenda": [], "next_meeting": None},
])
def test_report_tolerates_empty_exports(payload):
    pdf = render_meeting_report(payload)
    assert pdf.startswith(b"%PDF-")
    text = _text(pdf)
    for line in ("No members registered.", "No agenda items.",
                 "No decisions were recorded.", "No follow-up actions.",
                 "No papers were filed.", "No minutes were recorded."):
        assert line in text, line
    assert render_agenda(payload).startswith(b"%PDF-")


def test_report_tolerates_missing_and_odd_fields(export):
    ex = copy.deepcopy(export)
    ex["org"] = {"name": "Riverside Commons vzw", "logo_path": "/nonexistent/logo.png",
                 "placeholders": True}
    ex["meeting"]["date_iso"] = "not a date"
    ex["meeting"]["convened_at_iso"] = None
    ex["generated_at_iso"] = "2026-09-28"
    ex["attendance"] = {"rows": [{"name": None, "username": None, "status_label": None},
                                 {"name": "Ben Hartley", "status_label": "Present"}]}
    for decision in ex["decisions"]:
        decision.pop("number", None)
        decision["resolution"] = None
        decision["how_taken"] = None
        if decision["vote"]:
            decision["vote"] = {"for": None, "ballots": None}
    for action in ex["actions"]:
        for key in ("assignees", "also_on", "description", "due_iso", "status_label"):
            action[key] = None
    ex["actions"][0]["progress_notes"] = ["first note", {"text": "second", "at": "2026-09-01"}]
    ex["papers"] = [{"title": None, "file": None, "agenda_number": "99"}]
    ex["minutes"] = {"markdown": "Just one line & a <tag>.", "version": "n/a"}
    pdf = render_meeting_report(ex)
    text = _text(pdf)
    assert pdf.startswith(b"%PDF-")
    assert "Generated 28/09/2026" in text
    assert "not a date" in text
    assert "Present: 1" in text
    assert "first note" in text and "01/09/2026: second" in text
    assert "Just one line & a <tag>." in text
    assert "Status: Open — Assigned to: —" in text


def test_report_with_logo(export, tmp_path):
    from PIL import Image as PILImage

    logo = tmp_path / "logo.png"
    PILImage.new("RGB", (64, 64), (30, 58, 95)).save(logo)
    export["org"]["logo_path"] = str(logo)
    with_logo = render_meeting_report(export)
    assert with_logo.startswith(b"%PDF-")
    export["org"]["logo_path"] = None
    assert render_meeting_report(export) != with_logo

    # An unreadable logo is skipped, not fatal.
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    export["org"]["logo_path"] = str(broken)
    assert render_meeting_report(export).startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# Next-meeting agenda
# ---------------------------------------------------------------------------

def test_agenda_pdf(export):
    pdf = render_agenda(export)
    assert pdf.startswith(b"%PDF-")
    text = _text(pdf)
    assert "Meeting agenda" in text
    assert "Riverside Commons - board — 25/10/2026 17:00 UTC" in text
    assert "Date and time 25/10/2026 17:00 UTC" in text
    assert "Location Community hall, room 2" in text
    assert "Previous meeting Board meeting #4 — 27/09/2026 17:00 UTC" in text
    assert "2. Rainwater tank: installation report" in text
    assert "Ben Hartley reports on delivery and installation." in text
    assert "Open actions carried forward" in text
    assert "Ask the municipality for a water connection quote" in text
    assert "Draft the volunteer handbook" in text
    # Done actions do not come back to the next meeting.
    assert "Renew the hall booking" not in text
    assert "PROVISIONAL — NOT FOR FILING" in text


def test_agenda_without_next_meeting_or_open_actions(export):
    export["next_meeting"] = None
    for action in export["actions"]:
        action["status_label"] = "Done"
    text = _text(render_agenda(export))
    assert "Date and time To be confirmed" in text
    assert "No open actions are carried forward." in text


def test_agenda_is_deterministic(export):
    assert render_agenda(export) == render_agenda(copy.deepcopy(export))


# ---------------------------------------------------------------------------
# minutes_to_flowables
# ---------------------------------------------------------------------------

def _paragraph_markup(flowables) -> list[str]:
    out = []
    for f in flowables:
        if isinstance(f, Paragraph):
            out.append(f.text)
        elif isinstance(f, Table):
            for row in f._cellvalues:
                for cell in row:
                    if isinstance(cell, Paragraph):
                        out.append(cell.text)
                    elif isinstance(cell, (list, tuple)):
                        out.extend(c.text for c in cell if isinstance(c, Paragraph))
    return out


def test_minutes_to_flowables_structure():
    md = (
        "# Title\n\n"
        "Plain **bold** *italic* `code` ~~gone~~ & <b>raw</b>.\n\n"
        "- one\n  - nested\n- two\n\n"
        "3. third\n4. fourth\n\n"
        "> **RESOLVED:** that it works.\n\n"
        "---\n\n"
        "| Item | Amount |\n|:-----|-------:|\n| Seeds | 12 |\n| Tools & gloves | 40 |\n"
    )
    flow = minutes_to_flowables(md, build_styles())
    markup = _paragraph_markup(flow)
    joined = "\n".join(markup)
    assert "<b>bold</b>" in joined and "<i>italic</i>" in joined
    assert '<font face="Courier">code</font>' in joined
    assert "<strike>gone</strike>" in joined
    # Raw HTML stays text, ampersands are escaped.
    assert "&amp; &lt;b&gt;raw&lt;/b&gt;" in joined
    assert "<b>RESOLVED:</b> that it works." in joined
    assert "Tools &amp; gloves" in joined

    tables = [f for f in flow if isinstance(f, Table)]
    # The block quote and the GFM table.
    assert len(tables) == 2
    gfm = tables[-1]
    assert len(gfm._cellvalues) == 3 and len(gfm._cellvalues[0]) == 2
    assert gfm.repeatRows == 1

    bullets = [f.bulletText for f in flow if isinstance(f, Paragraph) and f.bulletText]
    assert bullets[:3] == ["•", "–", "•"]
    assert "3." in bullets and "4." in bullets


def test_minutes_to_flowables_empty_and_default_styles():
    assert minutes_to_flowables("", build_styles()) == []
    assert minutes_to_flowables(None, build_styles()) == []
    assert minutes_to_flowables("Hello", None)

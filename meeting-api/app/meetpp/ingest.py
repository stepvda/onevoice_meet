"""PDF upload checks, sandboxed text extraction and LLM structuring.

pypdf runs in a subprocess with RLIMIT_AS and a timeout to contain malicious
or bomb PDFs. Fewer than 200 characters per page on average means the PDF has
no text layer and is rejected (no OCR in R1).
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.meetpp import llm, prompts
from app.meetpp.models import MeetppDocument, MeetppSession, utcnow

log = logging.getLogger("app.meetpp")

MIN_CHARS_PER_PAGE = 200
EXTRACT_TIMEOUT_SECONDS = 20

_EXTRACT_SCRIPT = r"""
import json, sys
try:
    from pypdf import PdfReader
except Exception as e:
    print(json.dumps({"error": f"pypdf missing: {e}"})); sys.exit(0)
path = sys.argv[1]
max_pages = int(sys.argv[2])
try:
    reader = PdfReader(path)
    if reader.is_encrypted:
        print(json.dumps({"error": "encrypted"})); sys.exit(0)
    pages = reader.pages
    n = len(pages)
    take = min(n, max_pages)
    parts = []
    for i in range(take):
        try:
            parts.append(pages[i].extract_text() or "")
        except Exception:
            parts.append("")
    text = "\n\n".join(parts)
    print(json.dumps({"page_count": n, "text": text[:200000]}))
except Exception as e:
    print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
"""


def check_pdf_bytes(data: bytes) -> tuple[bool, str | None]:
    if len(data) > settings.meetpp_upload_max_bytes:
        return False, "tooLarge"
    if not data.startswith(b"%PDF-"):
        return False, "notPdf"
    if b"/Encrypt" in data[:4096]:
        return False, "encrypted"
    return True, None


def _rlimit():
    try:
        import resource

        def _set():
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        return _set
    except Exception:  # noqa: BLE001
        return None


def extract_pdf(path: Path) -> dict:
    """Run pypdf in a subprocess with a memory cap and timeout."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _EXTRACT_SCRIPT, str(path), str(settings.meetpp_upload_max_pages)],
            capture_output=True,
            timeout=EXTRACT_TIMEOUT_SECONDS,
            preexec_fn=_rlimit(),
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not out:
        return {"error": (proc.stderr or b"").decode("utf-8", "replace")[:200] or "extraction failed"}
    try:
        return json.loads(out.splitlines()[-1])
    except ValueError:
        return {"error": "bad extractor output"}


def validate_extracted(result: dict, max_pages: int) -> tuple[bool, str | None]:
    if result.get("error"):
        err = result["error"]
        if "encrypted" in err:
            return False, "encrypted"
        return False, "extractFailed"
    page_count = int(result.get("page_count") or 0)
    if page_count > max_pages:
        return False, "tooManyPages"
    text = result.get("text") or ""
    if page_count and len(text) / page_count < MIN_CHARS_PER_PAGE:
        return False, "noTextLayer"
    return True, None


async def structure_document(db: Session, doc: MeetppDocument, session: MeetppSession) -> dict:
    """Structure an uploaded document with one LLM call. Stores the result on
    the document and returns it. Nothing is applied to the session here."""
    text = doc.extracted_text or ""
    if doc.kind == "agenda":
        messages = prompts.build_agenda_messages(text=text, language=session.language)
        purpose = "parse_agenda"
    else:
        messages = prompts.build_notes_messages(text=text, language=session.language)
        purpose = "parse_notes"
    try:
        result = await llm.complete_json(
            purpose=purpose,
            messages=messages,
            max_tokens=1500,
            temperature=0.0,
            session_id=session.id,
            db=db,
            enforce_budget=False,
        )
        structured = llm.parse_json_loose(result.text)
    except llm.LLMError as exc:
        doc.status = "failed"
        doc.error = f"structuring failed: {exc}"[:500]
        doc.updated_at = utcnow()
        db.commit()
        return {}
    doc.structured_json = json.dumps(structured, ensure_ascii=False)
    doc.status = "done"
    doc.error = None
    doc.updated_at = utcnow()
    db.commit()
    return structured


def import_agenda(db: Session, session: MeetppSession, structured: dict) -> int:
    """Populate the Agenda tab from a structured agenda PDF. Only fills an
    empty agenda so it never overwrites items the chair already edited."""
    from ulid import ULID

    from app.meetpp.models import MeetppAgendaItem

    items = structured.get("items") or []
    if not items:
        return 0
    if db.query(MeetppAgendaItem).filter_by(session_id=session.id).count() > 0:
        return 0
    imported = 0
    for i, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        timebox = it.get("timebox_minutes")
        try:
            timebox = int(timebox) if timebox not in (None, "") else None
        except (TypeError, ValueError):
            timebox = None
        db.add(
            MeetppAgendaItem(
                id=str(ULID()),
                session_id=session.id,
                position=i,
                title=title[:400],
                presenter=(str(it.get("presenter")).strip()[:200] if it.get("presenter") else None),
                timebox_minutes=timebox,
                desired_outcome=(str(it.get("outcome")).strip()[:600] if it.get("outcome") else None),
                status="pending",
                source="pdf",
            )
        )
        imported += 1
    if imported:
        session.template = "agenda"
    db.commit()
    return imported


def import_previous_notes(db: Session, session: MeetppSession, structured: dict) -> int:
    """Store actions/decisions from a previous-notes PDF as series items
    (source pdf), so a series run without Meet++ can be bootstrapped."""
    from app.meetpp.models import MeetppAction, MeetppDecision, MeetppSeries
    from ulid import ULID

    series = db.get(MeetppSeries, session.series_id)
    imported = 0
    for a in structured.get("actions") or []:
        title = str(a.get("title") or "").strip()
        if not title:
            continue
        series.action_counter += 1
        db.add(
            MeetppAction(
                id=str(ULID()),
                series_id=series.id,
                session_id=session.id,
                ref=f"A-{series.action_counter:02d}",
                title=title[:300],
                owner_name=(a.get("owner") or None),
                due_date=(a.get("due") or None),
                status=(a.get("status") if a.get("status") in ("open", "done", "dropped") else "open"),
                origin="pdf",
            )
        )
        imported += 1
    for text in structured.get("decisions") or []:
        body = str(text).strip()
        if not body:
            continue
        series.decision_counter += 1
        db.add(
            MeetppDecision(
                id=str(ULID()),
                session_id=session.id,
                series_id=series.id,
                ref=f"D-{series.decision_counter:02d}",
                text=body[:600],
                status="confirmed",
                origin="pdf",
            )
        )
        imported += 1
    db.commit()
    return imported

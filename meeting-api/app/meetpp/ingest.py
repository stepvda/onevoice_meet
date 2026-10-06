"""Setup documents: PDF checks, sandboxed text extraction, structuring and
import (contract §1a, §2.1).

Both uploads are typically OneVoice OM meeting-report PDFs. The report
format is detected and split into its parts deterministically (Attendance,
Agenda, Decisions, Follow-up actions); the regular parts (status lines,
attendance rows, agenda numbering, inline (a)(b) sub-points) are parsed
without the LLM. The LLM only shortens long titles and drafts the decisions
to take, and structures documents that are not in the report format. Only the
relevant part of the text is ever sent to it.

pypdf runs in a subprocess with RLIMIT_AS and a timeout to contain malicious
or bomb PDFs. Fewer than 200 characters per page on average means the PDF has
no text layer and is rejected (no OCR).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.meetpp import bus, llm, ops, outline as outline_mod, prompts, util
from app.meetpp.models import (
    MeetppAction,
    MeetppDecision,
    MeetppDocument,
    MeetppRoster,
    MeetppSeries,
    MeetppSession,
)

log = logging.getLogger("app.meetpp")

MIN_CHARS_PER_PAGE = 200
EXTRACT_TIMEOUT_SECONDS = 30
EXTRACT_MAX_CHARS = 400_000

_EXTRACT_SCRIPT = r"""
import json, sys
try:
    from pypdf import PdfReader
except Exception as e:
    print(json.dumps({"error": f"pypdf missing: {e}"})); sys.exit(0)
path = sys.argv[1]
max_pages = int(sys.argv[2])
max_chars = int(sys.argv[3])
try:
    reader = PdfReader(path)
    if reader.is_encrypted:
        print(json.dumps({"error": "encrypted"})); sys.exit(0)
    pages = reader.pages
    n = len(pages)
    parts = []
    for i in range(min(n, max_pages)):
        try:
            parts.append(pages[i].extract_text() or "")
        except Exception:
            parts.append("")
    text = "\n\n".join(parts)
    print(json.dumps({"page_count": n, "text": text[:max_chars]}))
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
            try:
                resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
            except (ValueError, OSError):
                # Not supported on every platform (macOS); the timeout still applies.
                pass

        return _set
    except Exception:  # noqa: BLE001
        return None


def extract_pdf(path: Path) -> dict:
    """Run pypdf in a subprocess with a memory cap and a timeout."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _EXTRACT_SCRIPT, str(path), str(settings.meetpp_upload_max_pages), str(EXTRACT_MAX_CHARS)],
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
        return False, "encrypted" if "encrypted" in result["error"] else "extractFailed"
    page_count = int(result.get("page_count") or 0)
    if page_count > max_pages:
        return False, "tooManyPages"
    text = result.get("text") or ""
    if page_count and len(text) / page_count < MIN_CHARS_PER_PAGE:
        return False, "noTextLayer"
    return True, None


# ─── Text clean-up and format detection ────────────────────────────────────

OM_HEADINGS = ("Attendance", "Agenda", "Decisions", "Follow-up actions", "Papers filed with this meeting", "Minutes")
_TITLE_DATE_RE = re.compile(r".+ — \d{2}/\d{2}/\d{4} \d{2}:\d{2}( UTC)?$")
_POINT_RE = re.compile(r"^(\d{1,2})\s*[.)]\s*(\S.*)$")
_STATUS_WORDS = {"present": "present", "represented": "represented", "absent": "absent", "excused": "excused", "not registered": "not_registered"}
_ACTION_LABELS = ("Reported at this meeting:", "Progress notes:", "Completion note:", "Also on:", "From decision:")


def raw_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def is_om_report(text: str) -> bool:
    lines = set(raw_lines(text))
    return "Meeting report" in lines and sum(1 for h in OM_HEADINGS if h in lines) >= 3


def clean_lines(text: str) -> list[str]:
    """Drop the repeated page furniture (running header and footer)."""
    lines = raw_lines(text)
    counts = Counter(lines)
    org = lines[0] if lines else None
    out = []
    for s in lines:
        if re.search(r"\bPage \d+ of \d+$", s):
            continue
        if re.match(r"^Generated \d{1,2}/\d{1,2}/\d{4}", s):
            continue
        if "Enterprise number" in s and "·" in s:
            continue
        if re.match(r"^\S+@\S+\s+·\s+https?://\S+$", s):
            continue
        if counts[s] >= 2 and (s == "Meeting report" or s == org or _TITLE_DATE_RE.match(s)):
            continue
        out.append(s)
    return out


def split_om(lines: list[str]) -> dict[str, list[str]]:
    """Split a cleaned OM report into its parts by the heading lines."""
    parts: dict[str, list[str]] = {"_head": []}
    current = "_head"
    order = -1
    for s in lines:
        if s in OM_HEADINGS and OM_HEADINGS.index(s) > order and s not in parts:
            order = OM_HEADINGS.index(s)
            current = s
            parts[current] = []
            if s == "Minutes":
                break
            continue
        parts[current].append(s)
    return parts


def reflow(lines: list[str]) -> str | None:
    """Join wrapped PDF lines; a short line ending a sentence ends a paragraph."""
    out = ""
    prev = ""
    for s in lines:
        if not out:
            out = s
        elif prev.endswith((".", ":", "?", "!")) and len(prev) < 85:
            out += "\n\n" + s
        elif prev.endswith("-") and not prev.endswith(" -"):
            out += s
        else:
            out += " " + s
        prev = s
    return out.strip() or None


def _title_continues(header: str, nxt: str) -> bool:
    h = header.rstrip()
    if h.endswith(("—", "–", "— carried")):
        return True
    low = nxt.lower()
    if low.startswith(("carried forward", "forward")):
        return True
    if nxt.startswith(("Status:", "(")) or _POINT_RE.match(nxt):
        return False
    if "— carried forward" in nxt and len(nxt) < 90:
        return True
    return len(h) >= 70 and len(nxt) <= 60 and not nxt.rstrip().endswith((".", ":", "?", "!"))


_GENERIC_LABELS = {"noted", "note", "suggestion", "question", "confirming", "confirmation", "background", "proposal", "update", "info"}


def short_title(text: str, limit: int = 80) -> str:
    """A title for a point written as a sentence or a paragraph."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    m = re.match(r"^([^:]{2,40}):\s+(.+)$", text)
    if m:
        label = m.group(1).strip()
        first = label.lower().split(" for ")[0]
        if first not in _GENERIC_LABELS and len(label.split()) <= 5:
            return label
        text = m.group(2).strip()
    sentence = re.split(r"(?<=[.?!])\s", text, maxsplit=1)[0].rstrip(".")
    if len(sentence) > limit:
        sentence = sentence[:limit].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"
    return sentence[:1].upper() + sentence[1:]


def split_subpoints(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split inline "(a) … (b) …" sub-points (a, b, c … in sequence). Nested
    "(i) … (xiii)" or "(1) …" lists stay in the sub-point's text."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    markers = []
    expected = "a"
    pos = 0
    for m in re.finditer(r"(?:(?<=\s)|^)\(([a-z])\)\s*", flat):
        if m.start() < pos:
            continue
        letter = m.group(1)
        if letter != expected:
            continue
        if letter == "i" and "(ii)" in flat[m.end():]:
            break  # a roman list, not sub-point (i)
        markers.append(m)
        expected = chr(ord(expected) + 1)
        pos = m.end()
    if not markers:
        return flat, []
    body = flat[: markers[0].start()].strip()
    subs = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(flat)
        subs.append((m.group(1), flat[m.end():end].strip()))
    return body, subs


def _split_numbered(lines: list[str], *, require_status: bool = False) -> list[tuple[int, list[str]]]:
    """Group lines into numbered entries "1. …", "2. …" in sequence. With
    `require_status`, a new entry only starts after the previous entry's
    "Status:" line (decisions and actions)."""
    entries: list[tuple[int, list[str]]] = []
    expected = 1
    status_seen = True
    for s in lines:
        m = _POINT_RE.match(s)
        if m and int(m.group(1)) == expected and (status_seen or not require_status):
            entries.append((expected, [m.group(2).strip()]))
            expected += 1
            status_seen = False
            continue
        if entries:
            entries[-1][1].append(s)
            if s.startswith("Status:"):
                status_seen = True
    return entries


def _take_title(lines: list[str]) -> tuple[str, list[str]]:
    title = lines[0]
    i = 1
    while i < len(lines) and i < 4 and _title_continues(title, lines[i]):
        title = f"{title} {lines[i]}"
        i += 1
    return re.sub(r"\s+", " ", title).strip(), lines[i:]


# ─── Parsers ────────────────────────────────────────────────────────────────


def parse_attendance(lines: list[str]) -> list[dict]:
    header = {"Member", "Username", "Status", "Represented by", "Written mandate"}
    cells = [c for c in lines if c not in header and not re.match(r"^Present:\s*\d", c)]
    rows: list[dict] = []
    # One row per line (pdftotext -layout style).
    for c in cells:
        cols = [x.strip() for x in re.split(r"\s{2,}|\t", c) if x.strip()]
        if len(cols) >= 2 and any(x.lower() in _STATUS_WORDS for x in cols[1:]):
            si = next(i for i, x in enumerate(cols) if i > 0 and x.lower() in _STATUS_WORDS)
            rows.append(_attendance_row(cols[:si], cols[si], cols[si + 1:]))
    if rows:
        return rows
    # One cell per line (pypdf).
    status_idx = [i for i, c in enumerate(cells) if c.lower() in _STATUS_WORDS]
    start = 0
    for k, si in enumerate(status_idx):
        before = cells[start:si]
        nxt = status_idx[k + 1] if k + 1 < len(status_idx) else len(cells) + 3
        after_end = min(si + 3, len(cells))
        if k + 1 < len(status_idx):
            # The next row needs at least a name before its status.
            after_end = min(after_end, nxt - 1)
        after = cells[si + 1:after_end]
        if before:
            rows.append(_attendance_row(before, cells[si], after))
        start = max(after_end, si + 1)
    return rows


def _attendance_row(before: list[str], status: str, after: list[str]) -> dict:
    username = None
    if len(before) >= 2 and (before[-1].startswith("@") or before[-1] in ("—", "-") or " " not in before[-1]):
        username = before[-1]
        before = before[:-1]
    name = " ".join(before).strip()
    if username in ("—", "-", ""):
        username = None
    rep = after[0] if after and after[0] not in ("—", "-") else None
    mandate = after[1] if len(after) > 1 and after[1] not in ("—", "-") else None
    return {
        "name": name,
        "username": username.lstrip("@") if username else None,
        "status": _STATUS_WORDS[status.lower()],
        "represented_by": rep,
        "mandate": mandate,
    }


def _dmy(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if not m:
        m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
        if not m2:
            return None
        y, mo, d = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
    else:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def parse_agenda(lines: list[str], *, om: bool) -> list[dict]:
    points = []
    for number, entry in _split_numbered(lines):
        if om:
            title, rest = _take_title(entry)
            text = reflow(rest) or ""
            body, subs = split_subpoints(text)
            body = body or None
        else:
            full = re.sub(r"\s+", " ", " ".join(entry)).strip()
            body, subs = split_subpoints(full)
            title = short_title(body or full)
            body = body if body and body != title else None
        points.append(
            {
                "number": str(number),
                "title": title[:400],
                "body": body,
                "subpoints": [
                    {"label": label, "title": short_title(t), "body": t if short_title(t) != t else None}
                    for label, t in subs
                ],
            }
        )
    return points


def parse_decisions(lines: list[str]) -> list[dict]:
    out = []
    for number, entry in _split_numbered(lines, require_status=True):
        title, rest = _take_title(entry)
        status_line = next((s for s in rest if s.startswith("Status:")), "")
        m = re.match(r"Status:\s*([A-Za-z ]+?)(?:\s+—|$)", status_line)
        status = (m.group(1).strip().lower() if m else "") or None
        decided = re.search(r"decided (\d{2}/\d{2}/\d{4})(?: (\d{2}:\d{2}))?", status_line)
        out.append(
            {
                "number": number,
                "title": title[:300],
                "status": status,
                "decided_on": _dmy(decided.group(1)) if decided else None,
            }
        )
    return out


def _status_value(text: str) -> str:
    v = text.strip().lower()
    return {"open": "open", "in progress": "in_progress", "done": "done", "cancelled": "cancelled", "canceled": "cancelled"}.get(v, v)


def parse_actions(lines: list[str]) -> list[dict]:
    out = []
    for number, entry in _split_numbered(lines, require_status=True):
        title, rest = _take_title(entry)
        carried = bool(re.search(r"—\s*carried\s+forward\s*$", title))
        title = re.sub(r"\s*—\s*carried\s+forward\s*$", "", title).strip()
        desc_lines: list[str] = []
        status_lines: list[str] = []
        blocks: dict[str, list[str]] = {}
        current = "desc"
        for s in rest:
            if s.startswith("Status:") and current == "desc":
                current = "status"
                status_lines.append(s)
                continue
            label = next((lab for lab in _ACTION_LABELS if s.startswith(lab)), None)
            if label and current != "desc":
                current = label
                blocks[label] = [s[len(label):].strip()] if s[len(label):].strip() else []
                continue
            if current == "desc":
                desc_lines.append(s)
            elif current == "status":
                status_lines.append(s)
            else:
                blocks[current].append(s)
        status_text = " ".join(status_lines)
        from_decision = None
        if "From decision:" in status_text:
            status_text, from_decision = status_text.split("From decision:", 1)
            from_decision = from_decision.strip()
            status_text = status_text.rstrip(" —")
        if "From decision:" in blocks:
            from_decision = " ".join(blocks["From decision:"]).strip() or from_decision
        segs = [x.strip() for x in status_text.split(" — ")]
        status = _status_value(segs[0].replace("Status:", "")) if segs else "open"
        assignees: list[str] = []
        due = completed = None
        for seg in segs[1:]:
            if seg.startswith("Assigned to:"):
                assignees = [a.strip() for a in seg[len("Assigned to:"):].split(",") if a.strip()]
            elif seg.startswith("Due"):
                due = _dmy(seg)
            elif seg.startswith("Completed"):
                completed = _dmy(seg)
        also_on = []
        for part in " ".join(blocks.get("Also on:", [])).split(";"):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^(.*?)\s*(\(raised there\))?\s*—\s*(\d{2}/\d{2}/\d{4})$", part)
            if m:
                also_on.append({"title": m.group(1).strip(), "raised": bool(m.group(2)), "date": _dmy(m.group(3))})
            else:
                also_on.append({"title": part, "raised": "(raised there)" in part, "date": None})
        out.append(
            {
                "number": number,
                "title": title[:300],
                "carried_forward": carried,
                "description": reflow(desc_lines),
                "status": status,
                "assignees": assignees,
                "due": due,
                "completed": completed,
                "reported_note": reflow(blocks.get("Reported at this meeting:", [])),
                "progress_notes": reflow(blocks.get("Progress notes:", [])),
                "completion_note": reflow(blocks.get("Completion note:", [])),
                "also_on": also_on,
                "from_decision": from_decision,
            }
        )
    return out


def om_meeting_title(head: list[str]) -> tuple[str | None, str | None]:
    """(meeting title, date) from the report's facts block."""
    title = None
    when = None
    for i, s in enumerate(head):
        if s == "Date and time" and i + 1 < len(head):
            when = _dmy(head[i + 1])
            if i >= 1 and head[i - 1] not in ("Meeting report",):
                title = head[i - 1]
    return title, when


# ─── Decisions to take ──────────────────────────────────────────────────────

_HINTS = (
    (re.compile(r"\bShall we\s+([^?]{5,400})\?", re.I), lambda m: f"that we {m.group(1).strip()}"),
    (re.compile(r"\bResolved,?\s+that\s+([^?]{5,400})\??", re.I), lambda m: f"that {m.group(1).strip()}"),
    (re.compile(r"\bQuestion for the (?:board|meeting|members|directors|assembly):\s*([^?]{5,400}\?)", re.I), lambda m: None),
    (re.compile(r"\bConfirming:\s*([^.]{5,400})\.?", re.I), lambda m: "that " + m.group(1).strip()[0].lower() + m.group(1).strip()[1:]),
    (re.compile(r"\bSuggestion:\s*([^?]{5,600}\?)", re.I), lambda m: None),
)


def decision_hints(points: list[dict]) -> list[dict]:
    """Deterministic fallback for the decisions to take."""
    out: list[dict] = []
    for p in points:
        places = [(None, p.get("body") or "")] + [(sp["label"], sp.get("body") or sp.get("title") or "") for sp in p.get("subpoints", [])]
        found = False
        for label, text in places:
            for rx, res in _HINTS:
                for m in rx.finditer(text or ""):
                    question = m.group(0).strip()
                    out.append(
                        {
                            "point": p["number"],
                            "sub": label,
                            "title": short_title(question.split(":", 1)[-1] if ":" in question[:40] else question, 120),
                            "resolution": res(m),
                        }
                    )
                    found = True
        title = p.get("title") or ""
        text = f"{p.get('body') or ''}"
        if not found and re.search(r"\bwe can vote on it\b|\bput to the vote\b|\bfor approval\b", text, re.I):
            out.append({"point": p["number"], "sub": None, "title": title, "resolution": f"that the {title[0].lower() + title[1:]} be approved" if title else None})
        elif not found and re.match(r"^approv(e|al of)\b", title, re.I):
            source = re.split(r"(?<=[.?!])\s", (p.get("body") if not title.endswith("…") else None) or title.rstrip("…"), maxsplit=1)[0]
            source = source if re.match(r"^approv(e|al of)\b", source, re.I) else title.rstrip("…")
            obj = re.sub(r"^approv(e|al of)\s+", "", source.rstrip(".").rstrip("…"), flags=re.I)
            out.append({"point": p["number"], "sub": None, "title": title, "resolution": f"that {obj} be approved"})
    return out


# ─── Structuring (deterministic + LLM) ─────────────────────────────────────


async def _llm(db: Session, session_id: str, purpose: str, messages: list[dict], max_tokens: int = 3000) -> dict | None:
    if not llm.llm_configured():
        return None
    try:
        parsed, _ = await llm.complete_parsed(
            db=db, purpose=purpose, messages=messages, max_tokens=max_tokens, temperature=0.0, session_id=session_id
        )
        return parsed
    except llm.LLMError as exc:
        log.warning("meetpp: %s failed: %s", purpose, exc)
        return None


def _normalise_points(raw_points: list) -> list[dict]:
    points = []
    for i, p in enumerate(raw_points or [], start=1):
        if not isinstance(p, dict) or not str(p.get("title") or "").strip():
            continue
        subs = []
        for j, sp in enumerate(util.as_list(p.get("subpoints"))):
            if isinstance(sp, dict) and str(sp.get("title") or "").strip():
                subs.append({"label": chr(ord("a") + j) if j < 26 else str(j + 1), "title": util.truncate(sp["title"], 400), "body": util.truncate(sp.get("body"), 4000)})
            elif isinstance(sp, str) and sp.strip():
                subs.append({"label": chr(ord("a") + j) if j < 26 else str(j + 1), "title": short_title(sp), "body": sp.strip()})
        points.append(
            {
                "number": str(i),
                "title": util.truncate(p["title"], 400),
                "body": util.truncate(p.get("body"), 4000),
                "presenter": util.truncate(p.get("presenter"), 200),
                "timebox_minutes": p.get("timebox_minutes"),
                "subpoints": subs,
            }
        )
    return points


def _normalise_decisions(raw: list, points: list[dict]) -> list[dict]:
    numbers = {p["number"] for p in points}
    out = []
    for d in raw or []:
        if not isinstance(d, dict) or not str(d.get("title") or "").strip():
            continue
        point = str(d.get("point") or "").strip().rstrip(".")
        sub = str(d.get("sub") or "").strip().strip("()").lower() or None
        m = re.fullmatch(r"(\d+)\s*\(?([a-z])?\)?", point)
        if m:
            point, sub = m.group(1), sub or m.group(2)
        if point not in numbers:
            continue
        resolution = util.truncate(d.get("resolution"), 1200)
        if resolution and not resolution.lower().startswith("that "):
            resolution = "that " + resolution[0].lower() + resolution[1:]
        out.append({"point": point, "sub": sub, "title": util.truncate(d["title"], 300), "resolution": resolution})
    return out


async def structure_agenda(db: Session, session_id: str, text: str) -> dict:
    om = is_om_report(text)
    lines = clean_lines(text)
    section = split_om(lines).get("Agenda") if om else None
    if om and section is None:
        om = False
    if section is None:
        # Agenda documents: start at an "Agenda" heading when there is one.
        idx = next((i for i, s in enumerate(lines) if s.lower().rstrip(":") == "agenda"), None)
        section = lines[idx + 1:] if idx is not None else lines
    points = parse_agenda(section, om=om)
    fmt = "om_report" if om else "generic"
    llm_used = False
    if len(points) < 1:
        parsed = await _llm(db, session_id, "parse_agenda", prompts.build_agenda_structure_messages("\n".join(section)))
        if parsed:
            points = _normalise_points(util.as_list(parsed.get("points")))
            decisions = _normalise_decisions(util.as_list(parsed.get("decisions")), points)
            return {"format": fmt, "points": points, "decisions": decisions, "llm": True}
        return {"format": fmt, "points": [], "decisions": [], "llm": False}
    decisions: list[dict] = []
    items = [
        {
            "number": p["number"],
            "title": p["title"],
            "body": util.truncate(p.get("body"), 1500),
            "subpoints": [{"label": sp["label"], "title": sp["title"], "body": util.truncate(sp.get("body"), 1500)} for sp in p["subpoints"]],
        }
        for p in points
    ]
    refined = await _llm(db, session_id, "parse_agenda", prompts.build_agenda_refine_messages(items))
    if refined:
        llm_used = True
        by_number = {p["number"]: p for p in points}
        for t in util.as_list(refined.get("points")):
            if not isinstance(t, dict) or str(t.get("number")) not in by_number:
                continue
            p = by_number[str(t["number"])]
            new_title = util.truncate(t.get("title"), 120)
            if new_title and new_title != p["title"]:
                if p["body"] is None:
                    p["body"] = p["title"]
                p["title"] = new_title
            subs = {sp["label"]: sp for sp in p["subpoints"]}
            for st in util.as_list(t.get("subpoints")):
                if isinstance(st, dict) and str(st.get("label") or "").strip("()").lower() in subs and st.get("title"):
                    sp = subs[str(st["label"]).strip("()").lower()]
                    if sp.get("body") is None:
                        sp["body"] = sp["title"]
                    sp["title"] = util.truncate(st["title"], 120)
        decisions = _normalise_decisions(util.as_list(refined.get("decisions")), points)
    # The explicit cues ("Shall we …?", "Confirming: …", "Question for the
    # board: …") are reliable; add those the LLM left out, one per (sub-)point.
    covered = {(d["point"], d["sub"]) for d in decisions}
    for hint in _normalise_decisions(decision_hints(points), points):
        key = (hint["point"], hint["sub"])
        if key not in covered and (hint["point"], None) not in covered:
            decisions.append(hint)
            covered.add(key)
    order = {p["number"]: i for i, p in enumerate(points)}
    decisions.sort(key=lambda d: (order.get(d["point"], 0), d["sub"] or ""))
    # A title that is still a cut-off first sentence ("On September 29, 2026,
    # Anthropic published …") takes the title of the point's decision to take.
    for p in points:
        title = p.get("title") or ""
        if len(title) > 80 or title.endswith("…"):
            d = next((d for d in decisions if d["point"] == p["number"] and not d["sub"]), None)
            if d and len(d["title"]) <= 80:
                if p.get("body") is None:
                    p["body"] = title
                p["title"] = d["title"]
    return {"format": fmt, "points": points, "decisions": decisions, "llm": llm_used}


async def structure_previous_notes(db: Session, session_id: str, text: str) -> dict:
    om = is_om_report(text)
    if om:
        parts = split_om(clean_lines(text))
        title, when = om_meeting_title(parts.get("_head", []))
        return {
            "format": "om_report",
            "meeting_title": title,
            "meeting_date": when,
            "attendance": parse_attendance(parts.get("Attendance", [])),
            "decisions": parse_decisions(parts.get("Decisions", [])),
            "actions": parse_actions(parts.get("Follow-up actions", [])),
            "agenda_points": len(parse_agenda(parts.get("Agenda", []), om=True)),
        }
    lines = clean_lines(text)
    parsed = await _llm(db, session_id, "parse_notes", prompts.build_notes_structure_messages("\n".join(lines)))
    if not parsed:
        return {"format": "generic", "attendance": [], "decisions": [], "actions": [], "llm_failed": True}
    actions = []
    for i, a in enumerate(util.as_list(parsed.get("actions")), start=1):
        if not isinstance(a, dict) or not str(a.get("title") or "").strip():
            continue
        actions.append(
            {
                "number": i,
                "title": util.truncate(a["title"], 300),
                "carried_forward": False,
                "description": util.truncate(a.get("description"), 4000),
                "status": _status_value(str(a.get("status") or "open").replace("_", " ")),
                "assignees": [str(x)[:200] for x in util.as_list(a.get("assignees")) if x][:10],
                "due": _dmy(str(a.get("due") or "")),
                "completed": None,
                "reported_note": None,
                "progress_notes": util.truncate(a.get("progress_notes"), 4000),
                "completion_note": None,
                "also_on": [],
                "from_decision": None,
            }
        )
    attendance = []
    for r in util.as_list(parsed.get("attendance")):
        if isinstance(r, dict) and str(r.get("name") or "").strip():
            st = str(r.get("status") or "present").lower()
            attendance.append({"name": util.truncate(r["name"], 200), "username": util.truncate(r.get("username"), 200),
                               "status": st if st in ("present", "represented", "absent", "excused") else "present",
                               "represented_by": None, "mandate": None})
    decisions = [
        {"number": i, "title": util.truncate(d.get("title"), 300), "status": str(d.get("status") or "").lower() or None, "decided_on": None}
        for i, d in enumerate(util.as_list(parsed.get("decisions")), start=1)
        if isinstance(d, dict) and d.get("title")
    ]
    return {"format": "generic", "attendance": attendance, "decisions": decisions, "actions": actions}


# ─── Import ─────────────────────────────────────────────────────────────────


def import_agenda(db: Session, session: MeetppSession, structured: dict) -> ops.Changes:
    """Replace the outline's agenda points and prefill the decisions to take."""
    changes = ops.Changes()
    points = structured.get("points") or []
    if not points:
        return changes
    changed = outline_mod.replace_agenda(
        db,
        session,
        [
            {
                "title": p["title"],
                "body": p.get("body"),
                "presenter": p.get("presenter"),
                "timebox_minutes": p.get("timebox_minutes"),
                "subpoints": [{"title": sp["title"], "body": sp.get("body")} for sp in p.get("subpoints", [])],
            }
            for p in points
        ],
        source="pdf",
    )
    changes.sections.update(changed)
    for s in db.query(outline_mod.MeetppSection).filter_by(session_id=session.id).all():
        changes.sections.add(s.id)
    # Previous prefilled decisions are replaced.
    for d in db.query(MeetppDecision).filter_by(session_id=session.id, origin="pdf", status="pending", locked=False).all():
        changes.removed.append({"kind": "decision", "id": d.id})
        ops.delete_decision(db, d)
    db.flush()
    o = outline_mod.load(db, session.id)
    by_number = {o.numbers.get(s.id): s for s in o.flat if o.numbers.get(s.id)}
    series = db.get(MeetppSeries, session.series_id)
    for d in structured.get("decisions") or []:
        point = by_number.get(str(d.get("point")))
        if point is None:
            continue
        target = point
        if d.get("sub"):
            kids = o.children.get(point.id, [])
            idx = ord(str(d["sub"])[0]) - ord("a")
            if 0 <= idx < len(kids):
                target = kids[idx]
        series.decision_counter = int(series.decision_counter or 0) + 1
        row = MeetppDecision(
            id=util.ulid(),
            session_id=session.id,
            series_id=session.series_id,
            ref=f"D-{series.decision_counter}",
            section_id=target.id,
            title=d["title"],
            resolution=d.get("resolution"),
            status="pending",
            origin="pdf",
            evidence_json="[]",
        )
        db.add(row)
        changes.decisions.add(row.id)
    db.flush()
    return changes


def _roster_empty(db: Session, session: MeetppSession) -> bool:
    return db.query(MeetppRoster.id).filter_by(series_id=session.series_id).first() is None


_STATUS_RANK = {"open": 0, "in_progress": 1}


def _names(raw) -> set[str]:
    return {util.norm_name(n if isinstance(n, str) else (n or {}).get("name", "")) for n in raw or []} - {""}


def _same_action(title: str, assignees: set[str], row: MeetppAction) -> bool:
    """The same follow-up action, as two documents word it: close titles, or
    related titles with a shared assignee."""
    j = util.jaccard(title, row.title)
    if j >= 0.6:
        return True
    return j >= 0.35 and bool(assignees & _names(util.loads(row.assignees_json, [])))


def merge_actions(db: Session, session: MeetppSession, actions: list[dict], changes: ops.Changes) -> int:
    """Open follow-up actions from a setup PDF → series actions (origin pdf, To
    review). An action that overlaps one already known (from the other PDF, or
    an earlier meeting) is merged into it rather than listed twice: missing
    fields are filled in, progress notes appended, and the further status kept.
    Returns the number of new actions."""
    series = db.get(MeetppSeries, session.series_id)
    existing = db.query(MeetppAction).filter_by(series_id=session.series_id).all()
    added = 0
    for a in actions:
        if a.get("status") not in ("open", "in_progress") or not str(a.get("title") or "").strip():
            continue
        names = _names(a.get("assignees"))
        match = next((x for x in existing if _same_action(a["title"], names, x)), None)
        if match is not None:
            if match.locked:
                continue
            desc = (a.get("description") or "").strip()
            if desc and desc not in (match.description or ""):
                match.description = f"{match.description}\n\n{desc}" if match.description else desc
            if not match.due_date and a.get("due"):
                match.due_date = a["due"]
            if not util.loads(match.assignees_json, []) and names:
                match.assignees_json = util.dumps([{"name": n, "person_key": None} for n in a.get("assignees") or []])
            notes = (a.get("progress_notes") or "").strip()
            if notes and notes not in (match.progress_notes or ""):
                match.progress_notes = f"{match.progress_notes}\n{notes}" if match.progress_notes else notes
            if _STATUS_RANK.get(a.get("status"), 0) > _STATUS_RANK.get(match.status, 0) and match.status in _STATUS_RANK:
                match.status = a["status"]
            changes.actions.add(match.id)
            continue
        series.action_counter = int(series.action_counter or 0) + 1
        row = MeetppAction(
            id=util.ulid(),
            series_id=session.series_id,
            session_id=session.id,
            ref=f"A-{series.action_counter}",
            title=a["title"],
            description=a.get("description"),
            assignees_json=util.dumps([{"name": n, "person_key": None} for n in a.get("assignees") or []]),
            due_date=a.get("due"),
            status="in_progress" if a.get("status") == "in_progress" else "open",
            progress_notes=a.get("progress_notes"),
            origin="pdf",
            evidence_json="[]",
        )
        db.add(row)
        existing.append(row)
        changes.actions.add(row.id)
        added += 1
    db.flush()
    return added


def import_previous_notes(db: Session, session: MeetppSession, structured: dict) -> tuple[ops.Changes, dict]:
    """Open actions → series actions (origin pdf, To review); the attendance
    table seeds an empty roster (voting members) and expected attendees."""
    changes = ops.Changes()
    counts = {"open_actions": 0, "roster_members": 0}
    counts["open_actions"] = merge_actions(db, session, structured.get("actions") or [], changes)
    if structured.get("attendance") and _roster_empty(db, session):
        for r in structured["attendance"]:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            key = util.name_key(name)
            if db.query(MeetppRoster.id).filter_by(series_id=session.series_id, person_key=key).first():
                continue
            roster = MeetppRoster(
                id=util.ulid(),
                series_id=session.series_id,
                person_key=key,
                display_name=name[:200],
                username=r.get("username"),
                voting=True,
                active=True,
            )
            db.add(roster)
            counts["roster_members"] += 1
        db.flush()
        from app.meetpp.runtime import expected_attendees

        changes.attendees.update(expected_attendees(db, session))
        changes.quorum = True
    # Seeded attendees may already match people in the room (by name).
    db.flush()
    changes.sections.update(outline_mod.apply_skip_rules(db, session))
    return changes, counts


def summary_for(structured: dict, kind: str) -> dict:
    points = structured.get("points") or []
    actions = structured.get("actions") or []
    return {
        "format": structured.get("format") or "generic",
        "agenda_points": len(points) if points else int(structured.get("agenda_points") or 0),
        "subpoints": sum(len(p.get("subpoints") or []) for p in points),
        "open_actions": sum(1 for a in actions if a.get("status") in ("open", "in_progress")),
        "decisions": len(structured.get("decisions") or []) if kind == "previous_notes" else 0,
        "roster_members": len(structured.get("attendance") or []),
        "decisions_to_take": len(structured.get("decisions") or []) if kind == "agenda" else 0,
    }


def _doc_title(doc: MeetppDocument, structured: dict, text: str) -> str:
    if doc.kind == "previous_notes" and structured.get("meeting_title"):
        when = structured.get("meeting_date")
        return f"Report of {structured['meeting_title']}" + (f" ({when})" if when else "")
    first = next(iter(raw_lines(text)), "") if structured.get("format") != "om_report" else ""
    base = "Agenda" if doc.kind == "agenda" else "Notes of the previous meeting"
    return f"{base} — {first}"[:400] if first and len(first) < 120 else base


async def process_document(doc_id: str) -> None:
    """Background job: extract, structure and import one uploaded document."""
    db = SessionLocal()
    try:
        doc = db.get(MeetppDocument, doc_id)
        if doc is None:
            return
        session = db.get(MeetppSession, doc.session_id)
        doc.status = "parsing"
        db.commit()
        result = await asyncio.to_thread(extract_pdf, Path(doc.path))
        valid, err = validate_extracted(result, settings.meetpp_upload_max_pages)
        if not valid:
            doc.status, doc.error = "failed", err
            await bus.publish_changes(db, session, ops.Changes(documents={doc.id}))
            return
        doc.page_count = int(result.get("page_count") or 0)
        doc.extracted_text = result.get("text") or ""
        db.commit()
        changes = await import_text(db, session, doc, doc.extracted_text)
        changes.documents.add(doc.id)
        await bus.publish_changes(db, session, changes)
    except Exception as exc:  # noqa: BLE001
        log.exception("meetpp: document %s failed", doc_id)
        db.rollback()
        doc = db.get(MeetppDocument, doc_id)
        if doc is not None:
            doc.status = "failed"
            doc.error = f"{type(exc).__name__}: {exc}"[:500]
            session = db.get(MeetppSession, doc.session_id)
            await bus.publish_changes(db, session, ops.Changes(documents={doc.id}))
    finally:
        db.close()


async def import_text(db: Session, session: MeetppSession, doc: MeetppDocument, text: str) -> ops.Changes:
    """Structure the extracted text and import it (separated from the PDF
    extraction so tests can feed text directly)."""
    if doc.kind == "agenda":
        structured = await structure_agenda(db, session.id, text)
        # Re-read: the meeting may have started while the model read the agenda.
        db.refresh(session)
        if not structured["points"]:
            doc.status, doc.error = "failed", "no agenda points found"
            doc.structured_json = util.dumps(structured)
            return ops.Changes()
        changes = ops.Changes()
        if session.status == "setup":
            changes = import_agenda(db, session, structured)
            if structured.get("format") == "om_report":
                # An agenda in the report format may list the follow-up actions
                # too: merged with those of the previous report, never twice.
                actions = parse_actions(split_om(clean_lines(text)).get("Follow-up actions", []))
                if actions:
                    structured["actions"] = actions
                    merge_actions(db, session, actions, changes)
                    changes.sections.update(outline_mod.apply_skip_rules(db, session))
        else:
            structured["not_applied"] = "the agenda is only replaced during setup"
    else:
        structured = await structure_previous_notes(db, session.id, text)
        db.refresh(session)
        if structured.get("llm_failed"):
            doc.status, doc.error = "failed", "not a meeting report and the LLM is unavailable"
            doc.structured_json = util.dumps(structured)
            return ops.Changes()
        changes, _counts = import_previous_notes(db, session, structured)
    doc.structured_json = util.dumps(structured)
    doc.summary_json = util.dumps(summary_for(structured, doc.kind))
    doc.title = _doc_title(doc, structured, text)
    doc.status = "done"
    doc.error = None
    db.flush()
    return changes

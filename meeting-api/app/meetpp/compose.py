"""Minutes composition (FDD §8.7, Appendix C.2/C.3).

Running notes are collected live; when a section closes its narrative is
composed from the section's transcript (best tier), notes, decisions,
actions and agenda text. At the end the final composition writes the
opening and adjournment, and the record of voting and provenance are
generated from the data. `minutes_markdown()` assembles the whole document
in the structure of the OM meeting report's minutes.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.meetpp import bus, governance, llm, ops, outline as outline_mod, prompts, util
from app.meetpp.models import (
    MeetppAction,
    MeetppActionReport,
    MeetppAttendee,
    MeetppDecision,
    MeetppMinute,
    MeetppSection,
    MeetppSegment,
    MeetppSeries,
    MeetppSession,
    MeetppVote,
)
from app.models import Meeting

log = logging.getLogger("app.meetpp")

MAX_WORDS = 6000  # hard limit: only a runaway draft is rejected
MIN_WORDS = 50
# Minutes run at about a third of the words spoken (the meeting-240 reference:
# 13,100 transcript words, 4,600 words of minutes); the prompt asks for this.
TARGET_RATIO = 0.33
TARGET_MIN, TARGET_MAX = 80, 1500
# A draft over 1.5x the target (and 150 words over) is condensed by a second
# call that sees only the draft; if that fails the draft stands: long minutes
# are better than none. (Asking the composer itself again did not shorten it.)
TARGET_SLACK, TARGET_SLACK_WORDS = 1.5, 150
SUBSTANTIAL_TRANSCRIPT_WORDS = 200
NO_RESOLUTION = "No resolution was put."
MAX_TRANSCRIPT_CHARS = 60000

# Sections being composed right now (one composition per section at a time).
_in_flight: set[tuple[str, str]] = set()


def _type_label(series: MeetppSeries | None) -> str:
    return governance.TYPE_LABELS.get(series.meeting_type if series else "informal", "Meeting")


def _hhmm(dt: datetime | None) -> str:
    dt = util.aware(dt)
    return dt.strftime("%H:%M UTC") if dt else "—"


def _seg_dict(s: MeetppSegment) -> dict:
    t = util.aware(s.t_start or s.created_at)
    return {"seq": s.seq, "time": t.strftime("%H:%M:%S") if t else "", "name": s.name or "Unknown", "text": s.best_text}


def subtree_ids(o: outline_mod.Outline, top: MeetppSection) -> list[str]:
    return [top.id] + [c.id for c in o.children.get(top.id, [])]


def section_segments(db: Session, session: MeetppSession, o: outline_mod.Outline, top: MeetppSection) -> list[MeetppSegment]:
    ids = subtree_ids(o, top)
    cited: set[int] = set()
    for m in db.query(MeetppMinute).filter(MeetppMinute.session_id == session.id, MeetppMinute.section_id.in_(ids)).all():
        for n in util.loads(m.notes_json, []):
            cited.update(int(x) for x in (n.get("evidence") or []) if isinstance(x, int))
    for d in db.query(MeetppDecision).filter(MeetppDecision.session_id == session.id, MeetppDecision.section_id.in_(ids)).all():
        cited.update(util.loads(d.evidence_json, []))
    for a in db.query(MeetppAction).filter(MeetppAction.session_id == session.id, MeetppAction.section_id.in_(ids)).all():
        cited.update(util.loads(a.evidence_json, []))
    q = db.query(MeetppSegment).filter(MeetppSegment.session_id == session.id, MeetppSegment.is_gap.is_(False))
    cond = MeetppSegment.section_id.in_(ids)
    if cited:
        cond = or_(cond, MeetppSegment.seq.in_(sorted(cited)[:2000]))
    return q.filter(cond).order_by(MeetppSegment.seq).all()


def source_tier(segments: list[MeetppSegment]) -> str:
    if not segments:
        return "live"
    refined = sum(1 for s in segments if s.text_refined)
    if refined == 0:
        return "live"
    return "refined" if refined == len(segments) else "mixed"


def resolved_blocks(markdown: str) -> list[str]:
    """Texts of the `> **RESOLVED:** …` block quotes."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in (markdown or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            body = stripped.lstrip(">").strip()
            if "RESOLVED" in body.upper() and re.search(r"\*\*\s*RESOLVED\s*:?\s*\*\*|RESOLVED\s*:", body, re.I):
                if current is not None:
                    blocks.append(" ".join(current))
                current = [re.sub(r"^\*\*\s*RESOLVED\s*:?\s*\*\*\s*:?|^RESOLVED\s*:", "", body, flags=re.I).strip()]
            elif current is not None:
                current.append(body)
        else:
            if current is not None:
                blocks.append(" ".join(current))
                current = None
    if current is not None:
        blocks.append(" ".join(current))
    return [b.strip() for b in blocks if b.strip()]


def _matches(block: str, d: MeetppDecision) -> bool:
    b = block.lower()
    for cand in (d.resolution or "", d.title or ""):
        c = cand.lower().strip()
        if not c:
            continue
        if c[:60] in b or b[:60] in c:
            return True
        if util.jaccard(block, cand) >= 0.45:
            return True
    return False


def markdown_of(parsed: dict) -> str:
    """The minutes text of a reply: its "markdown", or — when a model garbles the
    key ({": ": "### 7.1 …"}, seen with the local model) — its one long text."""
    value = parsed.get("markdown")
    if isinstance(value, str) and value.strip():
        return value
    texts = [v for v in parsed.values() if isinstance(v, str) and len(v) > 40]
    return texts[0] if len(texts) == 1 else ""


def validate_section(markdown: str, adopted: list[MeetppDecision], substantial: bool) -> str | None:
    md = (markdown or "").strip()
    if not md:
        return "the markdown is empty"
    words = util.word_count(md)
    if words > MAX_WORDS:
        return f"the minutes are too long ({words} words; at most {MAX_WORDS})"
    if substantial and words < MIN_WORDS:
        return f"the minutes are too short ({words} words; at least {MIN_WORDS})"
    for block in resolved_blocks(md):
        if not any(_matches(block, d) for d in adopted):
            return f"RESOLVED block for a decision that was not adopted: {block[:120]!r}"
    return None


def _resolution_text(d: MeetppDecision) -> str:
    text = (d.resolution or d.title or "").strip()
    if not text.lower().startswith("that "):
        text = "that " + text[0].lower() + text[1:] if text else "that"
    return text


RESOLUTION_KINDS = ("agenda", "aob", "goal", "deliverables", "next_steps", "planning")
MIN_DISCUSSION_WORDS = 25


def finish_markdown(markdown: str, adopted: list[MeetppDecision], *, kind: str = "agenda") -> str:
    """Append a RESOLVED block for every adopted decision the draft missed, and
    the "No resolution was put." sentence when nothing was adopted under an
    agenda point (never under the opening, previous/new actions or closing)."""
    md = (markdown or "").strip()
    blocks = resolved_blocks(md)
    for d in adopted:
        if not any(_matches(b, d) for b in blocks):
            md += f"\n\n> **RESOLVED:** {_resolution_text(d)}"
    if kind in RESOLUTION_KINDS:
        if not adopted and NO_RESOLUTION.lower() not in md.lower():
            md += f"\n\n{NO_RESOLUTION}"
    else:
        md = re.sub(r"\n*\s*" + re.escape(NO_RESOLUTION) + r"\s*$", "", md).strip()
    return md.strip()


def _heading(o: outline_mod.Outline, s: MeetppSection) -> str:
    num = o.numbers.get(s.id)
    return f"{num}. {s.title}" if num else s.title


async def compose_section(session_id: str, section_id: str, *, force: bool = False) -> str:
    """Compose (or re-compose) the narrative of one top-level section.
    Returns the resulting minute status."""
    key = (session_id, section_id)
    if key in _in_flight:
        return "composing"
    _in_flight.add(key)
    try:
        return await _compose_section(session_id, section_id, force=force)
    finally:
        _in_flight.discard(key)


async def _compose_section(session_id: str, section_id: str, *, force: bool) -> str:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            return "missing"
        o = outline_mod.load(db, session_id)
        section = o.by_id.get(section_id)
        if section is None:
            return "missing"
        top = o.top(section)
        minute = outline_mod.minute_for(db, session, top.id)
        if minute.locked and not force:
            db.commit()
            return minute.status
        if force:
            minute.locked = False
        minute.status = "composing"
        minute.error = None
        changes = ops.Changes(minutes={minute.id})
        await bus.publish_changes(db, session, changes)

        series = db.get(MeetppSeries, session.series_id)
        ids = subtree_ids(o, top)
        decisions = (
            db.query(MeetppDecision)
            .filter(MeetppDecision.session_id == session.id, MeetppDecision.section_id.in_(ids))
            .order_by(MeetppDecision.created_at)
            .all()
        )
        adopted = [d for d in decisions if d.status == "adopted"]
        actions = (
            db.query(MeetppAction)
            .filter(MeetppAction.session_id == session.id, MeetppAction.section_id.in_(ids))
            .all()
        )
        home = outline_mod.previous_actions_home(o)
        if top.kind == "previous_actions" or (home is not None and top.id == home.id):
            # The previous actions reported on here, plus any action raised here.
            previous = [a for a in ops.session_actions(db, session) if ops.is_previous_action(a, session)]
            actions = previous + [a for a in actions if a not in previous]
        notes: list[str] = []
        for m in db.query(MeetppMinute).filter(MeetppMinute.session_id == session.id, MeetppMinute.section_id.in_(ids)).all():
            label = o.numbers.get(m.section_id or "") or ""
            for n in util.loads(m.notes_json, []):
                notes.append(f"- {('(' + label + ') ') if label and m.section_id != top.id else ''}{n.get('text')}")
        segments = section_segments(db, session, o, top)
        transcript = [_seg_dict(s) for s in segments]
        total = 0
        trimmed: list[dict] = []
        for line in reversed(transcript):
            total += len(line["text"]) + 30
            if total > MAX_TRANSCRIPT_CHARS:
                break
            trimmed.append(line)
        transcript = list(reversed(trimmed))
        tier = source_tier(segments)
        substantial = sum(util.word_count(s["text"]) for s in transcript) >= SUBSTANTIAL_TRANSCRIPT_WORDS
        members = [
            f"{a.display_name} ({a.status})"
            for a in db.query(MeetppAttendee).filter_by(session_id=session.id).order_by(MeetppAttendee.display_name).all()
        ]
        subpoints = [
            f"{o.numbers.get(c.id) or '-'} {c.title}" + (f" — {c.body}" if c.body else "")
            for c in o.children.get(top.id, [])
        ]
        decision_lines = [
            f"{d.ref} [{d.status.upper()}] {d.title}" + (f" — resolution: {d.resolution}" if d.resolution else "")
            + (f" — how taken: {d.how_taken}" if d.how_taken else "")
            for d in decisions
            if d.status != "pending"
        ] + [f"{d.ref} [NOT TAKEN] {d.title}" for d in decisions if d.status == "pending"]
        action_lines = []
        for a in actions:
            who = ", ".join(x.get("name", "") for x in util.loads(a.assignees_json, []) if isinstance(x, dict))
            action_lines.append(f"{a.ref} [{a.status}] {a.title}" + (f" — {who}" if who else "") + (f" — due {a.due_date}" if a.due_date else ""))

        transcript_words = sum(util.word_count(s["text"]) for s in transcript)
        new_actions = [a for a in actions if not ops.is_previous_action(a, session)]
        discussed = (
            transcript_words >= MIN_DISCUSSION_WORDS
            or bool(notes)
            or any(d.status != "pending" for d in decisions)
            or bool(new_actions)
        )
        if not discussed:
            # Never let the agenda text pass for discussion (FDD §8.7).
            narrative = (
                "The item was opened but not discussed in substance."
                if (top.started_at or top.ended_at or (top.elapsed_seconds or 0) > 0)
                else "_Not discussed._"
            )
            if adopted:
                narrative = finish_markdown(narrative, adopted, kind=top.kind)
            return await _store(db, session, top.id, narrative, [], tier, None, force)
        if not llm.llm_configured():
            return await _store(db, session, top.id, None, [], tier, "the LLM is not configured", force)

        target_words = max(TARGET_MIN, min(TARGET_MAX, int(transcript_words * TARGET_RATIO)))
        messages = prompts.build_section_messages(
            target_words=target_words,
            org=settings.meetpp_org_name,
            meeting_type_label=_type_label(series),
            heading=_heading(o, top),
            agenda_body=top.body,
            subpoints=subpoints,
            members=members,
            decisions=decision_lines,
            actions=action_lines,
            notes=notes,
            transcript=transcript,
        )

        def _validate(parsed: dict) -> str | None:
            return validate_section(markdown_of(parsed), adopted, substantial)

        try:
            parsed, _result = await llm.complete_parsed(
                db=db,
                purpose="compose_section",
                messages=messages,
                max_tokens=6000,
                temperature=0.3,
                session_id=session.id,
                validate=_validate,
            )
        except llm.LLMError as exc:
            log.warning("MEETPP_COMPOSE sid=%s section=%s status=failed error=%s", session.id, top.id, exc)
            return await _store(db, session, top.id, None, [], tier, str(exc)[:300], force)
        markdown = finish_markdown(markdown_of(parsed), adopted, kind=top.kind)
        if util.word_count(markdown) > max(TARGET_SLACK * target_words, target_words + TARGET_SLACK_WORDS):
            shorter = await _condense(db, session, series, markdown, target_words, adopted, substantial)
            if shorter:
                markdown = finish_markdown(shorter, adopted, kind=top.kind)
        verify = [str(v)[:300] for v in (parsed.get("verify") or []) if v][:20]
        return await _store(db, session, top.id, markdown, verify, tier, None, force)
    finally:
        db.close()


# Condense: one key point per ~35 words of the target.
WORDS_PER_POINT = 35


async def _condense(
    db: Session,
    session: MeetppSession,
    series: MeetppSeries | None,
    markdown: str,
    target_words: int,
    adopted: list[MeetppDecision],
    substantial: bool,
) -> str | None:
    """Shorten an over-long section draft in two calls — its key points, then
    minutes written from those points alone; None keeps the draft. The RESOLVED
    blocks are added back by finish_markdown."""
    max_points = max(4, round(target_words / WORDS_PER_POINT))
    headings = re.findall(r"^### .+$", markdown, re.M)

    def _valid_points(parsed: dict) -> str | None:
        points = parsed.get("points")
        if not isinstance(points, list) or not [x for x in points if str(x).strip()]:
            return "no key points"
        if len(points) > round(max_points * 1.3) + 2:
            return f"{len(points)} points; merge related points into at most {max_points}, one short sentence each"
        return None

    def _valid_minutes(parsed: dict) -> str | None:
        md = markdown_of(parsed)
        error = validate_section(md, adopted, substantial)
        if error:
            return error
        words = util.word_count(md)
        if words >= util.word_count(markdown):
            return "the minutes are not shorter"
        if words < target_words // 2:
            return f"the minutes are too short ({words} words; about {target_words} were asked)"
        if re.search(r"\[\d+(\.\d+)*\]", md):
            return "write the sub-headings, not the bracketed numbers"
        return None

    try:
        parsed, _ = await llm.complete_parsed(
            db=db, purpose="compose_condense", session_id=session.id, temperature=0.2, max_tokens=6000,
            messages=prompts.build_condense_points_messages(markdown=markdown, max_points=max_points),
            validate=_valid_points,
        )
        points = [str(x).strip() for x in parsed["points"] if str(x).strip()][: max_points + 5]
        parsed, _ = await llm.complete_parsed(
            db=db, purpose="compose_condense", session_id=session.id, temperature=0.3, max_tokens=4000,
            messages=prompts.build_condense_write_messages(
                org=settings.meetpp_org_name, meeting_type_label=_type_label(series),
                points=points, headings=headings, target_words=target_words,
            ),
            validate=_valid_minutes,
        )
    except llm.LLMError as exc:
        log.info("MEETPP_COMPOSE sid=%s condense failed, keeping the draft: %s", session.id, exc)
        return None
    return markdown_of(parsed) or None


RECONCILE_MAX_MINUTES_CHARS = 60000


async def reconcile_previous_actions(session_id: str) -> int:
    """At finalisation: previous actions that got no report during the meeting
    are checked against the composed minutes, and what the minutes say about
    them (status, a "reported at this meeting" note) is recorded. During the
    meeting a report is only taken when an action is named explicitly; in a
    meeting without an actions review the actions come up inside the agenda
    points (FDD v3.2, §8.5). Returns the number of actions reported. Never
    raises."""
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None or not llm.llm_configured():
            return 0
        series = db.get(MeetppSeries, session.series_id)
        reported = {
            r.action_id for r in db.query(MeetppActionReport).filter_by(session_id=session.id).all() if (r.note or "").strip()
        }
        candidates = [
            a for a in ops.session_actions(db, session)
            if ops.is_previous_action(a, session) and a.id not in reported and not a.locked
        ]
        if not candidates:
            return 0
        minutes = minutes_markdown(db, session, sections_only=True)[:RECONCILE_MAX_MINUTES_CHARS]
        if not minutes.strip():
            return 0
        by_ref = {a.ref: a for a in candidates}
        lines = []
        for a in candidates:
            who = ", ".join(x.get("name", "") for x in util.loads(a.assignees_json, []) if isinstance(x, dict))
            lines.append(
                f"{a.ref} · {a.title}" + (f" · {who}" if who else "") + f" · {a.status}"
                + (f" — {util.truncate(a.description, 240)}" if a.description else "")
            )

        def _validate(parsed: dict) -> str | None:
            # Lenient: refs not listed (an action reported live) are dropped
            # below, and so is every report whose quote is not in the minutes.
            return None if isinstance(parsed.get("reports"), list) else "reports must be a list"

        try:
            parsed, _ = await llm.complete_parsed(
                db=db, purpose="compose_actions", session_id=session.id, temperature=0.1, max_tokens=4000,
                messages=prompts.build_reconcile_messages(
                    org=settings.meetpp_org_name, meeting_type_label=_type_label(series), actions=lines, minutes=minutes,
                ),
                validate=_validate,
            )
        except llm.LLMError as exc:
            log.info("MEETPP_RECONCILE sid=%s failed: %s", session.id, exc)
            return 0
        changes = ops.Changes()
        count = 0
        flat = util.normalize_text(minutes)
        for r in parsed.get("reports") or []:
            if not isinstance(r, dict) or r.get("ref") not in by_ref:
                continue
            note = util.truncate(str(r.get("note") or "").strip(), 600)
            quote = util.normalize_text(str(r.get("quote") or ""))
            # Only what the minutes say: the quoted sentence must be in them.
            if not note or len(quote) < 20 or quote not in flat:
                log.info("MEETPP_RECONCILE sid=%s dropped %s (quote not in the minutes)", session.id, r.get("ref"))
                continue
            a = by_ref[r["ref"]]
            status = str(r.get("status") or "").strip().lower().replace(" ", "_")
            if status in ("open", "in_progress", "done", "cancelled") and status != a.status:
                a.status = status
                if status == "done":
                    a.completed_at = a.completed_at or util.now()
                    a.completion_note = a.completion_note or note
            report = db.query(MeetppActionReport).filter_by(action_id=a.id, session_id=session.id).first()
            if report is None:
                report = MeetppActionReport(action_id=a.id, session_id=session.id, note="", evidence_json="[]")
                db.add(report)
            report.note = note
            report.status_at_report = a.status
            changes.actions.add(a.id)
            count += 1
        db.flush()
        await bus.publish_changes(db, session, changes)
        log.info("MEETPP_RECONCILE sid=%s candidates=%s reported=%s", session.id, len(candidates), count)
        return count
    except Exception:  # noqa: BLE001 — finalisation goes on without it
        log.exception("meetpp: reconciling previous actions failed for %s", session_id)
        db.rollback()
        return 0
    finally:
        db.close()


VERIFY_MAX_TRANSCRIPT_CHARS = 120000


async def verify_closed_previous_actions(session_id: str) -> int:
    """At finalisation: every previous action the AI marked done during the
    meeting must be confirmed by a transcript line saying that this work is
    finished (quoted, and found in the transcript); otherwise it is reopened.
    A wrong "done" drops an action from the carried-forward list unseen, a
    wrong "open" stays in view (replay: "prove the full restore" was closed on
    "the restore worked fine" although "the running platform" was not yet
    restored). Human changes (locked) are kept. Returns the number reopened.
    Never raises."""
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None or not llm.llm_configured():
            return 0
        start = util.aware(session.started_at or session.created_at)
        closed = [
            a for a in ops.session_actions(db, session)
            if ops.is_previous_action(a, session) and a.status == "done" and not a.locked
            and a.completed_at is not None and (start is None or util.aware(a.completed_at) >= start)
        ]
        if not closed:
            return 0
        segs = (
            db.query(MeetppSegment)
            .filter(MeetppSegment.session_id == session.id, MeetppSegment.is_gap.is_(False))
            .order_by(MeetppSegment.seq)
            .all()
        )
        transcript = "\n".join(f"{s.name or 'Unknown'}: {s.best_text}" for s in segs)[-VERIFY_MAX_TRANSCRIPT_CHARS:]
        if not transcript.strip():
            return 0
        by_ref = {a.ref: a for a in closed}
        lines = [f"{a.ref} · {a.title}" + (f" — {util.truncate(a.description, 240)}" if a.description else "") for a in closed]
        try:
            parsed, _ = await llm.complete_parsed(
                db=db, purpose="compose_actions", session_id=session.id, temperature=0.0, max_tokens=2000,
                messages=prompts.build_verify_closed_messages(actions=lines, transcript=transcript),
                validate=lambda p: None if isinstance(p.get("confirmed"), list) else "confirmed must be a list",
            )
        except llm.LLMError as exc:
            log.info("MEETPP_VERIFY sid=%s failed, closures kept: %s", session.id, exc)
            return 0
        flat = util.normalize_text(transcript)
        confirmed = set()
        for c in parsed.get("confirmed") or []:
            if isinstance(c, dict) and c.get("ref") in by_ref:
                quote = util.normalize_text(str(c.get("quote") or ""))
                if len(quote) >= 15 and quote in flat:
                    confirmed.add(c["ref"])
        changes = ops.Changes()
        reopened = 0
        for ref, a in by_ref.items():
            if ref in confirmed:
                continue
            a.status = "open"
            a.completed_at = None
            report = db.query(MeetppActionReport).filter_by(action_id=a.id, session_id=session.id).first()
            if report is not None:
                report.status_at_report = "open"
            changes.actions.add(a.id)
            reopened += 1
        db.flush()
        await bus.publish_changes(db, session, changes)
        log.info("MEETPP_VERIFY sid=%s closed=%s confirmed=%s reopened=%s", session.id, len(closed), len(confirmed), reopened)
        return reopened
    except Exception:  # noqa: BLE001 — finalisation goes on without it
        log.exception("meetpp: verifying closed actions failed for %s", session_id)
        db.rollback()
        return 0
    finally:
        db.close()


async def _store(
    db: Session,
    session: MeetppSession,
    section_id: str,
    markdown: str | None,
    verify: list[str],
    tier: str,
    error: str | None,
    force: bool = False,
) -> str:
    db.expire_all()
    session = db.get(MeetppSession, session.id)
    section = db.get(MeetppSection, section_id)
    minute = outline_mod.minute_for(db, session, section_id)
    if minute.locked:
        # Edited by a person while composing: the edit wins.
        db.commit()
        return minute.status
    if not force and section is not None and section.status == "live" and session.status in ("running", "paused"):
        # Reopened (Back / Undo) while composing: keep it a draft.
        minute.status = "notes"
        await bus.publish_changes(db, session, ops.Changes(minutes={minute.id}))
        return minute.status
    if error is not None or markdown is None:
        minute.status = "failed"
        minute.error = (error or "composition failed")[:500]
    else:
        minute.narrative_md = markdown
        minute.status = "composed"
        minute.error = None
        minute.version = int(minute.version or 0) + 1
        minute.composed_at = util.now()
        minute.source_tier = tier
        minute.verify_json = util.dumps(verify)
    log.info("MEETPP_COMPOSE sid=%s section=%s status=%s tier=%s version=%s", session.id, section_id, minute.status, tier, minute.version)
    await bus.publish_changes(db, session, ops.Changes(minutes={minute.id}))
    return minute.status


def sections_to_compose(db: Session, session: MeetppSession, *, only_missing: bool = True) -> list[str]:
    """Top-level sections that need a (re-)composition at finalisation:
    discussed sections without a composed narrative, and composed ones whose
    transcript source improved since (unless locked)."""
    o = outline_mod.load(db, session.id)
    out = []
    minutes = {
        m.section_id: m for m in db.query(MeetppMinute).filter_by(session_id=session.id, kind="section").all()
    }
    for s in o.tops():
        if s.status in ("skipped",):
            continue
        m = minutes.get(s.id)
        if m is not None and m.locked:
            continue
        discussed = s.status in ("done", "live", "deferred") or s.started_at is not None
        has_notes = m is not None and bool(util.loads(m.notes_json, []))
        has_items = (
            db.query(MeetppDecision.id)
            .filter(MeetppDecision.session_id == session.id, MeetppDecision.section_id.in_(subtree_ids(o, s)), MeetppDecision.status != "pending")
            .first()
            is not None
        )
        if not (discussed or has_notes or has_items):
            continue
        if s.kind != "agenda" and not (has_notes or has_items or section_segments(db, session, o, s)):
            # A fixed section nobody spoke in gets no minutes of its own.
            continue
        if m is None or m.status in ("notes", "failed", "composing") or not m.narrative_md:
            out.append(s.id)
            continue
        if not only_missing:
            out.append(s.id)
            continue
        tier_now = source_tier(section_segments(db, session, o, s))
        if m.status == "composed" and tier_now != (m.source_tier or "live") and tier_now != "live":
            out.append(s.id)
    return out


# ─── Final composition ──────────────────────────────────────────────────────


def _chair_key(meeting: Meeting | None) -> str | None:
    return f"sub:{meeting.owner_user_id}" if meeting is not None else None


def attendance_lists(db: Session, session: MeetppSession) -> dict:
    meeting = db.get(Meeting, session.meeting_id)
    chair = _chair_key(meeting)
    out: dict[str, list[str]] = {"present": [], "represented": [], "absent": [], "excused": [], "not_registered": []}
    for a in db.query(MeetppAttendee).filter_by(session_id=session.id).order_by(MeetppAttendee.display_name).all():
        name = a.display_name + (" (chair)" if chair and a.person_key == chair and a.status == "present" else "")
        if a.status == "represented":
            name += f" (by {a.represented_by})" if a.represented_by else ""
        out.setdefault(a.status, []).append(name)
    out["present"].sort(key=lambda n: 0 if n.endswith("(chair)") else 1)
    return out


def _quorum_line(db: Session, session: MeetppSession, series: MeetppSeries | None) -> str | None:
    if not governance.is_formal(series):
        return None
    q = governance.quorum(db, session, series)
    noun = "directors" if series.meeting_type == "board" else "voting members"
    state = "met" if q["met"] else "not met"
    return f"{state} ({q['voting_present']} of {q['voting_total']} {noun} present or represented; quorum {q['required']})"


def facts(db: Session, session: MeetppSession) -> dict:
    series = db.get(MeetppSeries, session.series_id)
    meeting = db.get(Meeting, session.meeting_id)
    o = outline_mod.load(db, session.id)
    lists = attendance_lists(db, session)
    decisions = db.query(MeetppDecision).filter_by(session_id=session.id).all()
    return {
        "meeting": meeting.display_title if meeting else "",
        "type": _type_label(series),
        "date": util.aware(session.started_at or session.created_at).strftime("%A %d %B %Y"),
        "started_at": _hhmm(session.started_at),
        "ended_at": _hhmm(session.ended_at),
        "present": lists["present"],
        "represented": lists["represented"],
        "absent": lists["absent"],
        "excused": lists["excused"],
        "quorum": _quorum_line(db, session, series),
        "agenda": [f"{o.numbers.get(s.id) or ''} {s.title}".strip() for s in o.tops() if s.kind == "agenda"],
        "deferred": [s.title for s in o.tops() if s.status == "deferred"],
        "decisions": [f"{d.ref} {d.status}: {d.title}" for d in decisions if d.status != "pending"],
        "decisions_not_taken": [d.title for d in decisions if d.status == "pending"],
    }


def default_opening(f: dict) -> str:
    parts = [f"The meeting opened at {f['started_at']}."]
    if f["present"]:
        parts.append(f"Present: {', '.join(f['present'])}.")
    if f["represented"]:
        parts.append(f"Represented: {', '.join(f['represented'])}.")
    if f["absent"]:
        parts.append(f"Absent: {', '.join(f['absent'])}.")
    if f["excused"]:
        parts.append(f"Excused: {', '.join(f['excused'])}.")
    if f["quorum"]:
        parts.append(f"The quorum was {f['quorum']}.")
    return " ".join(parts)


def default_adjournment(f: dict) -> str:
    return f"The chair declared the meeting adjourned at {f['ended_at']}."


def voting_record_markdown(db: Session, session: MeetppSession) -> str | None:
    series = db.get(MeetppSeries, session.series_id)
    if not governance.is_formal(series):
        return None
    decisions = [
        d
        for d in db.query(MeetppDecision).filter_by(session_id=session.id).order_by(MeetppDecision.created_at).all()
        if d.status in ("adopted", "rejected")
    ]
    if not decisions:
        return "No proposition was put to the vote."
    rule = governance.MAJORITY_LABELS.get(series.majority_rule, "Simple majority")
    rows = ["| # | Resolution | Rule | Result |", "|---|---|---|---|"]
    for i, d in enumerate(decisions, start=1):
        vote = db.query(MeetppVote).filter_by(decision_id=d.id).first()
        result = governance.RESULT_LABELS.get((vote.result if vote and vote.result else d.status), d.status.title())
        if vote and vote.tally_for is not None:
            result += f" ({vote.tally_for}–{vote.tally_against or 0}–{vote.tally_abstain or 0})"
        text = (_resolution_text(d) + f" ({d.ref})").replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {i} | {text} | {rule} | {result} |")
    return "\n".join(rows)


def provenance_markdown(db: Session, session: MeetppSession, verify: list[str]) -> str:
    segs = db.query(MeetppSegment).filter(MeetppSegment.session_id == session.id, MeetppSegment.is_gap.is_(False)).all()
    refined = sum(1 for s in segs if s.text_refined)
    gaps = db.query(MeetppSegment).filter(MeetppSegment.session_id == session.id, MeetppSegment.is_gap.is_(True)).count()
    jobs = util.loads(session.jobs_json, {})
    tier2 = (jobs.get("tier2") or {}).get("status")
    parts = [
        f"Transcribed live per participant on the meeting server (tier 1, faster-whisper {settings.stt_model}, English)."
    ]
    if refined and segs:
        pct = round(100 * refined / len(segs))
        parts.append(
            "The transcript was refined on the association's Mac Studio (tier 2, Whisper large-v3-turbo); "
            f"{pct} % of the {len(segs)} transcript lines are refined text."
        )
    else:
        parts.append("The tier-2 refinement was not available, so these minutes rest on the live-quality transcript.")
    if tier2 == "skipped":
        parts.append("The final refinement pass was skipped.")
    parts.append("Speakers were attributed from each participant's own audio track.")
    if gaps:
        parts.append(f"The transcript has {gaps} interruption(s), marked as gaps.")
    opted = db.query(MeetppAttendee).filter_by(session_id=session.id, opted_out=True).count()
    if opted:
        parts.append(f"{opted} participant(s) opted out of transcription and were not transcribed.")
    parts.append(
        f"The minutes were drafted with {settings.llm_model} ({settings.llm_provider_label}) from the transcript and the "
        "running notes, and are subject to the chair's review."
    )
    text = " ".join(parts)
    if verify:
        text += "\n\nTo verify against the recording: " + "; ".join(dict.fromkeys(v.strip() for v in verify if v.strip())) + "."
    return text


def _set_part(db: Session, session: MeetppSession, kind: str, markdown: str | None, changes: ops.Changes) -> None:
    m = outline_mod.minute_for(db, session, None, kind=kind)
    if m.locked or markdown is None:
        return
    m.narrative_md = markdown
    m.status = "composed"
    m.error = None
    m.version = int(m.version or 0) + 1
    m.composed_at = util.now()
    changes.minutes.add(m.id)


async def compose_final(session_id: str) -> dict:
    """Opening, adjournment (LLM with deterministic fallback), record of voting
    and provenance (deterministic). Returns final_json."""
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        series = db.get(MeetppSeries, session.series_id)
        f = facts(db, session)
        verify: list[str] = []
        for m in db.query(MeetppMinute).filter_by(session_id=session.id, kind="section").all():
            verify.extend(util.loads(m.verify_json, []))
        llm_out: dict = {}
        if llm.llm_configured():
            section_md = minutes_markdown(db, session, sections_only=True)

            def _validate(parsed: dict) -> str | None:
                if not str(parsed.get("opening") or "").strip():
                    return "opening is empty"
                return None

            try:
                llm_out, _ = await llm.complete_parsed(
                    db=db,
                    purpose="compose_final",
                    messages=prompts.build_final_messages(
                        org=settings.meetpp_org_name, meeting_type_label=_type_label(series), facts=f, section_minutes=section_md
                    ),
                    max_tokens=2500,
                    temperature=0.2,
                    session_id=session.id,
                    validate=_validate,
                )
            except llm.LLMError as exc:
                log.warning("MEETPP_FINALISE sid=%s final composition fell back: %s", session.id, exc)
                llm_out = {}
        db.expire_all()
        session = db.get(MeetppSession, session_id)
        verify.extend(str(v)[:300] for v in (llm_out.get("verify") or []) if v)
        verify = list(dict.fromkeys(verify))[:30]
        changes = ops.Changes()
        _set_part(db, session, "opening", str(llm_out.get("opening") or "").strip() or default_opening(f), changes)
        _set_part(db, session, "adjournment", str(llm_out.get("adjournment") or "").strip() or default_adjournment(f), changes)
        _set_part(db, session, "voting_record", voting_record_markdown(db, session), changes)
        _set_part(db, session, "provenance", provenance_markdown(db, session, verify), changes)
        final = util.loads(session.final_json, {})
        o = outline_mod.load(db, session.id)
        next_agenda = []
        for item in llm_out.get("next_agenda") or []:
            if isinstance(item, dict) and item.get("title"):
                next_agenda.append({"title": util.truncate(item["title"], 300), "body": util.truncate(item.get("body"), 2000)})
            elif isinstance(item, str) and item.strip():
                next_agenda.append({"title": util.truncate(item, 300), "body": None})
        for s in o.tops():
            if s.status == "deferred" and all(util.jaccard(s.title, n["title"]) < 0.6 for n in next_agenda):
                next_agenda.append({"title": s.title, "body": s.body})
        final.update(
            {
                "summary": [str(x)[:400] for x in (llm_out.get("summary") or []) if x][:5],
                "next_agenda": next_agenda[:30],
                "required_next": [
                    {"name": a.display_name, "reason": a.required_reason}
                    for a in db.query(MeetppAttendee).filter_by(session_id=session.id, required_next=True).all()
                ],
                "verify": verify,
                "provenance": {"llm_model": settings.llm_model, "stt_model": settings.stt_model},
            }
        )
        session.final_json = util.dumps(final)
        await bus.publish_changes(db, session, changes)
        return final
    finally:
        db.close()


# ─── Document assembly ─────────────────────────────────────────────────────


def _part(db: Session, session: MeetppSession, kind: str) -> str | None:
    m = db.query(MeetppMinute).filter_by(session_id=session.id, kind=kind, section_id=None).first()
    return (m.narrative_md or "").strip() or None if m else None


def _notes_fallback(m: MeetppMinute | None) -> str | None:
    if m is None:
        return None
    notes = [n.get("text") for n in util.loads(m.notes_json, []) if isinstance(n, dict) and n.get("text")]
    return "\n".join(f"- {n}" for n in notes) if notes else None


def section_body(db: Session, session: MeetppSession, o: outline_mod.Outline, s: MeetppSection, minutes: dict) -> str | None:
    m = minutes.get(s.id)
    text = (m.narrative_md or "").strip() if m is not None and m.narrative_md else None
    if text:
        return text
    notes = []
    for sid in subtree_ids(o, s):
        fb = _notes_fallback(minutes.get(sid))
        if fb:
            notes.append(fb)
    adopted = [
        d
        for d in db.query(MeetppDecision).filter(MeetppDecision.session_id == session.id, MeetppDecision.section_id.in_(subtree_ids(o, s))).all()
        if d.status == "adopted"
    ]
    if notes or adopted:
        return finish_markdown("\n".join(notes), adopted, kind=s.kind)
    if s.status == "deferred":
        return "_Deferred to the next meeting._"
    if s.kind == "agenda" and s.status in ("pending",) and not s.started_at:
        return "_Not discussed._"
    return None


def minutes_markdown(db: Session, session: MeetppSession, *, sections_only: bool = False) -> str:
    """The whole minutes document (structure of the OM meeting report)."""
    series = db.get(MeetppSeries, session.series_id)
    meeting = db.get(Meeting, session.meeting_id)
    o = outline_mod.load(db, session.id)
    minutes = {
        m.section_id: m for m in db.query(MeetppMinute).filter_by(session_id=session.id, kind="section").all()
    }
    body: list[str] = []
    opening_section = None
    for s in o.tops():
        if s.status == "skipped":
            continue
        text = section_body(db, session, o, s, minutes)
        if s.kind == "opening":
            opening_section = text
            if sections_only and text:
                body.append(f"## OPENING DISCUSSION\n\n{text}")
            continue
        if text is None and s.kind not in ("agenda",):
            continue
        body.append(f"## {_heading(o, s)}\n\n{text or '_Not discussed._'}")
    if sections_only:
        return "\n\n".join(body)

    title = meeting.display_title if meeting else (series.title if series else "Meeting")
    when = util.aware(session.started_at or session.created_at)
    lists = attendance_lists(db, session)
    head = [f"# Minutes — {title}", ""]
    date_line = f"**{settings.meetpp_org_name}** · {when.strftime('%A %d %B %Y')}, {when.strftime('%H:%M')} UTC"
    location = f"{settings.public_url}/{meeting.room_name}" if meeting else None
    head.append(date_line + (f"  \nHeld online at {location}" if location else ""))
    head.append("")
    people = []
    if lists["present"]:
        people.append(f"**Present:** {', '.join(lists['present'])}.")
    if lists["represented"]:
        people.append(f"**Represented:** {', '.join(lists['represented'])}.")
    if lists["absent"]:
        people.append(f"**Absent:** {', '.join(lists['absent'])}.")
    if lists["excused"]:
        people.append(f"**Excused:** {', '.join(lists['excused'])}.")
    quorum_line = _quorum_line(db, session, series)
    if quorum_line:
        people.append(f"**Quorum:** {quorum_line}.")
    if people:
        head.append(" ".join(people))
        head.append("")
    # The final composition integrates the opening discussion into the opening
    # part; the section narrative is only used when that part does not exist.
    opening = _part(db, session, "opening")
    opening_text = opening or opening_section
    if opening_text:
        head.append(f"**Opening.** {opening_text}")
        head.append("")
    head.append("---")
    doc = "\n".join(head) + "\n\n" + "\n\n".join(body)
    adjournment = _part(db, session, "adjournment")
    if adjournment:
        doc += f"\n\n## Adjournment\n\n{adjournment}"
    tail = []
    voting = _part(db, session, "voting_record")
    if voting and governance.is_formal(series):
        tail.append(f"### Record of voting\n\n{voting}")
    provenance = _part(db, session, "provenance")
    if provenance:
        tail.append(f"### Provenance\n\n{provenance}")
    if tail:
        doc += "\n\n---\n\n" + "\n\n".join(tail)
    return doc.strip() + "\n"


def schedule_section(session_id: str, section_id: str, *, delay: float = 0.0, force: bool = False) -> asyncio.Task | None:
    """Fire-and-forget composition (used when a section closes)."""

    async def _run():
        if delay:
            await asyncio.sleep(delay)
            db = SessionLocal()
            try:
                s = db.get(MeetppSection, section_id)
                if s is None or s.status == "live":
                    return
            finally:
                db.close()
        try:
            await compose_section(session_id, section_id, force=force)
        except Exception:  # noqa: BLE001
            log.exception("meetpp: composition of %s failed", section_id)

    try:
        return asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        return None

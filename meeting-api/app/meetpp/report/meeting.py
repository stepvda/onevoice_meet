"""The meeting report: a port of the OM ``build_meeting_pdf`` onto the Meet++
export dict (contract §7).

Section order, wording and furniture follow the OM report: identity block,
"Meeting report" with its key/value table and quorum line, attendance register,
agenda, decisions with their vote register, follow-up actions, papers, minutes,
signature line. The OM builder queried its own tables; here every value comes
from the export, every field is optional, and nothing outside this package is
imported. The one deliberate departure is the minutes, which are rendered from
Markdown (see ``markdown.py``) instead of printed verbatim in Courier.
"""
from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer

from .common import (
    DASH, as_dict, as_list, count, cover, document, esc, fmt_date, fmt_datetime,
    footer_segments, keep, keep_with_table, p, paged_canvas, parse_when, render,
    rich_text, signature_block, table, text_of, yes_no,
)
from .markdown import minutes_to_flowables
from .strings import T
from .styles import build_styles

# Agenda text per (sub-)point in the meeting report; the full text stays in the agenda.
AGENDA_BODY_MAX = 900

_CLOSED_ACTION = {"done", "completed", "complete", "cancelled", "canceled", "closed",
                  "dropped", "withdrawn"}


# ---------------------------------------------------------------------------
# Small readers over the export
# ---------------------------------------------------------------------------

def is_general_assembly(meeting: dict) -> bool:
    label = text_of(meeting.get("type_label")).lower()
    kind = text_of(meeting.get("meeting_type")).lower()
    return label == "general assembly" or kind == "general_assembly"


def is_formal(meeting: dict) -> bool:
    """Whether the meeting is a formal sitting (board or general assembly).

    ``meeting.formal`` decides when the export carries it; otherwise the type
    label does. An informal meeting prints no "No vote was held." lines and no
    approval signature line.
    """
    flag = meeting.get("formal")
    if isinstance(flag, bool):
        return flag
    label = text_of(meeting.get("type_label")).lower()
    return label in ("board meeting", "general assembly")


def meeting_title(meeting: dict) -> str:
    return text_of(meeting.get("title")) or text_of(meeting.get("series_title"))


def header_meta(title: str, when_value: Any, when_text: str = "") -> str:
    """The second header line: "Title — dd/mm/yyyy hh:mm UTC"."""
    when = when_text or (fmt_datetime(when_value) if parse_when(when_value) else "")
    parts = [x for x in (title, when) if x]
    return f" {DASH} ".join(parts)


def username(value: Any) -> str:
    name = text_of(value)
    if not name:
        return DASH
    return name if "@" in name else f"@{name}"


def person(value: Any) -> str:
    """A name from a string or a ``{name: …}`` dict."""
    if isinstance(value, dict):
        return text_of(value.get("name")) or text_of(value.get("display_name"))
    return text_of(value)


def is_open_action(action: dict) -> bool:
    if text_of(action.get("completed_iso")):
        return False
    return text_of(action.get("status_label") or "Open").lower() not in _CLOSED_ACTION


def _letter(index: int) -> str:
    """a, b, …, z, aa, ab, … for unlabelled sub-points."""
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(ord("a") + rem) + out
    return out


# ---------------------------------------------------------------------------
# Shared section builders (also used by the next-meeting agenda)
# ---------------------------------------------------------------------------

def _condensed(body, limit: int | None):
    """A long agenda text cut at a sentence (or word) boundary; the meeting
    report points to the agenda for the rest."""
    text = text_of(body)
    if not limit or len(text) <= limit:
        return body
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    cut = cut[: end + 1] if end >= limit // 2 else cut.rsplit(" ", 1)[0]
    return f"{cut.rstrip()} {T('meeting.agenda_more')}"


def agenda_points(story: list, items: list, styles, *, max_body: int | None = None) -> None:
    """Numbered agenda points with their bodies and lettered sub-points."""
    sub_style = ParagraphStyle(
        "MeetppSubpoint", parent=styles["BodyText2"], alignment=TA_LEFT,
        fontName="Helvetica-Bold", leftIndent=18, bulletIndent=0,
        bulletFontName="Helvetica-Bold", spaceBefore=2, spaceAfter=2,
        keepWithNext=True,
    )
    for idx, raw in enumerate(items, 1):
        item = as_dict(raw)
        number = text_of(item.get("number")) or str(idx)
        title = text_of(item.get("title")) or DASH
        story.append(p(f"{number}. {title}", styles["SubsubTitle"]))
        if text_of(item.get("body")):
            story.extend(rich_text(_condensed(item.get("body"), max_body), styles))
        for j, raw_sub in enumerate(as_list(item.get("subpoints"))):
            sub = as_dict(raw_sub)
            label = text_of(sub.get("label")) or _letter(j)
            sub_title = text_of(sub.get("title"))
            story.append(Paragraph(esc(sub_title), sub_style, bulletText=f"({label})"))
            if text_of(sub.get("body")):
                story.extend(rich_text(_condensed(sub.get("body"), max_body), styles, left_indent=18))


def action_facts(action: dict, *, with_completion: bool = True) -> str:
    """The muted one-line summary under an action heading."""
    names = ", ".join(n for n in (person(a) for a in as_list(action.get("assignees"))) if n)
    facts = [
        T("meeting.action_status", status=text_of(action.get("status_label")) or "Open"),
        T("meeting.action_assigned", names=names or DASH),
    ]
    if parse_when(action.get("due_iso")) or text_of(action.get("due_iso")):
        facts.append(T("meeting.action_due", when=fmt_date(action.get("due_iso"))))
    if with_completion and (parse_when(action.get("completed_iso"))
                            or text_of(action.get("completed_iso"))):
        facts.append(T("meeting.action_completed", when=fmt_date(action.get("completed_iso"))))
    if text_of(action.get("from_decision")):
        facts.append(T("meeting.action_from_decision", title=text_of(action.get("from_decision"))))
    return f"  {DASH}  ".join(facts)


def _notes_text(value: Any) -> str:
    """Progress notes as prose: a string as-is, a list one paragraph per note."""
    if isinstance(value, (list, tuple)):
        parts = []
        for entry in value:
            if isinstance(entry, dict):
                text = text_of(entry.get("text") or entry.get("note"))
                when = entry.get("at") or entry.get("date_iso")
                if text and parse_when(when):
                    text = f"{fmt_date(when)}: {text}"
            else:
                text = text_of(entry)
            if text:
                parts.append(text)
        return "\n\n".join(parts)
    return text_of(value)


def _also_on(action: dict) -> str:
    """"Title (raised there) — dd/mm/yyyy; …" for the other sittings.

    An entry may say ``raised: true``. When none of them carries the flag and the
    action was carried forward, the earliest sitting is the one that raised it —
    a carried action was by definition raised before this meeting.
    """
    entries = [as_dict(e) for e in as_list(action.get("also_on"))]
    entries = [e for e in entries if text_of(e.get("title")) or text_of(e.get("date_iso"))]
    if not entries:
        return ""
    flagged = any("raised" in e for e in entries)
    raised_index = None
    if not flagged and action.get("carried_forward"):
        def sort_key(pair):
            when = parse_when(pair[1].get("date_iso"))
            if isinstance(when, datetime):
                return (0, when.date(), pair[0])
            if isinstance(when, date):
                return (0, when, pair[0])
            return (1, date.max, pair[0])
        raised_index = min(enumerate(entries), key=sort_key)[0]
    out = []
    for i, entry in enumerate(entries):
        label = text_of(entry.get("title")) or DASH
        if (entry.get("raised") if flagged else i == raised_index):
            label += f" ({T('meeting.action_raised_here')})"
        if text_of(entry.get("date_iso")):
            label += f" {DASH} {fmt_date(entry.get('date_iso'))}"
        out.append(label)
    return "; ".join(out)


def action_block(action: dict, n: int, styles, *, lead: list = (),
                 full: bool = True) -> list:
    """One follow-up action as a heading, prose and muted facts."""
    block = list(lead)
    number = text_of(action.get("number")) or str(n)
    heading = f"{number}. {text_of(action.get('title')) or DASH}"
    if action.get("carried_forward"):
        heading += f"  {DASH} {T('meeting.action_carried')}"
    block.append(p(heading, styles["SubsubTitle"]))
    if text_of(action.get("description")):
        block.extend(rich_text(action.get("description"), styles))
    block.append(p(action_facts(action, with_completion=full), styles["SmallMuted"]))
    if not full:
        return block

    if text_of(action.get("reported_note")):
        block.append(p(T("meeting.action_reported"), styles["SmallMuted"]))
        block.extend(rich_text(action.get("reported_note"), styles))
    progress = _notes_text(action.get("progress_notes"))
    if progress:
        block.append(p(T("meeting.action_progress"), styles["SmallMuted"]))
        block.extend(rich_text(progress, styles))
    if text_of(action.get("completion_note")):
        block.append(p(T("meeting.action_completion"), styles["SmallMuted"]))
        block.extend(rich_text(action.get("completion_note"), styles))
    elsewhere = _also_on(action)
    if elsewhere:
        block.append(p(T("meeting.action_also_on", meetings=elsewhere), styles["SmallMuted"]))
    return block


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def render_meeting_report(export: dict) -> bytes:
    """Render the meeting report PDF for a Meet++ export (contract §7)."""
    export = as_dict(export)
    org = as_dict(export.get("org"))
    meeting = as_dict(export.get("meeting"))
    formal = is_formal(meeting)
    styles = build_styles()

    plain_title = T("meeting.title_ga" if is_general_assembly(meeting) else "meeting.title")
    title = meeting_title(meeting)
    when = fmt_datetime(meeting.get("date_iso"))
    meta = header_meta(title, meeting.get("date_iso"))

    buf = io.BytesIO()
    doc = document(buf, text_of(org.get("name")), plain_title, T("meeting.subject"))
    story: list = []
    cover(story, styles, org, plain_title, title)

    _facts(story, meeting, when, styles)
    _attendance(story, as_dict(export.get("attendance")), styles)

    agenda = [as_dict(a) for a in as_list(export.get("agenda"))]
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(T("meeting.agenda"), styles["SubsectionTitle"]))
    if agenda:
        agenda_points(story, agenda, styles, max_body=AGENDA_BODY_MAX)
    else:
        story.append(Paragraph(T("meeting.no_agenda"), styles["BodyText2"]))

    story.append(Spacer(1, 5 * mm))
    _decisions(story, [as_dict(d) for d in as_list(export.get("decisions"))], styles, formal)

    story.append(Spacer(1, 4 * mm))
    _actions(story, [as_dict(a) for a in as_list(export.get("actions"))], styles)

    story.append(Spacer(1, 4 * mm))
    _papers(story, [as_dict(x) for x in as_list(export.get("papers"))], agenda, styles)

    story.append(Spacer(1, 5 * mm))
    _minutes(story, as_dict(export.get("minutes")), styles)

    if formal:
        # The approval line travels with the last block of the minutes rather
        # than standing alone on a page of its own.
        tail: list = []
        signature_block(tail, styles)
        last = story.pop() if story else None
        story.extend(keep([last, *tail]) if last is not None else tail)

    canvasmaker = paged_canvas(text_of(org.get("name")), plain_title, meta,
                               export.get("generated_at_iso"), footer_segments(org))
    return render(doc, story, buf, canvasmaker)


def _facts(story: list, meeting: dict, when: str, styles) -> None:
    title = meeting_title(meeting)
    series = text_of(meeting.get("series_title"))
    facts = [
        (T("meeting.datetime"), when),
        (T("meeting.location"), text_of(meeting.get("location")) or DASH),
        (T("meeting.type"), text_of(meeting.get("type_label")) or DASH),
    ]
    # Only when it says something the title above does not.
    if series and series != title:
        facts.append((T("meeting.series"), series))
    facts.append((T("col.status"), text_of(meeting.get("status_label")) or DASH))
    facts.append((T("meeting.convened_at"), fmt_datetime(meeting.get("convened_at_iso"))))
    if parse_when(meeting.get("adjourned_at_iso")):
        facts.append((T("meeting.adjourned_at"), fmt_datetime(meeting.get("adjourned_at_iso"))))
    story.append(table(["", ""], facts, styles, [0.3, 0.7]))
    if text_of(meeting.get("quorum_note")):
        story.append(p(T("meeting.quorum", note=text_of(meeting.get("quorum_note"))),
                       styles["BodyText2"]))


def _attendance(story: list, attendance: dict, styles) -> None:
    rows_in = [as_dict(r) for r in as_list(attendance.get("rows"))]
    summary_in = as_dict(attendance.get("summary"))

    counts = {"present": 0, "represented": 0, "absent": 0, "excused": 0, "not_registered": 0}
    computed = {k: 0 for k in counts}
    rows = []
    excused_names: list[str] = []
    for row in rows_in:
        status = text_of(row.get("status_label"))
        key = status.lower().replace(" ", "_")
        if key in computed:
            computed[key] += 1
        # An excused member did not attend, so they get no row in the register
        # of who did; they are named on the line under the summary instead.
        if key == "excused":
            excused_names.append(text_of(row.get("name")) or DASH)
            continue
        rows.append((
            text_of(row.get("name")) or DASH,
            username(row.get("username")),
            status or DASH,
            person(row.get("represented_by")) or DASH,
            text_of(row.get("mandate")) or DASH,
        ))
    for key in counts:
        value = summary_in.get(key)
        if key == "not_registered" and value is None:
            value = summary_in.get("expected")
        counts[key] = value if value is not None else computed[key]

    summary = T("meeting.attendance_summary",
                present=count(counts["present"]), represented=count(counts["represented"]),
                absent=count(counts["absent"]), excused=count(counts["excused"]),
                expected=count(counts["not_registered"]))

    story.append(Spacer(1, 5 * mm))
    lead = [Paragraph(T("meeting.attendance"), styles["SubsectionTitle"]),
            p(summary, styles["BodyText2"])]
    if excused_names:
        lead.append(p(T("meeting.excused_names", names=", ".join(excused_names)),
                      styles["BodyText2"]))
    if rows:
        story.extend(keep_with_table(
            lead,
            [T("col.member"), T("col.username"), T("col.status"),
             T("meeting.represented_by"), T("meeting.proxy_mandate")],
            rows, styles, [0.24, 0.18, 0.16, 0.22, 0.2]))
    else:
        story.extend(keep([*lead, Paragraph(T("meeting.no_members"), styles["BodyText2"])]))


def _decisions(story: list, decisions: list, styles, formal: bool) -> None:
    decisions_title = Paragraph(T("meeting.decisions"), styles["SubsectionTitle"])
    if not decisions:
        story.extend(keep([decisions_title,
                           Paragraph(T("meeting.no_decisions"), styles["BodyText2"])]))
    for n, decision in enumerate(decisions, 1):
        # The heading rides inside the first block: the heading styles carry
        # keepWithNext and reportlab will not reach past a KeepTogether.
        block: list = [decisions_title] if n == 1 else []
        number = text_of(decision.get("number")) or str(n)
        title = text_of(decision.get("title")) or DASH
        block.append(p(f"{number}. {title}", styles["SubsubTitle"]))
        text_blocks = [rich_text(decision.get(k), styles)
                       for k in ("resolution", "how_taken") if text_of(decision.get(k))]
        for i, flow in enumerate(text_blocks):
            if i:
                block.append(Spacer(1, 2 * mm))
            block.extend(flow)
        status_line = T("meeting.decision_status",
                        status=text_of(decision.get("status_label")) or "Proposed")
        if parse_when(decision.get("decided_at_iso")):
            status_line += (f" {DASH} "
                            + T("meeting.decided_at",
                                when=fmt_datetime(decision.get("decided_at_iso"))))
        block.append(p(status_line, styles["SmallMuted"]))

        vote = decision.get("vote")
        if not isinstance(vote, dict):
            # Informal meetings record no votes; a formal one says so.
            if formal:
                block.append(Paragraph(T("vote.none"), styles["BodyText2"]))
            story.extend(keep(block))
            story.append(Spacer(1, 3 * mm))
            continue

        block.append(table(
            [T("vote.question"), T("vote.for"), T("vote.against"),
             T("vote.abstain"), T("vote.result")],
            [(
                text_of(vote.get("question")) or title,
                count(vote.get("for")), count(vote.get("against")),
                count(vote.get("abstain")),
                text_of(vote.get("result_label")) or DASH,
            )],
            styles, [0.4, 0.13, 0.13, 0.14, 0.2], right_align=(1, 2, 3),
        ))

        arithmetic = []
        if text_of(vote.get("voting_body_label")):
            arithmetic.append((T("vote.body"), text_of(vote.get("voting_body_label"))))
        if text_of(vote.get("majority_label")):
            arithmetic.append((T("vote.majority"), text_of(vote.get("majority_label"))))
        if vote.get("eligible") is not None:
            arithmetic.append((T("vote.eligible"), count(vote.get("eligible"))))
        if text_of(vote.get("basis_label")):
            arithmetic.append((T("vote.basis"), text_of(vote.get("basis_label"))))
        if vote.get("present_or_represented") is not None:
            arithmetic.append((T("vote.present"), count(vote.get("present_or_represented"))))
        if vote.get("quorum_required") is not None:
            arithmetic.append((T("vote.quorum_required"), count(vote.get("quorum_required"))))
        if vote.get("quorum_met") is not None:
            arithmetic.append((T("vote.quorum_met"), yes_no(vote.get("quorum_met"))))
        if arithmetic:
            block.append(Spacer(1, 2 * mm))
            block.append(table(["", ""], arithmetic, styles, [0.35, 0.65]))

        if text_of(vote.get("outcome_sentence")):
            block.append(Spacer(1, 2 * mm))
            block.append(p(text_of(vote.get("outcome_sentence")), styles["BodyText2"]))

        ballots = [as_dict(b) for b in as_list(vote.get("ballots"))]
        if ballots:
            block.append(Spacer(1, 2 * mm))
            story.extend(keep_with_table(
                block,
                [T("col.member"), T("vote.question"), T("vote.cast_by"), T("vote.proxy")],
                [(
                    text_of(b.get("name")) or DASH,
                    text_of(b.get("vote_label")) or DASH,
                    person(b.get("cast_by")) or DASH,
                    yes_no(b.get("proxy")),
                ) for b in ballots],
                styles, [0.4, 0.18, 0.28, 0.14],
            ))
        else:
            story.extend(keep(block))
        story.append(Spacer(1, 4 * mm))


def _actions(story: list, actions: list, styles) -> None:
    actions_title = Paragraph(T("meeting.actions"), styles["SubsectionTitle"])
    if not actions:
        story.extend(keep([actions_title,
                           Paragraph(T("meeting.no_actions"), styles["BodyText2"])]))
    for n, action in enumerate(actions, 1):
        block = action_block(action, n, styles, lead=[actions_title] if n == 1 else [])
        story.extend(keep(block))
        story.append(Spacer(1, 3 * mm))


def _papers(story: list, papers: list, agenda: list, styles) -> None:
    title = Paragraph(T("meeting.attachments"), styles["SubsectionTitle"])
    if not papers:
        story.extend(keep([title, Paragraph(T("meeting.no_attachments"), styles["BodyText2"])]))
        return
    agenda_titles = {}
    for idx, item in enumerate(agenda, 1):
        number = text_of(item.get("number")) or str(idx)
        item_title = text_of(item.get("title"))
        agenda_titles[number] = f"{number}. {item_title}" if item_title else number
    rows = []
    for paper in papers:
        number = text_of(paper.get("agenda_number"))
        rows.append((
            text_of(paper.get("title")) or DASH,
            text_of(paper.get("file")) or DASH,
            agenda_titles.get(number, number) if number else DASH,
        ))
    story.extend(keep_with_table(
        [title],
        [T("meeting.attachment_title"), T("meeting.attachment_file"),
         T("meeting.attachment_item")],
        rows, styles, [0.38, 0.34, 0.28],
    ))


def _minutes(story: list, minutes: dict, styles) -> None:
    story.append(Paragraph(T("meeting.minutes"), styles["SubsectionTitle"]))
    markdown = minutes.get("markdown")
    if not text_of(markdown):
        story.append(Paragraph(T("meeting.no_minutes"), styles["BodyText2"]))
        return
    version = minutes.get("version")
    try:
        version_no = int(version) if version is not None and version != "" else 0
    except (TypeError, ValueError):
        version_no = 0
    # Optional: the export carries it once the minutes have been approved.
    if parse_when(minutes.get("approved_at_iso")):
        story.append(p(T("meeting.approved", when=fmt_datetime(minutes.get("approved_at_iso"))),
                       styles["SmallMuted"]))
    if parse_when(minutes.get("saved_at_iso")):
        saved = fmt_datetime(minutes.get("saved_at_iso"))
        story.append(p(
            T("meeting.minutes_saved_version", when=saved, no=version_no) if version_no
            else T("meeting.minutes_saved", when=saved),
            styles["SmallMuted"]))
    if version_no > 1:
        story.append(p(T("meeting.minutes_version_count", count=version_no),
                       styles["SmallMuted"]))
    story.extend(minutes_to_flowables(markdown, styles))

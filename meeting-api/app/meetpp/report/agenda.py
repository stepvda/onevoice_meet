"""The next-meeting agenda: the convocation's companion to the meeting report.

Same furniture as the report (identity block, running header, identity footer,
PROVISIONAL stamp), then a key/value table for ``export["next_meeting"]``, the
numbered ``export["next_agenda"]``, and the follow-up actions that are still
open and therefore come back to the next sitting.
"""
from __future__ import annotations

import io

from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer

from .common import (
    DASH, as_dict, as_list, cover, document, fmt_datetime, footer_segments, keep,
    paged_canvas, parse_when, render, table, text_of,
)
from .meeting import action_block, agenda_points, header_meta, is_open_action
from .strings import T
from .styles import build_styles


def render_agenda(export: dict) -> bytes:
    """Render the next-meeting agenda PDF for a Meet++ export (contract §7)."""
    export = as_dict(export)
    org = as_dict(export.get("org"))
    meeting = as_dict(export.get("meeting"))
    next_meeting = as_dict(export.get("next_meeting"))
    styles = build_styles()

    plain_title = T("agenda.title")
    series = text_of(meeting.get("series_title")) or text_of(meeting.get("title"))
    next_when = next_meeting.get("date_iso")
    when = fmt_datetime(next_when) if parse_when(next_when) else (
        text_of(next_when) or T("agenda.to_be_confirmed"))
    meta = header_meta(series, None, when)

    buf = io.BytesIO()
    doc = document(buf, text_of(org.get("name")), plain_title, T("agenda.subject"))
    story: list = []
    cover(story, styles, org, plain_title, series)

    facts = [
        (T("meeting.datetime"), when),
        (T("meeting.location"),
         text_of(next_meeting.get("location")) or text_of(meeting.get("location")) or DASH),
        (T("meeting.type"), text_of(meeting.get("type_label")) or DASH),
    ]
    if parse_when(meeting.get("date_iso")):
        previous = fmt_datetime(meeting.get("date_iso"))
        title = text_of(meeting.get("title"))
        facts.append((T("agenda.previous_meeting"),
                      f"{title} {DASH} {previous}" if title and title != series else previous))
    story.append(table(["", ""], facts, styles, [0.3, 0.7]))

    items = [as_dict(i) for i in as_list(export.get("next_agenda"))]
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(T("meeting.agenda"), styles["SubsectionTitle"]))
    if items:
        agenda_points(story, items, styles)
    else:
        story.append(Paragraph(T("meeting.no_agenda"), styles["BodyText2"]))

    open_actions = [a for a in (as_dict(x) for x in as_list(export.get("actions")))
                    if is_open_action(a)]
    story.append(Spacer(1, 5 * mm))
    heading = Paragraph(T("agenda.open_actions"), styles["SubsectionTitle"])
    if not open_actions:
        story.extend(keep([heading,
                           Paragraph(T("agenda.no_open_actions"), styles["BodyText2"])]))
    for n, action in enumerate(open_actions, 1):
        # Renumbered for this document; the carried-forward mark belongs to the
        # report it was carried into, so it is not repeated here.
        block = action_block({**action, "number": None, "carried_forward": False}, n,
                             styles, lead=[heading] if n == 1 else [], full=False)
        story.extend(keep(block))
        story.append(Spacer(1, 3 * mm))

    canvasmaker = paged_canvas(text_of(org.get("name")), plain_title, meta,
                               export.get("generated_at_iso"), footer_segments(org))
    return render(doc, story, buf, canvasmaker)

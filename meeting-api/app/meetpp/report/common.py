"""Page furniture and layout helpers, ported from the OM ``org_pdf_service``.

What changed in the port, and why:

* The association identity comes from ``export["org"]`` instead of module
  constants, so every line of the article-1 block is optional: an empty field is
  left out rather than printed as "Enterprise number: ".
* "Generated …" is the export's ``generated_at_iso`` instead of ``now()``, and
  the PDF's own CreationDate/ID are pinned to it, so the renderer is a pure
  function of its input (same export in, same bytes out).
* Nothing here imports the OM codebase or touches a database.
"""
from __future__ import annotations

import io
import os
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, TimeStamp
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    HRFlowable, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

from .strings import T
from .styles import (
    BOTTOM_MARGIN, COLOR_BRAND, COLOR_DANGER, COLOR_GRID, COLOR_HEADER_BG,
    COLOR_MUTED, COLOR_ROW_ALT, FRAME_HEIGHT, PAGE_MARGIN, TOP_MARGIN, USABLE_WIDTH,
)

DASH = "—"
# Middle dot, as the OM identity block separates particulars.
SEP = " · "

# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

# Characters a Helvetica/WinAnsi page cannot draw and ReportLab's Symbol /
# ZapfDingbats fallback does not cover either: they would print as black boxes.
# Folded to the nearest WinAnsi glyph. Arrows, >=, <= and check marks are left
# alone — the fallback fonts do carry those.
_FOLD = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "−": "-",
    "―": "—",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "​": None, "‌": None, "‍": None, "⁠": None,
    "﻿": None, "︎": None, "️": None,
    "′": "'", "″": '"',
})


def clean(value: Any) -> str:
    """``value`` as display text: None is empty, odd whitespace folded."""
    if value is None:
        return ""
    return str(value).translate(_FOLD)


def esc(value: Any) -> str:
    """Escape untrusted text for ReportLab's Paragraph mini-XML.

    A name or a minute containing '&' or '<' raises at build time (or vanishes
    as an unknown tag) unless it is escaped.
    """
    return (clean(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def esc_attr(value: Any) -> str:
    """Escape for a mini-XML attribute value (double-quoted)."""
    return esc(value).replace('"', "&quot;")


def p(text: Any, style) -> Paragraph:
    """Paragraph from an untrusted string (the OM ``_p``)."""
    return Paragraph(esc(text), style)


def text_of(value: Any) -> str:
    """A stripped string, or "" for None / non-strings that are empty."""
    return clean(value).strip()


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_when(value: Any):
    """An ISO-8601 string (or date/datetime) as an aware UTC datetime, a date,
    or None when absent or unreadable.

    The wire format is ``…Z`` or ``…+00:00`` (contract preamble); a naive value
    is taken as UTC, as the OM ``_as_utc`` does for SQLite rows.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    if _DATE_ONLY_RE.match(s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_date(value: Any) -> str:
    """dd/mm/yyyy, "—" when absent, the raw text when unreadable."""
    when = parse_when(value)
    if when is None:
        return text_of(value) or DASH
    return when.strftime("%d/%m/%Y")


def fmt_datetime(value: Any) -> str:
    """dd/mm/yyyy hh:mm UTC (date only when the value carries no time)."""
    when = parse_when(value)
    if when is None:
        return text_of(value) or DASH
    if not isinstance(when, datetime):
        return when.strftime("%d/%m/%Y")
    return when.strftime("%d/%m/%Y %H:%M") + " UTC"


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def yes_no(value: Any) -> str:
    return T("doc.yes" if value else "doc.no")


def count(value: Any) -> str:
    """A tally cell: integers print as integers, None as a dash."""
    if value is None or value == "":
        return DASH
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return clean(value)


# ---------------------------------------------------------------------------
# Identity (article 1 particulars) from export["org"]
# ---------------------------------------------------------------------------

_FIELD_NAMES = {
    "enterprise": "enterprise number",
    "enterprise_number": "enterprise number",
    "email": "email address",
    "iban": "bank account number",
    "rpr": "register of legal persons",
    "seat": "registered seat",
    "website": "website",
}


def rpr_line(org: dict) -> str:
    """The register-of-legal-persons mention; "RPR" kept as article 1 wants it."""
    rpr = text_of(org.get("rpr"))
    if not rpr:
        return ""
    return rpr if rpr.upper().startswith("RPR") else f"RPR {rpr}"


def identity_lines(org: dict) -> list[str]:
    """The article 1 block under the name, one string per line; empty fields
    are omitted rather than printed with a dangling label."""
    lines: list[str] = []
    tagline = text_of(org.get("tagline"))
    if tagline:
        lines.append(tagline)
    seat = text_of(org.get("seat"))
    if seat:
        lines.append(T("identity.seat", seat=seat))
    enterprise = text_of(org.get("enterprise"))
    if enterprise:
        lines.append(T("identity.enterprise", number=enterprise))
    rpr = rpr_line(org)
    if rpr:
        lines.append(rpr)
    contact = []
    email = text_of(org.get("email"))
    if email:
        contact.append(T("identity.email", email=email))
    website = text_of(org.get("website"))
    if website:
        contact.append(T("identity.website", website=website))
    if contact:
        lines.append(SEP.join(contact))
    iban = text_of(org.get("iban"))
    if iban:
        lines.append(T("identity.iban", iban=iban))
    return lines


def footer_segments(org: dict) -> list[str]:
    """The particulars repeated in every page footer, split for wrapping."""
    segments = [
        text_of(org.get("name")),
        text_of(org.get("seat")),
        T("identity.enterprise_footer", number=text_of(org.get("enterprise")))
        if text_of(org.get("enterprise")) else "",
        rpr_line(org),
        text_of(org.get("email")),
        text_of(org.get("website")),
    ]
    return [s for s in segments if s]


def placeholder_warning(org: dict) -> Optional[str]:
    """The PROVISIONAL stamp text, or None when the identity is final.

    ``org.placeholders`` is normally a bool; a list of field names is also
    accepted and then quoted in the stamp, as the OM stamp does.
    """
    flag = org.get("placeholders")
    if not flag:
        return None
    if isinstance(flag, (list, tuple, set)):
        names = []
        for field in flag:
            label = _FIELD_NAMES.get(str(field), str(field).replace("_", " "))
            if label not in names:
                names.append(label)
        if names:
            return T("identity.placeholder_listed", listed=", ".join(names))
    return T("identity.placeholder")


# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------

def _wrap_segments(measure, segments: Sequence[str], sep: str,
                   max_width: float) -> list[str]:
    """Greedily pack ``segments`` into lines no wider than ``max_width``.

    Broken between particulars rather than by character so an address or an
    enterprise number is never split across two lines.
    """
    lines: list[str] = []
    current = ""
    for segment in segments:
        candidate = f"{current}{sep}{segment}" if current else segment
        if current and measure(candidate) > max_width:
            lines.append(current)
            current = segment
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _timestamp(when: Optional[datetime]) -> Optional[TimeStamp]:
    """A ReportLab TimeStamp pinned to ``when`` (UTC), for CreationDate."""
    if when is None:
        return None
    ts = TimeStamp(invariant=1)
    tt = when.utctimetuple()
    ts.t = float(when.timestamp())
    ts.lt = time.gmtime(int(ts.t))
    ts.YMDhms = tuple(tt)[:6]
    ts.dhh = 0
    ts.dmm = 0
    ts.tzname = "UTC"
    return ts


def paged_canvas(org_name: str, title: str, meta: str, generated_iso: Any,
                 identity_segments: Sequence[str], pagesize=A4):
    """Canvas class that stamps the header, the identity footer and
    'Page N of M' (the OM ``_paged_canvas``).

    The total page count is only known once the story has been laid out, so
    pages are buffered and the chrome is drawn on the second pass in save().
    """
    page_width, page_height = pagesize
    usable_width = page_width - 2 * PAGE_MARGIN
    generated_at = parse_when(generated_iso)
    if isinstance(generated_at, datetime):
        generated = T("doc.generated", when=generated_at.strftime("%d/%m/%Y %H:%M UTC"))
    elif generated_at is not None:
        generated = T("doc.generated", when=generated_at.strftime("%d/%m/%Y"))
    else:
        generated = ""
    stamp = _timestamp(generated_at if isinstance(generated_at, datetime) else None)
    org_name = clean(org_name)
    title = clean(title)
    meta = clean(meta)
    identity_segments = [clean(s) for s in identity_segments]

    class _PagedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if stamp is not None:
                # Pins the PDF's CreationDate/ModDate to the export's
                # generated_at; with invariant=1 the file ID is fixed too.
                self._doc._timeStamp = stamp
            self._buffered_pages = []

        def showPage(self):
            self._buffered_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._buffered_pages)
            for state in self._buffered_pages:
                self.__dict__.update(state)
                self._draw_chrome(total)
                super().showPage()
            super().save()

        def _draw_chrome(self, total: int):
            self.saveState()
            self.setFont("Helvetica-Bold", 8)
            self.setFillColor(COLOR_HEADER_BG)
            self.drawString(PAGE_MARGIN, page_height - 14 * mm, org_name)
            self.setFont("Helvetica", 8)
            self.setFillColor(COLOR_MUTED)
            self.drawRightString(page_width - PAGE_MARGIN, page_height - 14 * mm, title)
            if meta:
                self.drawString(PAGE_MARGIN, page_height - 18 * mm, meta)
            self.setStrokeColor(COLOR_BRAND)
            self.setLineWidth(0.5)
            self.line(PAGE_MARGIN, page_height - 20 * mm,
                      page_width - PAGE_MARGIN, page_height - 20 * mm)

            self.setFont("Helvetica", 7)
            self.setFillColor(COLOR_MUTED)
            if generated:
                self.drawString(PAGE_MARGIN, 6 * mm, generated)
            self.drawRightString(
                page_width - PAGE_MARGIN, 6 * mm,
                T("doc.page", n=self._pageNumber, total=total),
            )

            # Identity particulars, stacked upward from just above the
            # generated/page line.
            identity_lines_ = _wrap_segments(
                lambda s: self.stringWidth(s, "Helvetica", 7),
                identity_segments, SEP, usable_width,
            )
            y = 9.5 * mm
            for line in reversed(identity_lines_):
                self.drawString(PAGE_MARGIN, y, line)
                y += 3 * mm
            self.setStrokeColor(COLOR_MUTED)
            self.setLineWidth(0.25)
            self.line(PAGE_MARGIN, y - 1 * mm, page_width - PAGE_MARGIN, y - 1 * mm)
            self.restoreState()

    return _PagedCanvas


def document(buf: io.BytesIO, org_name: str, title: str, subject: str,
             pagesize=A4) -> SimpleDocTemplate:
    """The A4 frame every report is laid out in (the OM ``_document``)."""
    org_name = clean(org_name)
    return SimpleDocTemplate(
        buf, pagesize=pagesize,
        leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN,
        topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
        title=f"{org_name} {DASH} {title}" if org_name else title,
        author=org_name,
        subject=subject,
        lang="en",
        # Deterministic output: no wall-clock timestamp or random file ID.
        invariant=1,
    )


def render(doc: SimpleDocTemplate, story: list, buf: io.BytesIO, canvasmaker) -> bytes:
    doc.build(story, canvasmaker=canvasmaker)
    return buf.getvalue()


def placeholder_banner(styles, text: str, *, width: float = USABLE_WIDTH) -> Table:
    """The 'do not file this' stamp, as a bordered, filled table."""
    warning = ParagraphStyle(
        "OrgPlaceholderWarning", parent=styles["BodyText2"],
        fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=COLOR_DANGER,
    )
    table = Table([[p(text, warning)]], colWidths=[width])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fef2f2")),
        ("BOX", (0, 0), (-1, -1), 1, COLOR_DANGER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _logo(path: Any) -> Optional[Image]:
    """The logo flowable, or None when there is none or it cannot be read.

    Probed here rather than left to build time: a broken path would otherwise
    abort the whole report on the first page.
    """
    if not path:
        return None
    try:
        real = os.path.realpath(str(path))
        if not os.path.isfile(real):
            return None
        ImageReader(real).getSize()
    except Exception:  # noqa: BLE001 — any unreadable logo is simply skipped
        return None
    logo = Image(real, width=28 * mm, height=28 * mm)
    logo.hAlign = "CENTER"
    return logo


def cover(story: list, styles, org: dict, title: str, subtitle: str, *,
          width: float = USABLE_WIDTH) -> None:
    """The cover block: logo, name, the identity particulars, the title
    (the OM ``_cover``)."""
    logo = _logo(org.get("logo_path"))
    if logo is not None:
        story.append(logo)
        story.append(Spacer(1, 4 * mm))
    name = text_of(org.get("name"))
    if name:
        story.append(p(name, styles["CoverTitle"]))

    identity_style = ParagraphStyle(
        "OrgCoverIdentity", parent=styles["CoverSubtitle"],
        fontSize=9, leading=12, spaceAfter=0,
    )
    for line in identity_lines(org):
        story.append(p(line, identity_style))
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width="100%", thickness=1, color=COLOR_BRAND,
                            spaceBefore=2, spaceAfter=8))
    warning = placeholder_warning(org)
    if warning:
        story.append(placeholder_banner(styles, warning, width=width))
        story.append(Spacer(1, 4 * mm))
    story.append(p(title, styles["SectionTitle"]))
    if subtitle:
        story.append(p(subtitle, styles["BodyText2"]))
    story.append(Spacer(1, 4 * mm))


def signature_block(story: list, styles) -> None:
    """The approval line signed on paper (catalogue string is mini-XML)."""
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(T("doc.signature"), styles["SmallMuted"]))


# ---------------------------------------------------------------------------
# Free text that carries its own structure
# ---------------------------------------------------------------------------

_BULLET_RE = re.compile(r"^(\s*)([-*•–]|\d{1,3}[.)]|[a-z][.)])\s+", re.I)


def rich_text(text: Any, styles, *, style_name: str = "BodyText2",
              left_indent: float = 0) -> list:
    """Prose that may carry its own line breaks, bullets or numbering
    (the OM ``_rich_text``).

    A blank line starts a paragraph, a "- " or "1." line becomes a real indented
    list item (the typed marker is kept, never renumbered), and the lines of a
    paragraph are joined. Left-aligned, not justified.
    """
    body = ParagraphStyle(
        f"OrgProse_{style_name}_{left_indent}", parent=styles[style_name],
        alignment=TA_LEFT, leftIndent=left_indent,
    )
    item = ParagraphStyle(
        f"OrgListItem_{style_name}_{left_indent}", parent=body,
        leftIndent=left_indent + 10, bulletIndent=left_indent, spaceAfter=2,
        alignment=TA_LEFT,
    )
    out: list = []
    for raw_block in clean(text).replace("\r\n", "\n").split("\n\n"):
        lines = [ln for ln in raw_block.split("\n") if ln.strip()]
        if not lines:
            continue
        run: list[str] = []

        def flush():
            if run:
                out.append(p(" ".join(run), body))
                run.clear()

        for line in lines:
            marker = _BULLET_RE.match(line)
            if marker:
                flush()
                label = marker.group(2)
                rest = line[marker.end():].strip()
                out.append(Paragraph(
                    esc(rest), item,
                    bulletText="•" if label in "-*•–" else label,
                ))
            else:
                run.append(line.strip())
        flush()
        out.append(Spacer(1, 2 * mm))
    return out[:-1] if out else []


# ---------------------------------------------------------------------------
# Tables and keep-together
# ---------------------------------------------------------------------------

def table_style_commands(emphasised: Iterable[int] = ()) -> list:
    """The report table look: navy header, hairline grid, striped rows."""
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_HEADER_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, COLOR_GRID),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, COLOR_ROW_ALT]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for r in sorted(emphasised):
        cmds.append(("BACKGROUND", (0, r + 1), (-1, r + 1), colors.HexColor("#e5e7eb")))
    return cmds


#: A row taller than this share of the text frame may be split across pages.
_TALL_ROW = 0.75
#: The smallest piece of a split row, in points (about three 8 pt lines).
_MIN_ROW_PIECE = 30


def allow_row_split(t: Table, width: float) -> Table:
    """Let a table split *inside* a row when one of its rows is too tall.

    ReportLab only breaks tables between rows, so a single cell holding more
    text than a page (a pasted resolution, a long quoted passage) raises
    LayoutError and the whole report fails. Tables with ordinary rows are left
    exactly as they were — in-row splitting is enabled only where a row is
    close to a page high, and even then ReportLab still tries a row boundary
    first.
    """
    try:
        t.wrap(width, FRAME_HEIGHT)
        heights = [h for h in (t._rowHeights or []) if h]
    except Exception:  # noqa: BLE001 — measuring is best effort
        return t
    if heights and max(heights) > FRAME_HEIGHT * _TALL_ROW:
        t.splitInRow = _MIN_ROW_PIECE
    return t


def table(headers: Sequence[str], rows: Sequence[Sequence], styles,
          widths: Sequence[float], right_align: Sequence[int] = (),
          emphasise_last: bool = False, *,
          total_width: Optional[float] = None) -> Table:
    """A report table (the OM ``_table``): header repeated on every page, no
    row cap, widths as fractions of the text frame."""
    cell = styles["TableCell"]
    cell_right = ParagraphStyle("OrgCellRight", parent=cell, alignment=TA_RIGHT)
    cell_bold = ParagraphStyle("OrgCellBold", parent=cell, fontName="Helvetica-Bold")
    cell_bold_right = ParagraphStyle("OrgCellBoldRight", parent=cell_bold, alignment=TA_RIGHT)

    emphasised = set()
    if emphasise_last and rows:
        emphasised.add(len(rows) - 1)

    data = [[p(h, styles["TableHeader"]) for h in headers]]
    for r, row in enumerate(rows):
        bold = r in emphasised
        data.append([
            p(value,
              (cell_bold_right if bold else cell_right) if i in right_align
              else (cell_bold if bold else cell))
            for i, value in enumerate(row)
        ])

    frame_width = USABLE_WIDTH if total_width is None else total_width
    col_widths = [w * frame_width for w in widths]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle(table_style_commands(emphasised)))
    return allow_row_split(t, frame_width)


def fits_page(flowables: Sequence) -> bool:
    """Whether ``flowables`` could stand together on one page (the OM
    ``_fits_page``): measured with the same space-merging KeepTogether uses."""
    total = 0.0
    space_after = 0.0
    at_top = True
    for flowable in flowables:
        width, height = flowable.wrap(USABLE_WIDTH, FRAME_HEIGHT)
        if width <= 0 or height <= 0:
            continue
        total += height
        if not at_top:
            total += max(flowable.getSpaceBefore() - space_after, 0)
        at_top = False
        space_after = flowable.getSpaceAfter()
        total += space_after
    return total - space_after <= FRAME_HEIGHT


def keep(flowables: Sequence) -> list:
    """One unbreakable block, unless it cannot fit on a page (the OM ``_keep``)."""
    return [KeepTogether(list(flowables))] if fits_page(flowables) else list(flowables)


def keep_with_table(lead: Sequence, headers: Sequence[str], rows: Sequence[Sequence],
                    styles, widths: Sequence[float], *,
                    right_align: Sequence[int] = (), emphasise_last: bool = False,
                    anchor_rows: int = 4) -> list:
    """A heading — and whatever else introduces a table — bound to its first
    rows (the OM ``_keep_with_table``)."""
    lead = list(lead)
    tbl = table(headers, rows, styles, widths,
                right_align=right_align, emphasise_last=emphasise_last)
    if len(rows) <= anchor_rows or fits_page([*lead, tbl]):
        return keep([*lead, tbl])

    anchor = table(headers, rows[:anchor_rows], styles, widths, right_align=right_align)
    if not fits_page([*lead, anchor]):
        return [*lead, tbl]
    rest = table(headers, rows[anchor_rows:], styles, widths,
                 right_align=right_align, emphasise_last=emphasise_last)
    return [KeepTogether([*lead, anchor]), rest]

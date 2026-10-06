"""Page geometry, brand colours and paragraph styles of the meeting report.

Ported verbatim from the OneVoice OM module (``pdf_toolkit._build_styles`` and
the constants at the top of ``org_pdf_service``) so a Meet++ report and an OM
report are the same document to the eye: same Helvetica/WinAnsi fonts, same
navy header band, same 20 mm side margins, same table furniture.

Fonts: no TTF is registered, so Helvetica/WinAnsi is all there is. FR/NL/EN
accents and the euro sign round-trip; characters outside WinAnsi are folded to
ASCII look-alikes by ``common.clean`` before they reach a Paragraph.
"""
from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm

# ---------------------------------------------------------------------------
# Brand colours (pdf_toolkit)
# ---------------------------------------------------------------------------

COLOR_BRAND = colors.HexColor("#1a56db")
COLOR_HEADER_BG = colors.HexColor("#1e3a5f")
COLOR_HEADER_TEXT = colors.white
COLOR_ROW_ALT = colors.HexColor("#f0f4ff")
COLOR_MUTED = colors.HexColor("#6b7280")
COLOR_ACCENT = colors.HexColor("#2563eb")
COLOR_DARK = colors.HexColor("#111827")
COLOR_SECTION_BG = colors.HexColor("#f8fafc")
COLOR_DANGER = colors.HexColor("#dc2626")
COLOR_GRID = colors.HexColor("#d1d5db")

# ---------------------------------------------------------------------------
# Page geometry (org_pdf_service)
# ---------------------------------------------------------------------------

PAGE_MARGIN = 20 * mm
# Clears the drawn header band.
TOP_MARGIN = 26 * mm
# A4 portrait usable width with 20 mm side margins (~515 pt). Every column
# budget is a fraction of this.
USABLE_WIDTH = A4[0] - 2 * PAGE_MARGIN
# The identity footer needs two 7 pt lines under the "Generated"/"Page N of M"
# line, so the bottom margin is 4 mm deeper than the header band.
BOTTOM_MARGIN = 24 * mm
# The text frame less the 6 pt a platypus Frame keeps as padding at each end.
FRAME_HEIGHT = A4[1] - TOP_MARGIN - BOTTOM_MARGIN - 2 * 6

MONO_FONT = "Courier"
MONO_SIZE = 8.5
MONO_LEADING = 11.0


def build_styles():
    """The OM report stylesheet (``pdf_toolkit._build_styles``), unchanged."""
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        "CoverTitle", parent=styles["Title"],
        fontSize=28, leading=34, textColor=COLOR_HEADER_BG,
        alignment=TA_CENTER, spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        "CoverSubtitle", parent=styles["Normal"],
        fontSize=14, leading=18, textColor=COLOR_MUTED,
        alignment=TA_CENTER, spaceAfter=20,
    ))
    styles.add(ParagraphStyle(
        "SectionTitle", parent=styles["Heading1"],
        fontSize=18, leading=22, textColor=COLOR_HEADER_BG,
        spaceBefore=16, spaceAfter=8,
        keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        "SubsectionTitle", parent=styles["Heading2"],
        fontSize=14, leading=18, textColor=COLOR_BRAND,
        spaceBefore=12, spaceAfter=6,
        keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        "SubsubTitle", parent=styles["Heading3"],
        fontSize=12, leading=15, textColor=COLOR_DARK,
        spaceBefore=8, spaceAfter=4,
        keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        "BodyText2", parent=styles["Normal"],
        fontSize=10, leading=14, textColor=COLOR_DARK,
        alignment=TA_JUSTIFY, spaceAfter=6,
        allowWidows=0, allowOrphans=0,
    ))
    styles.add(ParagraphStyle(
        "SmallMuted", parent=styles["Normal"],
        fontSize=8, leading=10, textColor=COLOR_MUTED,
        alignment=TA_LEFT,
    ))
    styles.add(ParagraphStyle(
        "TableHeader", parent=styles["Normal"],
        fontSize=8, leading=10, textColor=colors.white,
    ))
    styles.add(ParagraphStyle(
        "TableCell", parent=styles["Normal"],
        fontSize=8, leading=10, textColor=COLOR_DARK,
    ))
    return styles

"""Minutes Markdown → ReportLab flowables.

The OM report prints the minutes verbatim in Courier, which is right for the
hand-typed plain-text minutes it was written for and wrong for Meet++ minutes,
which are Markdown: "**Present:**" printed with its asterisks, and a GFM
"Record of voting" table reduced to rows of pipes. Here the Markdown is parsed
with markdown-it-py (CommonMark + GFM tables + strikethrough, raw HTML off so a
"<b>" typed in a minute stays text) and every block becomes a real flowable in
the report's own type scale:

* ``#``..``####`` headings, sized under the report's "Minutes" heading;
* paragraphs with **bold**, *italic*, ``code``, ~~strike~~ and links;
* bullet and ordered lists, nested, with the start number honoured;
* block quotes as a left-ruled, lightly shaded panel — the minutes'
  "> **RESOLVED:** that …" lines read as resolutions, not as body text;
* horizontal rules;
* GFM tables as report tables (navy header, hairline grid, striped rows,
  column alignment from the delimiter row, header repeated across pages);
* fenced and indented code, verbatim in Courier, soft-wrapped to the frame.

All text is escaped for ReportLab's mini-XML before any markup is added.
"""
from __future__ import annotations

from typing import Any, Optional

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    HRFlowable, Indenter, Paragraph, Preformatted, Spacer, Table, TableStyle,
)

from .common import allow_row_split, clean, esc, esc_attr, table_style_commands
from .styles import (
    COLOR_BRAND, COLOR_DARK, COLOR_GRID, COLOR_HEADER_BG, COLOR_MUTED,
    COLOR_SECTION_BG, MONO_FONT, MONO_LEADING, MONO_SIZE, USABLE_WIDTH,
    build_styles,
)

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_MD.enable(["table", "strikethrough"])

_SAFE_SCHEMES = ("http://", "https://", "mailto:")
_BULLETS = ("•", "–", "·")
_QUOTE_RULE = 2.0
_QUOTE_PAD = 8.0
_CELL_FONT = "Helvetica"
_CELL_SIZE = 8.0
_CELL_PAD = 8.0  # LEFTPADDING + RIGHTPADDING of table_style_commands()


def minutes_to_flowables(markdown: str, styles=None, *,
                         width: float = USABLE_WIDTH) -> list:
    """Render minutes Markdown as a list of ReportLab flowables.

    ``styles`` is the report stylesheet (``report.build_styles()``); any
    ReportLab stylesheet with a ``Normal`` style works, the Markdown styles are
    derived from ``BodyText2`` when it is there. ``width`` is the frame width
    the flowables will be laid out in.
    """
    if styles is None:
        styles = build_styles()
    text = clean(markdown).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []
    tree = SyntaxTreeNode(_MD.parse(text))
    out = _Renderer(styles).blocks(tree.children, 0.0, width)
    while out and isinstance(out[-1], Spacer):
        out.pop()
    return out


def _style(styles, name: str, fallback: str = "Normal"):
    try:
        return styles[name]
    except KeyError:
        return styles[fallback]


class _Renderer:
    def __init__(self, styles):
        base = _style(styles, "BodyText2")
        self.body = ParagraphStyle(
            "MdBody", parent=base, alignment=TA_LEFT, spaceAfter=6,
        )
        sub = _style(styles, "SubsubTitle", "Normal")
        self.headings = {
            1: ParagraphStyle(
                "MdH1", parent=self.body, fontName="Helvetica-Bold",
                fontSize=13, leading=16, textColor=COLOR_HEADER_BG,
                spaceBefore=10, spaceAfter=5, keepWithNext=True,
            ),
            # The agenda-point headings of the minutes ("## 3. Budget") wear
            # the same style as the report's own agenda points.
            2: ParagraphStyle(
                "MdH2", parent=self.body, fontName=sub.fontName,
                fontSize=12, leading=15, textColor=COLOR_DARK,
                spaceBefore=8, spaceAfter=4, keepWithNext=True,
            ),
            3: ParagraphStyle(
                "MdH3", parent=self.body, fontName="Helvetica-Bold",
                fontSize=10.5, leading=14, textColor=COLOR_DARK,
                spaceBefore=6, spaceAfter=3, keepWithNext=True,
            ),
            4: ParagraphStyle(
                "MdH4", parent=self.body, fontName="Helvetica-BoldOblique",
                fontSize=10, leading=13, textColor=COLOR_DARK,
                spaceBefore=4, spaceAfter=2, keepWithNext=True,
            ),
        }
        self.minor_heading = ParagraphStyle(
            "MdH5", parent=self.headings[4], fontName="Helvetica-Bold",
            textColor=COLOR_MUTED,
        )
        self.cell = _style(styles, "TableCell")
        self.header = _style(styles, "TableHeader")
        self.code = ParagraphStyle(
            "MdCode", fontName=MONO_FONT, fontSize=MONO_SIZE, leading=MONO_LEADING,
            textColor=base.textColor, spaceAfter=0,
        )
        self._cache: dict = {}

    # ------------------------------------------------------------------ blocks

    def blocks(self, nodes, indent: float, width: float) -> list:
        out: list = []
        for node in nodes:
            out.extend(self.block(node, indent, width))
        return out

    def block(self, node: SyntaxTreeNode, indent: float, width: float) -> list:
        kind = node.type
        if kind == "heading":
            text = self.inline(_inline_child(node))
            if not text.strip():
                return []
            level = _level(node)
            style = self.headings.get(level, self.minor_heading)
            return [Paragraph(text, self._indented(style, indent))]
        if kind == "paragraph":
            text = self.inline(_inline_child(node))
            if not text.strip():
                return []
            return [Paragraph(text, self._indented(self.body, indent))]
        if kind in ("bullet_list", "ordered_list"):
            flow = self.list(node, indent, width, level=0)
            if indent == 0:
                flow.append(Spacer(1, 4))
            return flow
        if kind == "blockquote":
            return self.quote(node, indent, width)
        if kind == "hr":
            return self._with_indent(indent, [HRFlowable(
                width="100%", thickness=0.5, color=COLOR_GRID,
                spaceBefore=4, spaceAfter=8,
            )])
        if kind in ("fence", "code_block"):
            return self.code_block(node.content, indent, width)
        if kind == "table":
            return self._with_indent(indent, [self.table(node, width - indent),
                                              Spacer(1, 6)])
        if kind == "inline":
            text = self.inline(node)
            return [Paragraph(text, self._indented(self.body, indent))] if text.strip() else []
        if node.children:
            return self.blocks(node.children, indent, width)
        content = getattr(node, "content", "") or ""
        if content.strip():
            return [Paragraph(esc(content), self._indented(self.body, indent))]
        return []

    def list(self, node: SyntaxTreeNode, indent: float, width: float, level: int) -> list:
        ordered = node.type == "ordered_list"
        start = 1
        if ordered:
            try:
                start = int(node.attrs.get("start", 1))
            except (TypeError, ValueError):
                start = 1
        items = [c for c in node.children if c.type == "list_item"]
        if ordered:
            widest = max((stringWidth(f"{start + i}.", self.body.fontName,
                                      self.body.fontSize) for i in range(len(items))),
                         default=0)
            step = max(14.0, widest + 6)
        else:
            step = 12.0
        bullet = _BULLETS[level % len(_BULLETS)]
        out: list = []
        for i, item in enumerate(items):
            label = f"{start + i}." if ordered else bullet
            loose = any(c.type == "paragraph" and not getattr(c, "hidden", False)
                        for c in item.children)
            style = self._item_style(indent, step, loose)
            labelled = False
            for child in item.children:
                if not labelled and child.type == "paragraph":
                    out.append(Paragraph(self.inline(_inline_child(child)), style,
                                         bulletText=label))
                    labelled = True
                    continue
                if not labelled:
                    out.append(Paragraph("", style, bulletText=label))
                    labelled = True
                if child.type in ("bullet_list", "ordered_list"):
                    out.extend(self.list(child, indent + step, width, level + 1))
                elif child.type == "paragraph":
                    out.append(Paragraph(self.inline(_inline_child(child)),
                                         self._item_style(indent, step, loose, cont=True)))
                else:
                    out.extend(self.block(child, indent + step, width))
            if not labelled:
                out.append(Paragraph("", style, bulletText=label))
        return out

    def quote(self, node: SyntaxTreeNode, indent: float, width: float) -> list:
        """A left-ruled, shaded panel. One table row per inner flowable so a long
        quote can still break between paragraphs."""
        avail = width - indent
        inner_width = avail - _QUOTE_PAD - 6
        inner = self.blocks(node.children, 0.0, inner_width)
        while inner and isinstance(inner[-1], Spacer):
            inner.pop()
        if not inner:
            return []
        rows = [[f] for f in inner]
        t = Table(rows, colWidths=[avail], hAlign="LEFT")
        cmds = [
            ("LINEBEFORE", (0, 0), (0, -1), _QUOTE_RULE, COLOR_BRAND),
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_SECTION_BG),
            ("LEFTPADDING", (0, 0), (-1, -1), _QUOTE_PAD),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, 0), 5),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
        # A table cell drops the space around its flowable, so the gap between
        # two paragraphs of the quote is restored as padding, merged the way
        # the frame would merge spaceAfter and spaceBefore.
        for r in range(1, len(inner)):
            gap = max(inner[r - 1].getSpaceAfter(), inner[r].getSpaceBefore())
            if gap:
                cmds.append(("TOPPADDING", (0, r), (-1, r), gap))
        t.setStyle(TableStyle(cmds))
        return self._with_indent(indent, [allow_row_split(t, avail), Spacer(1, 6)])

    def code_block(self, content: str, indent: float, width: float) -> list:
        text = (content or "").rstrip("\n")
        if not text.strip():
            return []
        avail = width - indent - 8
        columns = max(20, int(avail // stringWidth("M", MONO_FONT, MONO_SIZE)))
        style = self._indented(self.code, indent + 8)
        return [Preformatted(_wrap_verbatim(text, columns), style), Spacer(1, 6)]

    def table(self, node: SyntaxTreeNode, width: float) -> Table:
        header_rows: list = []
        body_rows: list = []
        for section in node.children:
            target = header_rows if section.type == "thead" else body_rows
            rows = section.children if section.type in ("thead", "tbody") else [section]
            for tr in rows:
                if tr.type != "tr":
                    continue
                target.append([c for c in tr.children if c.type in ("th", "td")])
        all_rows = header_rows + body_rows
        n_cols = max((len(r) for r in all_rows), default=1) or 1
        aligns: list = [TA_LEFT] * n_cols
        source = header_rows[0] if header_rows else (body_rows[0] if body_rows else [])
        for i, cell in enumerate(source[:n_cols]):
            aligns[i] = _align(cell)

        plain = [[_plain(c) for c in r] + [""] * (n_cols - len(r)) for r in all_rows]
        widths = _column_widths(plain, n_cols, width)

        data = []
        for r, row in enumerate(all_rows):
            is_header = r < len(header_rows)
            base = self.header if is_header else self.cell
            cells = []
            for i in range(n_cols):
                markup = self.inline(_inline_child(row[i])) if i < len(row) else ""
                cells.append(Paragraph(markup, self._aligned(base, aligns[i])))
            data.append(cells)
        if not data:
            data = [[Paragraph("", self.cell)]]
        t = Table(data, colWidths=widths, repeatRows=1 if header_rows else 0,
                  hAlign="LEFT")
        cmds = table_style_commands()
        if not header_rows:
            cmds = [c for c in cmds if not (c[0] == "BACKGROUND" and c[1] == (0, 0))]
            cmds = [("ROWBACKGROUNDS", (0, 0), (-1, -1), c[3]) if c[0] == "ROWBACKGROUNDS"
                    else c for c in cmds]
        t.setStyle(TableStyle(cmds))
        return allow_row_split(t, width)

    # ------------------------------------------------------------------ inline

    def inline(self, node: Optional[SyntaxTreeNode]) -> str:
        if node is None:
            return ""
        parts: list[str] = []
        for child in node.children:
            kind = child.type
            if kind == "text":
                parts.append(esc(child.content))
            elif kind == "strong":
                parts.append(f"<b>{self.inline(child)}</b>")
            elif kind == "em":
                parts.append(f"<i>{self.inline(child)}</i>")
            elif kind == "s":
                parts.append(f"<strike>{self.inline(child)}</strike>")
            elif kind == "code_inline":
                parts.append(f'<font face="{MONO_FONT}">{esc(child.content)}</font>')
            elif kind == "softbreak":
                parts.append(" ")
            elif kind == "hardbreak":
                parts.append("<br/>")
            elif kind == "link":
                inner = self.inline(child)
                href = str(child.attrs.get("href", "") or "")
                if href.lower().startswith(_SAFE_SCHEMES):
                    parts.append(f'<a href="{esc_attr(href)}" color="#1a56db">{inner}</a>')
                else:
                    parts.append(inner)
            elif kind == "image":
                # Images are not fetched; the alt text stands in for them.
                parts.append(self.inline(child) or esc(child.attrs.get("alt", "")))
            elif child.children:
                parts.append(self.inline(child))
            else:
                parts.append(esc(getattr(child, "content", "") or ""))
        return "".join(parts)

    # ------------------------------------------------------------------ styles

    def _indented(self, style: ParagraphStyle, indent: float) -> ParagraphStyle:
        if not indent:
            return style
        key = ("ind", style.name, indent)
        if key not in self._cache:
            self._cache[key] = ParagraphStyle(
                f"{style.name}_i{indent:g}", parent=style,
                leftIndent=style.leftIndent + indent,
            )
        return self._cache[key]

    def _item_style(self, indent: float, step: float, loose: bool,
                    cont: bool = False) -> ParagraphStyle:
        key = ("item", indent, step, loose, cont)
        if key not in self._cache:
            self._cache[key] = ParagraphStyle(
                f"MdItem_{indent:g}_{step:g}_{int(loose)}_{int(cont)}", parent=self.body,
                leftIndent=indent + step, bulletIndent=indent,
                bulletFontName=self.body.fontName, bulletFontSize=self.body.fontSize,
                spaceBefore=0, spaceAfter=4 if loose else 2,
            )
        return self._cache[key]

    def _aligned(self, style: ParagraphStyle, alignment: int) -> ParagraphStyle:
        if alignment == style.alignment:
            return style
        key = ("align", style.name, alignment)
        if key not in self._cache:
            self._cache[key] = ParagraphStyle(
                f"{style.name}_a{alignment}", parent=style, alignment=alignment,
            )
        return self._cache[key]

    @staticmethod
    def _with_indent(indent: float, flowables: list) -> list:
        if not indent:
            return flowables
        return [Indenter(left=indent), *flowables, Indenter(left=-indent)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inline_child(node: Optional[SyntaxTreeNode]) -> Optional[SyntaxTreeNode]:
    if node is None:
        return None
    if node.type == "inline":
        return node
    for child in node.children:
        if child.type == "inline":
            return child
    return None


def _level(node: SyntaxTreeNode) -> int:
    try:
        return int(str(node.tag)[1:])
    except (TypeError, ValueError):
        return 1


def _align(cell: SyntaxTreeNode) -> int:
    style = str((cell.attrs or {}).get("style", "") or "")
    if "right" in style:
        return TA_RIGHT
    if "center" in style:
        return TA_CENTER
    return TA_LEFT


def _plain(node: Any) -> str:
    """The visible text of a cell, for measuring column widths."""
    if node is None:
        return ""
    if node.type in ("text", "code_inline"):
        return node.content or ""
    if node.type in ("softbreak", "hardbreak"):
        return " "
    return "".join(_plain(c) for c in node.children)


def _column_widths(rows: list, n_cols: int, avail: float) -> list:
    """Column widths that fill ``avail``: preferred (unwrapped) widths when they
    fit, otherwise every column gets its longest word and the rest of the
    width is shared in proportion to how much more each would like."""
    def measure(s: str) -> float:
        return stringWidth(s, _CELL_FONT, _CELL_SIZE)

    pref = [0.0] * n_cols
    minw = [0.0] * n_cols
    for row in rows:
        for i in range(n_cols):
            text = row[i] if i < len(row) else ""
            pref[i] = max(pref[i], measure(text))
            longest = max((measure(w) for w in text.split()), default=0.0)
            minw[i] = max(minw[i], longest)
    pref = [min(w + _CELL_PAD + 2, avail) for w in pref]
    minw = [max(24.0, min(w + _CELL_PAD + 2, avail * 0.6)) for w in minw]
    pref = [max(a, b) for a, b in zip(pref, minw)]
    total_pref = sum(pref)
    if total_pref <= avail:
        return [w * avail / total_pref for w in pref]
    total_min = sum(minw)
    if total_min >= avail:
        return [w * avail / total_min for w in minw]
    flex = [a - b for a, b in zip(pref, minw)]
    total_flex = sum(flex) or 1.0
    extra = avail - total_min
    return [m + extra * f / total_flex for m, f in zip(minw, flex)]


_BULLET_MARK = ("- ", "* ", "• ", "– ")


def _wrap_verbatim(text: str, columns: int) -> str:
    """Soft-wrap over-long code lines at word boundaries, continuing at the
    line's own indent (the OM ``_wrap_verbatim``, simplified)."""
    out: list[str] = []
    for line in text.replace("\t", "    ").split("\n"):
        if len(line) <= columns:
            out.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line[indent:]
        hang = indent + (2 if body.startswith(_BULLET_MARK) else 0)
        prefix = " " * indent
        current = ""
        for word in body.split(" "):
            while len(word) > columns - hang:
                # A single token wider than the frame: hard-break it.
                if current:
                    out.append(prefix + current)
                    prefix, current = " " * hang, ""
                cut = max(1, columns - len(prefix))
                out.append(prefix + word[:cut])
                prefix, word = " " * hang, word[cut:]
            candidate = f"{current} {word}" if current else word
            if current and len(prefix) + len(candidate) > columns:
                out.append(prefix + current)
                prefix, current = " " * hang, word
            else:
                current = candidate
        if current:
            out.append(prefix + current)
    return "\n".join(out)


__all__ = ["minutes_to_flowables"]

"""Meet++ meeting report renderer (contract §7).

Pure, deterministic ReportLab renderers over the export dict built by
``app.meetpp.export.build_export``:

* ``render_meeting_report(export) -> bytes`` — the meeting report PDF, a port of
  the OneVoice OM module's meeting report (same structure, fonts, colours,
  tables, running header and identity footer), with the minutes rendered from
  Markdown;
* ``render_agenda(export) -> bytes`` — the next-meeting agenda PDF;
* ``minutes_to_flowables(markdown, styles) -> list`` — the Markdown renderer on
  its own, for reuse in other ReportLab documents;
* ``build_styles()`` — the report stylesheet ``minutes_to_flowables`` expects.

No database, no clock, no network: the same export always yields the same bytes.
"""
from __future__ import annotations

from .agenda import render_agenda
from .markdown import minutes_to_flowables
from .meeting import render_meeting_report
from .styles import build_styles

__all__ = ["render_meeting_report", "render_agenda", "minutes_to_flowables", "build_styles"]

"""
Generate an iCalendar (VCALENDAR/VEVENT) representation of a Meeting.

Kept dependency-free so the API container doesn't need to add an extra
package for one file. RFC 5545 line folding (CRLF + space at 75 chars) is
applied via `_fold` because some clients (Outlook) silently truncate long
unfolded lines.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import Meeting


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _utc_stamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fold(line: str) -> str:
    if len(line) <= 75:
        return line
    chunks = [line[i : i + 73] for i in range(0, len(line), 73)]
    return chunks[0] + "".join("\r\n " + c for c in chunks[1:])


def ics_for_meeting(m: Meeting) -> str:
    join_url = f"{settings.public_url}/{m.room_name}"
    dtstart = m.scheduled_at or datetime.now(timezone.utc)
    duration = m.duration_minutes if m.duration_minutes and m.duration_minutes > 0 else 60
    dtend = dtstart + timedelta(minutes=duration)
    now = datetime.now(timezone.utc)
    uid = f"meet-{m.id}@meet.witysk.org"
    description = (
        f"Join the meeting: {join_url}"
        + (f"\\nHost: {m.owner_name}" if m.owner_name else "")
        + (f"\\n\\n{_ics_escape(m.lobby_greeting)}" if m.lobby_greeting else "")
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//meet.witysk.org//Meeting Invite//EN",
        "METHOD:PUBLISH",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_utc_stamp(now)}",
        f"DTSTART:{_utc_stamp(dtstart)}",
        f"DTEND:{_utc_stamp(dtend)}",
        f"SUMMARY:{_ics_escape(m.display_title)}",
        f"DESCRIPTION:{description}",
        f"LOCATION:{_ics_escape(join_url)}",
        f"URL:{_ics_escape(join_url)}",
    ]
    if m.recurrence_rule:
        # Trust the stored RRULE; it was validated on write.
        lines.append(f"RRULE:{m.recurrence_rule}")
    lines += [
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


# Accept-list for the recurrence rules the create-meeting form offers.
ALLOWED_RRULES: set[str] = {
    "FREQ=DAILY",
    "FREQ=WEEKLY",
    "FREQ=WEEKLY;INTERVAL=2",
    "FREQ=MONTHLY",
}

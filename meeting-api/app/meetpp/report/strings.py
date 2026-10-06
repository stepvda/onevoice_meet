"""English labels of the meeting report.

Copied from the English half of the OM catalogue (``org_pdf_i18n._EN``) so the
wording on the page is the wording the association already files. Meet++ is
English-only (contract, preamble), so there is no language switch here; the
few keys the OM catalogue does not carry (the next-meeting agenda) are written
in the same register and marked below.
"""
from __future__ import annotations

_EN = {
    # --- page chrome -------------------------------------------------------
    "doc.generated": "Generated {when}",
    "doc.page": "Page {n} of {total}",
    # Authored as ReportLab mini-XML (numeric entities for the spacing), so
    # this one string is handed to Paragraph unescaped.
    "doc.signature": ("Approved by the General Assembly &#8212; date: ______________ "
                      "&#160;&#160; signature: ______________________"),
    "doc.yes": "Yes",
    "doc.no": "No",

    # --- identity block ----------------------------------------------------
    "identity.seat": "Registered seat: {seat}",
    "identity.enterprise": "Enterprise number: {number}",
    "identity.enterprise_footer": "Enterprise number {number}",
    "identity.email": "Email: {email}",
    "identity.website": "Website: {website}",
    "identity.iban": "Bank account (IBAN): {iban}",
    "identity.placeholder_listed": (
        "PROVISIONAL — NOT FOR FILING. This document still carries placeholder "
        "identification data ({listed}). It may not be filed with the "
        "enterprise court, nor sent to a third party, until the real values "
        "have been entered."),
    # Used when the export only says "placeholders: true" without naming the
    # fields: the OM fallback would print "(identification data)" twice over.
    "identity.placeholder": (
        "PROVISIONAL — NOT FOR FILING. This document still carries placeholder "
        "identification data. It may not be filed with the enterprise court, "
        "nor sent to a third party, until the real values have been entered."),

    # --- shared column headings -------------------------------------------
    "col.status": "Status",
    "col.member": "Member",
    "col.username": "Username",

    # --- meeting report ----------------------------------------------------
    "meeting.title": "Meeting report",
    "meeting.title_ga": "General Assembly — report",
    "meeting.subject": "Minutes of a meeting of the association",
    "meeting.datetime": "Date and time",
    "meeting.location": "Location",
    "meeting.type": "Type",
    "meeting.series": "Series",
    "meeting.convened_at": "Convened at",
    "meeting.adjourned_at": "Adjourned at",
    "meeting.quorum": "Quorum: {note}",
    "meeting.attendance": "Attendance",
    "meeting.attendance_summary": ("Present: {present}  |  Represented: {represented}  |  "
                                   "Absent: {absent}  |  Excused: {excused}  |  "
                                   "Not registered: {expected}"),
    "meeting.represented_by": "Represented by",
    "meeting.proxy_mandate": "Written mandate",
    "meeting.no_members": "No members registered.",
    "meeting.excused_names": "Excused: {names}.",
    "meeting.agenda": "Agenda",
    "meeting.no_agenda": "No agenda items.",
    "meeting.decisions": "Decisions",
    "meeting.no_decisions": "No decisions were recorded.",
    "meeting.decision_status": "Status: {status}",
    "meeting.decided_at": "decided {when}",
    "meeting.actions": "Follow-up actions",
    "meeting.no_actions": "No follow-up actions.",
    "meeting.action_carried": "carried forward",
    "meeting.action_raised_here": "raised there",
    "meeting.action_status": "Status: {status}",
    "meeting.action_assigned": "Assigned to: {names}",
    "meeting.action_due": "Due {when}",
    "meeting.action_completed": "Completed {when}",
    "meeting.action_from_decision": "From decision: {title}",
    "meeting.action_reported": "Reported at this meeting:",
    "meeting.action_progress": "Progress notes:",
    "meeting.action_completion": "Completion note:",
    "meeting.action_also_on": "Also on: {meetings}",
    "meeting.attachments": "Papers filed with this meeting",
    "meeting.attachment_title": "Paper",
    "meeting.attachment_item": "Agenda point",
    "meeting.attachment_file": "File",
    "meeting.no_attachments": "No papers were filed.",
    "meeting.agenda_more": "… (full text in the agenda)",
    "meeting.minutes": "Minutes",
    "meeting.no_minutes": "No minutes were recorded.",
    "meeting.approved": "Approved {when}",
    "meeting.minutes_saved": "Minutes last saved {when}",
    "meeting.minutes_saved_version": "Minutes last saved {when} — version {no}",
    "meeting.minutes_version_count": "{count} versions have been saved.",

    # --- votes -------------------------------------------------------------
    "vote.question": "Vote",
    "vote.for": "For",
    "vote.against": "Against",
    "vote.abstain": "Abstain",
    "vote.result": "Result",
    "vote.cast_by": "Cast by",
    "vote.proxy": "Proxy",
    "vote.none": "No vote was held.",
    "vote.body": "Voting body",
    "vote.majority": "Majority required",
    "vote.eligible": "Entitled to vote",
    "vote.present": "Present or represented",
    "vote.basis": "Basis",
    "vote.quorum_required": "Quorum required",
    "vote.quorum_met": "Quorum met",

    # --- next-meeting agenda (Meet++ only; not in the OM catalogue) ---------
    "agenda.title": "Meeting agenda",
    "agenda.subject": "Agenda of a meeting of the association",
    "agenda.to_be_confirmed": "To be confirmed",
    "agenda.previous_meeting": "Previous meeting",
    "agenda.open_actions": "Open actions carried forward",
    "agenda.no_open_actions": "No open actions are carried forward.",
}


def T(key: str, **kw) -> str:
    """Look up a label and interpolate it.

    Falls back to the key itself, like the OM ``T()``: a missing label shows up
    as ``meeting.attendance`` on the page, which is unmistakable.
    """
    template = _EN.get(key)
    if template is None:
        return key
    if not kw:
        return template
    try:
        return template.format(**kw)
    except (KeyError, IndexError, ValueError):
        return template

"""Prompt construction (English only; FDD Appendix C, contract §5).

Stable content comes first (system prompt with schema and example, then the
outline and the record) so the provider's prefix cache hits; the transcript
comes last. Display names are used as spoken; e-mail addresses and usernames
are never sent. DeepSeek's JSON mode needs the word "json" and an example in
the prompt, so every prompt carries both.
"""
from __future__ import annotations

import json

PROMPT_VERSION = "v3.2"

TICK_EXAMPLE = {
    "topic": {"section": "S5", "sub": "b", "confidence": 0.86},
    "advance": None,
    "ops": [
        {
            "op": "decision.update",
            "ref": "D-4",
            "status": "adopted",
            "resolution": "that the rainwater tank is installed before the municipal connection is quoted",
            "how_taken": "Taken with the assent of both directors present.",
            "decided_at_seq": 233,
            "vote": {"method": "assent", "for": 2, "against": 0, "abstain": 0,
                     "ballots": [{"person": "Alex Morgan", "choice": "for"}, {"person": "Sam Lee", "choice": "for"}]},
            "evidence": [229, 231, 233],
        },
        {
            "op": "action.add",
            "section": "S5",
            "title": "Ask two suppliers for a quote for the rainwater tank",
            "description": "The municipal quote is not expected before spring.",
            "assignees": ["Sam Lee"],
            "due": None,
            "from_decision": None,
            "evidence": [214],
        },
        {"op": "action.update", "ref": "A-13", "report_note": "Three volunteers signed up; the watering rota is circulated.", "status": "open", "evidence": [205]},
        {"op": "attendance.set", "person": "Chris Doe", "status": "excused", "evidence": [3]},
    ],
    "notes": [{"section": "S5", "text": "Sam Lee will collect the quotes before the next meeting.", "evidence": [231]}],
}

TICK_SCHEMA = """{
 "topic": {"section": "S<n>", "sub": "<sub-point letter or S-id, optional>", "confidence": 0.0-1.0},
 "advance": {"to": "S<n>", "confidence": 0.0-1.0, "reason": "<short quote>"} | null,
 "ops": [ any of
  {"op":"decision.add","section":"S<n>","title":"<=300","resolution":"that …","how_taken":"<=600","status":"proposed|adopted|rejected|withdrawn","decided_at_seq":<seq>|null,"vote":{"method":"voice|show_of_hands|assent|consensus|roll_call","for":n,"against":n,"abstain":n,"ballots":[{"person":"<name>","choice":"for|against|abstain"}]}|null,"evidence":[seq,…]},
  {"op":"decision.update","ref":"D-<n>","title"?,"resolution"?,"how_taken"?,"status"?,"decided_at_seq"?,"vote"?,"evidence":[seq,…]},
  {"op":"action.add","section":"S<n>","title":"<=300","description"?,"assignees":["<name>"],"due":"YYYY-MM-DD"|null,"from_decision":"D-<n>"|null,"evidence":[seq,…]},
  {"op":"action.update","ref":"A-<n>","status"?:"open|in_progress|done|cancelled","report_note"?,"progress_note"?,"completion_note"?,"due"?,"assignees"?,"evidence":[seq,…]},
  {"op":"attendance.set","person":"<name>","status":"present|represented|absent|excused","represented_by"?,"evidence":[seq,…]},
  {"op":"attendance.require_next","person":"<name>","reason":"<=300","evidence":[seq,…]},
  {"op":"section.add","kind":"subpoint|aob","parent":"S<n>"?,"title":"<=300"},
  {"op":"next_meeting.propose","when_text":"<as said>","iso":"YYYY-MM-DDTHH:MM"|null,"evidence":[seq,…]}
 ],
 "notes": [{"section":"S<n>","text":"<=300 characters","evidence":[seq,…]}]
}"""


def tick_system(*, org: str, meeting_type_label: str) -> str:
    return (
        f"You keep the live record of a {meeting_type_label.lower()} of {org}, held in English.\n"
        "You receive the meeting outline (sections S1…Sn, sub-points indented), the current record and "
        "new transcript lines. Return json only: one object matching the schema below.\n"
        "Rules:\n"
        "- topic: the outline section the NEW lines are about (give the sub-point in \"sub\" when clear), with a "
        "confidence between 0 and 1.\n"
        "- advance: only when the meeting clearly moves on to a later section (for example the chair says "
        "\"next item\" and the talk turns to it); otherwise null. Never backwards. Moving through the sub-points "
        "or numbered paragraphs of the LIVE point (its agenda text is shown) is not an advance.\n"
        "- When someone proposes or announces a decision (\"we want to take a decision that …\", \"I propose that "
        "…\", \"let's decide …\"), add it at once with decision.add, status proposed; update it to adopted (or rejected) "
        "when it is agreed. When someone announces or asks for an action (\"new action: …\", \"we want to define an "
        "action …\", \"can you …\", \"I'll …\"), add it with action.add. Do this in any section, also when the point is "
        "about something else.\n"
        "- Record decisions when something is agreed, resolved, approved or rejected; write the resolution in "
        "the form \"that …\". Record how it was taken (\"Put by the chair as a voice vote; both directors present "
        "answered in favour.\") and any vote you can hear; ballots only with audible names. Every adopted or "
        "rejected decision carries a vote: when no count was taken and nobody objected, method \"assent\".\n"
        "- When a decision is taken, give it a title that states the outcome (\"Rainwater tank — installed before the municipal "
        "connection\"), not the question that was asked.\n"
        "- DECISIONS TO TAKE are already on the record with a ref (status pending): when one is decided, "
        "withdrawn or discussed, use decision.update with its ref. Never add it again with decision.add. "
        "The same holds for any decision already listed: update it by ref.\n"
        "- Record actions when someone undertakes or is asked to do something; list assignees by name exactly "
        "as in the member list (other names are allowed for guests). Use action.update with the ref for an "
        "action that is already listed.\n"
        "- For previous actions (A-n, marked TO REVIEW until reported) record what was reported about them at "
        "this meeting (report_note), status changes (done with a completion_note) and progress notes. Every "
        "action.update on a previous action carries a report_note.\n"
        "- Set an action to done only when it is said that this particular action is finished. A general remark "
        "(\"that closes off the garden actions\") does not cover an item named as not yet done (\"the only thing "
        "we didn't do is …\", \"keep that open\"): that one stays open, with a report_note saying what remains.\n"
        "- Before action.add, compare with PREVIOUS ACTIONS and ACTIONS RECORDED: when the undertaking is the same "
        "work as a listed action, even worded differently, use action.update on it instead.\n"
        "- If later lines show that something on the record is wrong (for example an action marked done that is "
        "still open), correct it with an update. Do not repeat an update that is already on the record.\n"
        "- attendance.set when someone is said to be absent, excused (apologies) or represented by someone else; "
        "attendance.require_next when someone must attend the next meeting.\n"
        "- section.add only when a new sub-point or an \"any other business\" item is raised.\n"
        "- notes: up to 6 short factual points (at most 300 characters each) for the minutes of the section they "
        "belong to; third person, past tense, names as given; no speculation; do not repeat RUNNING NOTES.\n"
        "- Cite evidence as transcript line numbers [seq]. CONTEXT lines may be cited, but every op except "
        "section.add and every note must cite at least one NEW line.\n"
        "- Use only what is said. Do not invent names, figures or dates. If nothing applies, return empty lists "
        "and \"advance\": null.\n"
        f"Schema:\n{TICK_SCHEMA}\n"
        f"Example json output:\n{json.dumps(TICK_EXAMPLE, ensure_ascii=False)}"
    )


def _block(title: str, lines: list[str]) -> str:
    return f"{title}:\n" + ("\n".join(lines) if lines else "(none)")


def transcript_line(seg: dict) -> str:
    return f"[{seg['seq']}] {seg.get('time') or '--:--:--'} {seg.get('name') or 'Unknown'}: {seg.get('text') or ''}"


def build_tick_messages(
    *,
    org: str,
    meeting_type_label: str,
    title: str,
    date: str,
    mode: str,
    outline_lines: list[str],
    live: str | None,
    topic: str | None,
    members: list[str],
    previous_actions: list[str],
    pending_decisions: list[str],
    decisions: list[str],
    actions: list[str],
    running_notes: list[str],
    context: list[dict],
    window: list[dict],
) -> list[dict]:
    user = "\n\n".join(
        [
            f"MEETING: {title} · {date} · {meeting_type_label} · AI mode {mode}",
            _block(f"OUTLINE (LIVE = {live or 'none'}; last topic = {topic or 'none'})", outline_lines),
            _block("MEMBERS AND ATTENDANCE", members),
            _block("PREVIOUS ACTIONS (open; report on them with action.update)", previous_actions),
            _block("DECISIONS TO TAKE (pending; update by ref, never re-add)", pending_decisions),
            _block("DECISIONS RECORDED", decisions),
            _block("ACTIONS RECORDED AT THIS MEETING", actions),
            _block("RUNNING NOTES OF THE LIVE SECTION", running_notes),
            _block("CONTEXT (already processed; may be cited)", [transcript_line(s) for s in context]),
            _block("NEW (cite at least one of these)", [transcript_line(s) for s in window]),
        ]
    )
    return [
        {"role": "system", "content": tick_system(org=org, meeting_type_label=meeting_type_label)},
        {"role": "user", "content": user},
    ]


# ─── Section composition (C.2) ─────────────────────────────────────────────

SECTION_EXAMPLE = {
    "markdown": (
        "Sam Lee reported that the garden had used town water all summer. The chair asked whether a tank could "
        "be added later; Sam Lee answered that this was not difficult.\n\n"
        "### 2.1 Water supply\n\nThe members agreed not to wait for the municipal quote.\n\n"
        "> **RESOLVED:** that the rainwater tank is installed before the municipal connection is quoted."
    ),
    "verify": ["the EUR 300 figure quoted for the tank"],
}


def section_system(*, org: str, meeting_type_label: str) -> str:
    return (
        f"Write the minutes for one agenda section of a {meeting_type_label.lower()} of {org} in English.\n"
        "Style: formal minutes; third person; past tense; names as given; concise but complete; plain "
        "paragraphs. Do not repeat the section heading (it is added for you).\n"
        "Report only what the TRANSCRIPT and NOTES show was said or done. The AGENDA TEXT and SUB-POINTS are "
        "context for understanding, never evidence that something was discussed: do not restate them as "
        "discussion. Leave out sub-points that were not discussed.\n"
        "Use \"### n.m Title\" sub-headings for the sub-points that were discussed, with the numbers given.\n"
        "For every ADOPTED decision add a block quote \"> **RESOLVED:** that …\" using its resolution text. Never "
        "write a RESOLVED block for a decision that is not listed as adopted. If no decision was adopted in this "
        "agenda point, end with the sentence \"No resolution was put.\" (not for the opening, previous actions, "
        "new actions or closing sections).\n"
        "Do not invent figures, dates or names; list any figure, amount or name you are unsure about in "
        "\"verify\". Keep close to the LENGTH given: record what was reported, argued, agreed and undertaken, "
        "not every remark; a short exchange gets a short paragraph. The RUNNING NOTES are raw material taken "
        "during the meeting: merge and condense them, do not reproduce them one by one.\n"
        "Return json only: {\"markdown\": \"...\", \"verify\": [\"...\"]}.\n"
        f"Example json output:\n{json.dumps(SECTION_EXAMPLE, ensure_ascii=False)}"
    )


def build_section_messages(
    *,
    org: str,
    meeting_type_label: str,
    heading: str,
    agenda_body: str | None,
    subpoints: list[str],
    members: list[str],
    decisions: list[str],
    actions: list[str],
    notes: list[str],
    transcript: list[dict],
    target_words: int | None = None,
    retry_error: str | None = None,
) -> list[dict]:
    parts = [
        f"SECTION: {heading}",
        f"LENGTH: about {target_words or 400} words",
        f"AGENDA TEXT:\n{agenda_body or '(none)'}",
        _block("SUB-POINTS", subpoints),
        _block("MEMBERS", members),
        _block("DECISIONS (status; only ADOPTED ones get a RESOLVED block)", decisions),
        _block("ACTIONS", actions),
        _block("RUNNING NOTES", notes),
        _block("TRANSCRIPT OF THE SECTION", [transcript_line(s) for s in transcript]),
    ]
    if retry_error:
        parts.append(f"Your previous draft was rejected: {retry_error}. Correct it.")
    return [
        {"role": "system", "content": section_system(org=org, meeting_type_label=meeting_type_label)},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def build_condense_points_messages(*, markdown: str, max_points: int) -> list[dict]:
    """Condense, step 1: the key points of an over-long section draft. (Asked
    to shorten the draft itself, the model mostly copies it.)"""
    system = (
        "From the minutes of one agenda section, list the key points for the official record: what was reported, "
        f"argued, decided or undertaken, and by whom. At most {max_points} points, one sentence each, in the order "
        "they occur. When the minutes have \"### n.m\" sub-headings, start each point with the number in square "
        "brackets, e.g. \"[6.2] …\". Merge points that say the same thing; leave out examples, quotations, asides and "
        "the RESOLVED blocks. Return json only: {\"points\": [\"...\"]}."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": markdown}]


def build_condense_write_messages(
    *, org: str, meeting_type_label: str, points: list[str], headings: list[str], target_words: int
) -> list[dict]:
    """Condense, step 2: the minutes written from the key points alone."""
    heads = "\n".join(headings) or "(none)"
    system = (
        f"Write the minutes of one agenda section of a {meeting_type_label.lower()} of {org} from the key points "
        "given, in formal minutes style: third person, past tense, names as given, connected prose. Group related "
        "points into paragraphs of three to six sentences. When points carry a number in square brackets, put them "
        "under the matching sub-heading, written on its own line exactly as listed in SUB-HEADINGS; never write the "
        "bracketed numbers. Add nothing that is not in the points, and do not write RESOLVED blocks (they are added "
        f"for you). About {target_words} words. Return json only: {{\"markdown\": \"...\"}}."
    )
    user = f"SUB-HEADINGS:\n{heads}\n\nKEY POINTS:\n" + "\n".join(f"- {p}" for p in points)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_reconcile_messages(*, org: str, meeting_type_label: str, actions: list[str], minutes: str) -> list[dict]:
    """Previous actions against the minutes of this meeting (finalisation)."""
    system = (
        f"You check the follow-up actions of earlier meetings of {org} against the minutes of this "
        f"{meeting_type_label.lower()}. For each action listed, decide whether the minutes report anything about it — "
        "progress, completion, a change of plan, or that it stays open for a stated reason. When they do, give its status "
        "now (open, in_progress, done or cancelled) and a short note for the report's “reported at this meeting”, one or "
        "two sentences, third person, past tense, names as given, based only on the minutes. An action is done only when "
        "the minutes say that this work is finished; a general remark does not close a specific action. Report an "
        "action only when the minutes clearly refer to that same work, and copy into \"quote\" the sentence of the "
        "minutes that does so, word for word. When in doubt, leave it out: no report is better than a wrong one. Return "
        "json only: {\"reports\": [{\"ref\": \"A-7\", \"status\": \"done\", \"note\": \"...\", \"quote\": \"...\"}]}."
    )
    user = "ACTIONS:\n" + "\n".join(actions) + "\n\nMINUTES OF THIS MEETING:\n" + minutes
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_verify_closed_messages(*, actions: list[str], transcript: str) -> list[dict]:
    """Previous actions closed during the meeting, checked against the transcript."""
    system = (
        "These follow-up actions were marked done during a meeting. For each one, find the line of the transcript where "
        "a speaker says that this particular work is finished, and copy that line's words into \"quote\" exactly. A "
        "remark about related or general work does not count (\"the backup works\" does not finish \"prove the full "
        "restore\"), nor does a remark that it is still to be done. Leave out every action with no such line. Return "
        "json only: {\"confirmed\": [{\"ref\": \"A-7\", \"quote\": \"...\"}]}."
    )
    user = "ACTIONS MARKED DONE:\n" + "\n".join(actions) + "\n\nTRANSCRIPT:\n" + transcript
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ─── Final composition (C.3) ───────────────────────────────────────────────

FINAL_EXAMPLE = {
    "opening": "The chair noted that Chris Doe had not joined, confirmed that the two directors present were content "
    "to proceed, and took the agenda from 19:05 UTC.",
    "adjournment": "The chair thanked the members and declared the meeting adjourned at 20:40 UTC.",
    "summary": ["The rainwater tank was approved ahead of the municipal connection."],
    "next_agenda": [{"title": "Rainwater tank", "body": "Report on the two supplier quotes."}],
    "verify": ["the EUR 180 hall rental figure"],
}


def final_system(*, org: str, meeting_type_label: str) -> str:
    return (
        f"Assemble the closing parts of the minutes of a {meeting_type_label.lower()} of {org}, in English, formal "
        "minutes style (third person, past tense).\n"
        "- opening: one paragraph on who was present and absent, the quorum (formal meetings only), the start time "
        "and the agenda taken; integrate what the OPENING DISCUSSION section shows was said at the opening, without "
        "repeating facts.\n"
        "- adjournment: one or two sentences with the closing time.\n"
        "- summary: up to 5 one-sentence highlights.\n"
        "- next_agenda: points for the next meeting (deferred points, follow-ups, open questions).\n"
        "- verify: figures, amounts and names that should be checked against the recording.\n"
        "Do not rewrite the section minutes and do not invent facts. Return json only with these five keys.\n"
        f"Example json output:\n{json.dumps(FINAL_EXAMPLE, ensure_ascii=False)}"
    )


def build_final_messages(*, org: str, meeting_type_label: str, facts: dict, section_minutes: str) -> list[dict]:
    user = (
        "FACTS (json):\n"
        + json.dumps(facts, ensure_ascii=False, indent=1)
        + "\n\nSECTION MINUTES (for reference only):\n"
        + (section_minutes[-30000:] or "(none)")
    )
    return [
        {"role": "system", "content": final_system(org=org, meeting_type_label=meeting_type_label)},
        {"role": "user", "content": user},
    ]


# ─── Setup documents ───────────────────────────────────────────────────────

AGENDA_REFINE_EXAMPLE = {
    "points": [
        {"number": "4", "title": "Website: move to a new host"},
        {"number": "2", "subpoints": [{"label": "c", "title": "Tank first, or wait for the municipal quote"}]},
    ],
    "decisions": [
        {
            "point": "2",
            "sub": "c",
            "title": "Install the rainwater tank first",
            "resolution": "that the rainwater tank is installed before the municipal connection is quoted",
        }
    ],
}


def build_agenda_refine_messages(points: list[dict]) -> list[dict]:
    """The agenda structure is parsed deterministically; the LLM only writes
    short titles for long points and finds the decisions to take."""
    system = (
        "You prepare a meeting agenda for the live record. You receive the agenda points as json (number, title, "
        "body, sub-points with labels). Return json only with two keys:\n"
        "- points: for every point whose title is longer than 80 characters, is a whole sentence or ends with "
        "\"…\", a short title (at most 80 characters, no final full stop) that names the subject the meeting "
        "deals with (often the proposal or question at the end of the point), not the background; likewise "
        "\"subpoints\" [{\"label\", \"title\"}] for long sub-point titles. Leave out what is already short.\n"
        "- decisions: the decisions the meeting is asked to take: motions and questions for decision such as "
        "\"Shall we …?\", \"Resolved that …?\", \"Question for the board: …\", \"We can vote on it …\", "
        "\"Confirming: …\", \"Suggestion: …?\", or a point that asks for approval. For each give the point "
        "number, the sub-point label when it sits in one, a short title and a draft resolution in the form "
        "\"that …\". Do not list questions that only ask for information, nor points that ask for a report "
        "or a confirmation of fact (\"Confirmation that X can now …\", \"Update on …\").\n"
        "Use only the text given; do not invent.\n"
        f"Example json output:\n{json.dumps(AGENDA_REFINE_EXAMPLE, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "AGENDA POINTS (json):\n" + json.dumps(points, ensure_ascii=False)[:40000]},
    ]


AGENDA_STRUCTURE_EXAMPLE = {
    "points": [
        {
            "title": "Volunteer handbook",
            "body": "Draft for approval.",
            "presenter": None,
            "timebox_minutes": None,
            "subpoints": [{"title": "Safety rules", "body": "New section on tools."}],
        }
    ],
    "decisions": [{"point": "1", "sub": None, "title": "Approve the handbook", "resolution": "that the volunteer handbook is approved"}],
}


def build_agenda_structure_messages(text: str) -> list[dict]:
    system = (
        "You structure a meeting agenda taken from a PDF. Return json only: {\"points\": [{\"title\", \"body\", "
        "\"presenter\", \"timebox_minutes\", \"subpoints\": [{\"title\", \"body\"}]}], \"decisions\": [{\"point\", "
        "\"sub\", \"title\", \"resolution\"}]}. Points are numbered in order from 1. Titles at most 80 characters; "
        "the body keeps the description. Sub-points (a), (b), … become subpoints; nested lists stay in the body. "
        "decisions lists the motions and questions for decision (\"Shall we …?\", \"Resolved that …\", \"Question "
        "for the board: …\") with a draft resolution \"that …\". Use only the document; at most 40 points.\n"
        f"Example json output:\n{json.dumps(AGENDA_STRUCTURE_EXAMPLE, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": text[:30000]}]


NOTES_EXAMPLE = {
    "actions": [
        {
            "ref": "7",
            "title": "Collect two quotes for the rainwater tank",
            "description": "Before the next meeting.",
            "assignees": ["Sam Lee"],
            "due": "2026-10-11",
            "status": "open",
            "progress_notes": None,
        }
    ],
    "decisions": [{"title": "Approve the minutes of meeting #3", "status": "adopted"}],
    "attendance": [{"name": "Chris Doe", "username": "chris", "status": "absent"}],
}


def build_notes_structure_messages(text: str) -> list[dict]:
    system = (
        "You read the report or notes of a previous meeting. Return json only with: actions (the follow-up actions "
        "with ref, title, description, assignees, due as YYYY-MM-DD or null, status open|in_progress|done|cancelled, "
        "progress_notes), decisions (title and status) and attendance (name, username or null, status "
        "present|represented|absent|excused). Use only the document.\n"
        f"Example json output:\n{json.dumps(NOTES_EXAMPLE, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": text[:30000]}]

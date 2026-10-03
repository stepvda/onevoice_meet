"""Prompt construction.

Stable content comes first (system prompt + schema + examples, then meeting
header / agenda / previous actions / state) so DeepSeek's prefix cache hits.
The transcript window comes last.
"""
from __future__ import annotations

import json

PROMPT_VERSION = "tick-v1"
SCHEMA_VERSION = "ops-v1"

# Compact schema string shown to the model.
SCHEMA = """{
 "ops": [ one of
   {"op":"agenda.set_active","item_id":"<id>"} |
   {"op":"agenda.set_status","item_id":"<id>","status":"done"|"deferred"} |
   {"op":"agenda.add","title":"...","presenter":"..."|null} |
   {"op":"decision.add","item_id":"<id>"|null,"text":"<=400","rationale":"<=400"|null,"evidence":[seq,...]} |
   {"op":"decision.update","id":"<id>","text":"<=400"|null,"status":"proposed"|"confirmed"|"rejected"} |
   {"op":"action.add","title":"<=200","owner_alias":"P<n>"|null,"due":"YYYY-MM-DD"|null,"item_id":"<id>"|null,"evidence":[seq,...]} |
   {"op":"action.update","id":"<id>","status":"open"|"in_progress"|"done"|"dropped"|"carried"|null,"owner_alias":"P<n>"|null,"due":"YYYY-MM-DD"|null,"note":"<=500"|null,"evidence":[seq,...]} |
   {"op":"minutes.upsert","item_id":"<id>","body_md":"<=1500 chars markdown"} |
   {"op":"attendance.apologies","person":"<alias or quoted name>","evidence":[seq,...]} |
   {"op":"attendance.require_next","person":"<alias or quoted name>","reason":"<=120","evidence":[seq,...]}
 ],
 "phase_signal": {"to":"opening|previous_actions|agenda|discussion|discussion:<id>|discussion:next_item|aob|new_actions|closing","confidence":0.0-1.0,"reason":"<=160"} | null,
 "notes": "optional debug text"
}"""

_LAST_SEQ = object()


def _seg_lines(segments: list[dict]) -> str:
    out = []
    for s in segments:
        out.append(f"[{s.get('seq')}] {s.get('speaker') or '?'} {s.get('t') or ''} {s.get('text') or ''}".strip())
    return "\n".join(out) or "(none)"


def build_tick_messages(
    *,
    language: str,
    title: str,
    date: str,
    template: str,
    phase: str,
    current_item_id: str | None,
    aliases: dict[str, str],
    agenda: list[dict],
    prev_actions: list[dict],
    state: dict,
    context: list[dict],
    window: list[dict],
) -> list[dict]:
    alias_line = ", ".join(f"{k}={v}" for k, v in aliases.items()) or "(none)"
    system = (
        f"You maintain the official record of a business meeting held in {language}.\n"
        "Rules:\n"
        "- Use ONLY what is said in the transcript lines provided. Never invent people, dates or decisions.\n"
        "- If unsure, emit no operation. Fewer correct operations beat many guesses.\n"
        "- People are referred to by alias (P1…Pn). Owners must be an alias or a quoted name from the "
        "attendee list; otherwise leave owner empty.\n"
        "- Every decision/action/attendance operation must cite evidence: the [seq] numbers of the lines.\n"
        "- Record every explicit decision (\"we agree\", \"decided\", \"we will not\", \"approved\") with decision.add, "
        "even if there are no agenda items (item_id may be null).\n"
        "- Never modify items marked \"locked\"; you may still reference them.\n"
        f"- Write minutes concisely in {language}, third person, factual, max 1500 characters per item.\n"
        "- Output a single JSON object that matches the schema below. Output json only.\n"
        f"Schema: {SCHEMA}\n"
        "Example output: {\"ops\":[{\"op\":\"action.add\",\"title\":\"Send the draft\",\"owner_alias\":\"P2\","
        "\"due\":\"2026-10-17\",\"item_id\":\"01JA...\",\"evidence\":[412,415]}],\"phase_signal\":null}"
    )
    user = (
        f"MEETING: {title} · {date} · template={template} · phase={phase} · active_item={current_item_id or '-'}\n"
        f"ALIASES: {alias_line}\n"
        f"AGENDA: {json.dumps(agenda, ensure_ascii=False)}\n"
        f"PREVIOUS ACTIONS: {json.dumps(prev_actions, ensure_ascii=False)}\n"
        f"STATE: {json.dumps(state, ensure_ascii=False)}\n"
        "TRANSCRIPT (already processed, for context only):\n"
        f"{_seg_lines(context)}\n"
        "TRANSCRIPT (new):\n"
        f"{_seg_lines(window)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_agenda_messages(*, language: str, text: str) -> list[dict]:
    system = (
        "You structure an uploaded meeting agenda. Output json only with this exact shape: "
        '{"items":[{"title":"...","presenter":"..."|null,"timebox_minutes":int|null,"outcome":"..."|null}],'
        '"attendees":["..."]}. Use only what is in the document. Write in '
        f"{language}. Return at most 40 items."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": text[:120000]}]


def build_notes_messages(*, language: str, text: str) -> list[dict]:
    system = (
        "You structure previous meeting notes. Output json only with this exact shape: "
        '{"actions":[{"title":"...","owner":"..."|null,"due":"YYYY-MM-DD"|null,"status":"open"|"done"|"dropped"}],'
        '"decisions":[{"text":"..."}],"attendees":["..."],"next_agenda":["..."]}. '
        f"Use only what is in the document. Write in {language}."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": text[:120000]}]


def build_finalise_messages(
    *, language: str, state: dict, transcript: str, minutes: list[dict]
) -> list[dict]:
    system = (
        "You finalise the minutes of a business meeting. You are given the live state and the "
        f"full transcript. Write in {language}. Merge duplicates, fix owners, add missed items "
        "with evidence. Never change items that are locked. Output json only with this shape: "
        '{"summary":["<=5 bullets"],"minutes":[{"item_id":"...","body_md":"..."}],'
        '"decisions":[{"id":"...","text":"..."}],"actions":[{"id":"...","title":"...","owner":"..."|null,'
        '"due":"YYYY-MM-DD"|null}],"next_agenda":[{"title":"...","timebox_minutes":int|null}],'
        '"required_next":[{"name":"...","reason":"..."}],"changes":[{"kind":"...","id":"...","note":"..."}]}'
    )
    user = (
        f"STATE: {json.dumps(state, ensure_ascii=False)}\n"
        f"MINUTES: {json.dumps(minutes, ensure_ascii=False)}\n"
        f"TRANSCRIPT:\n{transcript[-120000:]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]

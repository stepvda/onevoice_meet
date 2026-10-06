"""Spoken commands of the chair and co-hosts (FDD v3.2).

"Go to the next agenda point", "move to item 4" and "end this meeting" are
acted on as soon as the live transcript has them, not left to the
interpretation model (which reads them as one cue among many and may wait for
the talk to confirm). Moves are announced like an AI move, with Undo; ending
asks the chair to confirm in a dialog.
"""
from __future__ import annotations

import logging
import re
import time

log = logging.getLogger("app.meetpp")

_VERB = r"(?:go|move|skip|jump|proceed|continue|carry\s+on|let'?s\s+(?:go|move|continue))"
_POINT = r"(?:agenda\s+)?(?:point|item|topic|section|subject)s?"
NEXT_RE = re.compile(
    rf"\b{_VERB}\b[^.?!]{{0,25}}?\bnext\b[^.?!]{{0,12}}?\b{_POINT}\b"
    rf"|^\W*(?:ok(?:ay)?\W+|right\W+|so\W+)?(?:the\s+)?next\s+{_POINT}\b",
    re.IGNORECASE,
)
_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}
GOTO_RE = re.compile(
    rf"\b{_VERB}\b[^.?!]{{0,12}}?\bto\s+(?:agenda\s+)?(?:point|item)\s+(?:number\s+)?"
    r"(\d{1,2}|" + "|".join(_NUMBERS) + r")\b",
    re.IGNORECASE,
)
END_RE = re.compile(
    r"\b(?:end|close|finish|conclude|adjourn|stop)\b[^.?!]{0,12}?\b(?:this|the|our)\s+meeting\b"
    r"(?!\s+(?:report|minutes|notes|agenda|room|link|invite|invitation|series|recording))"
    r"|\b(?:this|the)\s+meeting\s+is\s+(?:now\s+)?(?:closed|adjourned|over|ended)\b"
    r"|\bi\s+declare\s+(?:this|the)\s+meeting\s+(?:closed|adjourned)\b",
    re.IGNORECASE,
)
# "Before we move to the next item…", "we can't end the meeting yet".
_NOT_NOW = re.compile(r"\b(?:not|never|before|until|once|after|when|if|later)\b|n't\b", re.IGNORECASE)

COOLDOWN_SECONDS = 15
_last: dict[tuple[str, str], float] = {}


def detect(text: str) -> tuple[str, int | None] | None:
    """("next", None), ("goto", n) or ("end", None) for a command, else None."""
    t = text or ""
    for kind, rx in (("end", END_RE), ("goto", GOTO_RE), ("next", NEXT_RE)):
        m = rx.search(t)
        if not m:
            continue
        lead = t[max(0, m.start() - 30):m.start()]
        if _NOT_NOW.search(lead):
            continue
        if kind == "goto":
            raw = m.group(1).lower()
            return ("goto", int(raw) if raw.isdigit() else _NUMBERS[raw])
        return (kind, None)
    return None


def _cooled(session_id: str, kind: str) -> bool:
    now = time.monotonic()
    if now - _last.get((session_id, kind), -1e9) < COOLDOWN_SECONDS:
        return False
    _last[(session_id, kind)] = now
    return True


async def handle(session_id: str, heard: list[tuple[str, str]]) -> None:
    """`heard`: (identity, text) of new transcript lines from the chair or a
    co-host. Never raises."""
    from app.db import SessionLocal
    from app.models import Meeting
    from app.meetpp import bus, outline as outline_mod, runtime
    from app.meetpp.models import MeetppSession

    for identity, text in heard:
        cmd = detect(text)
        if cmd is None or not _cooled(session_id, cmd[0]):
            continue
        kind, number = cmd
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, session_id)
            if session is None or session.status not in ("running", "paused"):
                return
            meeting = db.get(Meeting, session.meeting_id)
            if kind == "end":
                await bus.send(
                    meeting.room_name if meeting else None,
                    bus.message("end_request", session_id, heard=text[:300], identity=identity),
                    runtime.chair_identities(meeting),
                )
                log.info("MEETPP_COMMAND sid=%s kind=end by=%s", session_id, identity)
                continue
            o = outline_mod.load(db, session_id)
            if kind == "next":
                target = outline_mod.next_target(o, session)
            else:
                target = next(
                    (s for s in o.nav() if s.kind == "agenda" and o.numbers.get(s.id) == str(number)), None
                )
            if target is None or target.id == session.live_section_id:
                continue
            res = outline_mod.move(db, session, target, by="ai")
            await runtime._after_move(
                db, session, res, by="ai", compose_delay=outline_mod.AI_UNDO_SECONDS + 1,
                subtitle=f"Heard: “{text[:80]}” — the chair can undo",
            )
            runtime.runtime.poke(session_id, "chair")
            log.info("MEETPP_COMMAND sid=%s kind=%s to=%s by=%s", session_id, kind, target.id, identity)
        except Exception:  # noqa: BLE001 — a command must never break ingest
            log.exception("meetpp: spoken command failed for %s", session_id)
        finally:
            db.close()

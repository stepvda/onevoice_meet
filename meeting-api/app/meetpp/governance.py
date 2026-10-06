"""Formal-meeting rules: voting roster, quorum, vote records and the wording
used in the report (FDD §8.4, §8.6, §9.3; contract §7)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.meetpp import util
from app.meetpp.models import (
    MeetppAttendee,
    MeetppBallot,
    MeetppDecision,
    MeetppRoster,
    MeetppSeries,
    MeetppSession,
    MeetppVote,
)

MEETING_TYPES = ("informal", "board", "general_assembly")
MAJORITY_RULES = ("ordinary", "unanimous", "two_thirds", "four_fifths")
VOTE_METHODS = ("voice", "show_of_hands", "assent", "consensus", "roll_call")
CHOICES = ("for", "against", "abstain", "not_recorded")

MAJORITY_LABELS = {
    "ordinary": "Simple majority",
    "unanimous": "Unanimity",
    "two_thirds": "Two-thirds majority",
    "four_fifths": "Four-fifths majority",
}
TYPE_LABELS = {"informal": "Meeting", "board": "Board meeting", "general_assembly": "General assembly"}
BODY_LABELS = {"board": "Board", "general_assembly": "General assembly", "informal": "Meeting"}
RESULT_LABELS = {"adopted": "Adopted", "rejected": "Rejected"}
CHOICE_LABELS = {"for": "For", "against": "Against", "abstain": "Abstain", "not_recorded": "Not recorded"}
_RULE_PHRASE = {
    "ordinary": ("on an ordinary majority of the votes cast", "an ordinary majority of the votes cast"),
    "unanimous": ("unanimously", "unanimity"),
    "two_thirds": ("on a two-thirds majority of the votes cast", "a two-thirds majority of the votes cast"),
    "four_fifths": ("on a four-fifths majority of the votes cast", "a four-fifths majority of the votes cast"),
}
_BODY_NOUN = {"board": "the board", "general_assembly": "the general assembly", "informal": "the meeting"}
PRESENT_STATUSES = ("present", "represented")


def is_formal(series: MeetppSeries | None) -> bool:
    return bool(series) and series.meeting_type in ("board", "general_assembly")


def norm_choice(value) -> str:
    v = str(value or "").strip().lower().replace(" ", "_")
    return {
        "yes": "for", "aye": "for", "in_favour": "for", "in_favor": "for", "favour": "for", "favor": "for",
        "no": "against", "nay": "against", "abstention": "abstain", "abstained": "abstain",
        "": "not_recorded", "none": "not_recorded", "unknown": "not_recorded",
    }.get(v, v if v in CHOICES else "not_recorded")


def voting_keys(db: Session, session: MeetppSession) -> tuple[set[str], dict[str, MeetppAttendee]]:
    """Voting members for this session: the attendee row decides when the
    person has one, else the series roster."""
    attendees = db.query(MeetppAttendee).filter_by(session_id=session.id).all()
    by_key = {a.person_key: a for a in attendees}
    keys: set[str] = set()
    for r in db.query(MeetppRoster).filter_by(series_id=session.series_id, active=True).all():
        a = by_key.get(r.person_key)
        if (a.voting if a is not None else r.voting):
            keys.add(r.person_key)
    for a in attendees:
        if a.voting:
            keys.add(a.person_key)
        else:
            keys.discard(a.person_key)
    return keys, by_key


def quorum(db: Session, session: MeetppSession, series: MeetppSeries | None = None) -> dict:
    series = series or db.get(MeetppSeries, session.series_id)
    keys, by_key = voting_keys(db, session)
    total = len(keys)
    present = sum(1 for k in keys if by_key.get(k) is not None and by_key[k].status in PRESENT_STATUSES)
    required = series.quorum_required if series and series.quorum_required else (total // 2 + 1 if total else 0)
    return {"required": required, "voting_present": present, "voting_total": total, "met": total > 0 and present >= required}


def compute_result(rule: str, n_for: int | None, n_against: int | None, n_abstain: int | None) -> str | None:
    if n_for is None and n_against is None:
        return None
    f = int(n_for or 0)
    a = int(n_against or 0)
    cast = f + a
    if f <= 0:
        return "rejected"
    if rule == "unanimous":
        ok = a == 0
    elif rule == "two_thirds":
        ok = 3 * f >= 2 * cast
    elif rule == "four_fifths":
        ok = 5 * f >= 4 * cast
    else:
        ok = f > a
    return "adopted" if ok else "rejected"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def outcome_sentence(
    *,
    result: str | None,
    meeting_type: str,
    rule: str,
    n_for: int | None,
    n_against: int | None,
    n_abstain: int | None,
    eligible: int | None,
    present: int | None,
    quorum_required: int | None,
    quorum_met: bool | None,
) -> str:
    body = _BODY_NOUN.get(meeting_type, "the meeting")
    adopted_phrase, required_phrase = _RULE_PHRASE.get(rule, _RULE_PHRASE["ordinary"])
    parts = []
    if result == "adopted":
        parts.append(f"Adopted by {body} {adopted_phrase}.")
    elif result == "rejected":
        parts.append(f"Rejected by {body}: {required_phrase} was not reached.")
    if n_for is not None or n_against is not None or n_abstain is not None:
        f, a, ab = int(n_for or 0), int(n_against or 0), int(n_abstain or 0)
        parts.append(
            f"{_plural(f, 'vote', 'votes')} in favour, {a} against, {_plural(ab, 'abstention', 'abstentions')}."
        )
    if eligible is not None:
        noun = ("director", "directors") if meeting_type == "board" else ("member", "members")
        seg = f"{_plural(int(eligible), *noun)} entitled to vote"
        if present is not None:
            seg += f", {int(present)} present or represented"
        if quorum_required is not None:
            seg += f" (quorum {int(quorum_required)})"
        parts.append(seg + ".")
    if quorum_met is False:
        parts.append("The quorum was not met.")
    return " ".join(parts)


def match_person(db: Session, session: MeetppSession, name: str | None) -> tuple[str, str | None]:
    """(display name, person_key) for a spoken name: attendees, then roster."""
    raw = (name or "").strip().strip('"“”\'')
    if not raw:
        return "", None
    needle = util.norm_name(raw)
    candidates: list[tuple[str, str]] = []
    for a in db.query(MeetppAttendee).filter_by(session_id=session.id).all():
        candidates.append((a.display_name, a.person_key))
    for r in db.query(MeetppRoster).filter_by(series_id=session.series_id).all():
        candidates.append((r.display_name, r.person_key))
        if r.username and util.norm_name(r.username.lstrip("@")) == needle:
            return r.display_name, r.person_key
    for display, key in candidates:
        if util.norm_name(display) == needle:
            return display, key
    # First name or partial match ("Robin" → "Robin Hale") when unambiguous.
    partial = {key: display for display, key in candidates if needle and (
        util.norm_name(display).startswith(needle + " ") or (" " + needle) in util.norm_name(display)
    )}
    if len(partial) == 1:
        key, display = next(iter(partial.items()))
        return display, key
    return raw[:200], None


def count_exceeds_present(vote: MeetppVote | None) -> bool:
    """More votes counted than voting members present or represented."""
    if vote is None or not vote.present_count:
        return False
    cast = int(vote.tally_for or 0) + int(vote.tally_against or 0) + int(vote.tally_abstain or 0)
    return cast > int(vote.present_count)


def _known_keys(db: Session, session: MeetppSession) -> dict[str, str]:
    """Person key as a client may send it back (room data shows guests by an
    alias) → the stored key."""
    keys = [a.person_key for a in db.query(MeetppAttendee).filter_by(session_id=session.id).all()]
    keys += [r.person_key for r in db.query(MeetppRoster).filter_by(series_id=session.series_id).all()]
    out: dict[str, str] = {}
    for k in keys:
        out[k] = k
        out[util.public_person_key(session.id, k) or k] = k
    return out


def apply_vote(
    db: Session,
    session: MeetppSession,
    decision: MeetppDecision,
    data: dict,
    *,
    confirmed: bool | None = None,
    ai: bool = False,
) -> MeetppVote:
    """Create or update the vote record of a decision. Eligible/present and
    quorum are always recomputed server-side; the result follows the series'
    majority rule when tallies are known. A count from the model (`ai`) that
    exceeds the voting members present is kept but decides nothing: the
    chair checks it in review."""
    series = db.get(MeetppSeries, session.series_id)
    vote = db.query(MeetppVote).filter_by(decision_id=decision.id).first()
    if vote is None:
        vote = MeetppVote(id=util.ulid(), decision_id=decision.id, method="assent")
        db.add(vote)
        db.flush()
    method = str(data.get("method") or vote.method or "assent").strip().lower().replace(" ", "_")
    if method in ("show_of_hand", "hands"):
        method = "show_of_hands"
    vote.method = method if method in VOTE_METHODS else "assent"
    if data.get("question"):
        vote.question = util.truncate(data.get("question"), 500)
    elif not vote.question:
        vote.question = util.truncate(decision.title, 500)

    keys, by_key = voting_keys(db, session)
    ballots_in = data.get("ballots")
    ballots: list[dict] | None = None
    if isinstance(ballots_in, list) and ballots_in:
        ballots = []
        known = _known_keys(db, session)
        for b in ballots_in[:100]:
            if not isinstance(b, dict):
                continue
            name = b.get("name") or b.get("person")
            display, key = match_person(db, session, name)
            if not display:
                continue
            ballots.append(
                {
                    "name": display,
                    "person_key": known.get(str(b.get("person_key") or ""), key),
                    "choice": norm_choice(b.get("choice")),
                    "cast_by": util.truncate(b.get("cast_by"), 200) or display,
                    "proxy": bool(b.get("proxy")),
                }
            )
    elif vote.method in ("assent", "consensus") and not db.query(MeetppBallot).filter_by(vote_id=vote.id).count():
        # Assent: the present voting members agreed; the rest is not recorded.
        ballots = []
        for k in sorted(keys, key=lambda k: (by_key[k].display_name if k in by_key else k)):
            a = by_key.get(k)
            name = a.display_name if a is not None else _roster_name(db, session, k)
            here = a is not None and a.status in PRESENT_STATUSES
            ballots.append({"name": name, "person_key": k, "choice": "for" if here else "not_recorded", "cast_by": name if here else None, "proxy": False})
    if ballots is not None:
        db.query(MeetppBallot).filter_by(vote_id=vote.id).delete(synchronize_session=False)
        for b in ballots:
            db.add(MeetppBallot(vote_id=vote.id, **b))

    def _int(v):
        try:
            return max(0, int(v)) if v is not None and v != "" else None
        except (TypeError, ValueError):
            return None

    tf, ta, tab = _int(data.get("for")), _int(data.get("against")), _int(data.get("abstain"))
    q = quorum(db, session, series)
    if vote.method in ("assent", "consensus") and not any((tf, ta, tab)):
        # "Taken with the assent of those present": no count was given (the
        # model sends zeros), so the count is the assenting voters.
        tf = ta = tab = None
    if tf is None and ta is None and tab is None and ballots and any(b["choice"] != "not_recorded" for b in ballots):
        tf = sum(1 for b in ballots if b["choice"] == "for")
        ta = sum(1 for b in ballots if b["choice"] == "against")
        tab = sum(1 for b in ballots if b["choice"] == "abstain")
    # Whose count this is: the model's, unless a person entered or confirmed it
    # (a person's count is never rewritten: they may know of votes we do not).
    ai_count = ai or (tf is None and ta is None and tab is None and not decision.locked and not vote.confirmed)
    if tf is not None or ta is not None or tab is not None:
        vote.tally_for, vote.tally_against, vote.tally_abstain = tf or 0, ta or 0, tab or 0
    vote.eligible_count = q["voting_total"]
    vote.present_count = q["voting_present"]
    vote.quorum_required = q["required"]
    vote.quorum_met = q["met"]
    rule = series.majority_rule if series else "ordinary"
    if ai_count and count_exceeds_present(vote):
        result = None  # an impossible count from the model decides nothing
    else:
        result = compute_result(rule, vote.tally_for, vote.tally_against, vote.tally_abstain)
    if result is None and decision.status in ("adopted", "rejected"):
        result = decision.status
    vote.result = result
    vote.outcome_note = outcome_sentence(
        result=result,
        meeting_type=series.meeting_type if series else "informal",
        rule=rule,
        n_for=vote.tally_for,
        n_against=vote.tally_against,
        n_abstain=vote.tally_abstain,
        eligible=vote.eligible_count,
        present=vote.present_count,
        quorum_required=vote.quorum_required,
        quorum_met=vote.quorum_met if is_formal(series) else None,
    )
    if confirmed is not None:
        vote.confirmed = confirmed
    db.flush()
    return vote


def _roster_name(db: Session, session: MeetppSession, key: str) -> str:
    r = db.query(MeetppRoster).filter_by(series_id=session.series_id, person_key=key).first()
    return r.display_name if r else key

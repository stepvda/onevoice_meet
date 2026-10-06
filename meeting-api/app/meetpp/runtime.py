"""Session runtime (FDD §7.5, §8; contract §4, §6.1).

One `SessionRunner` task per active session inside meeting-api, guarded by a
Redis lease and resumable from the database (running, paused and finalising
sessions are resumed on startup). The runner keeps the agent in step
(accepted identities and pause every 20 s; restart when missing from
/health), runs interpretation ticks (every 12 s with new speech, at once on a
cue phrase or a chair move; single-flight), nudges overrunning timeboxes and
drives the finalisation jobs.

Everything that changes state goes: mutate → atomic version bump → commit →
broadcast (bus.publish_changes). Session rows are re-read after every await.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.livekit_client import mint_agent_token
from app.meetpp import agent as agent_mod
from app.meetpp import bus, compose, governance, llm, ops, outline as outline_mod, prompts, util
from app.meetpp.models import (
    MeetppActionReport,
    MeetppAttendee,
    MeetppConsent,
    MeetppDecision,
    MeetppMinute,
    MeetppOp,
    MeetppRoster,
    MeetppSegment,
    MeetppSeries,
    MeetppSession,
)
from app.models import Meeting, User

log = logging.getLogger("app.meetpp")

CUE_RE = re.compile(
    r"\b(next (item|point|topic)|moving on|move on to|let'?s move on|on to (item|point)|agreed|we agree|"
    r"all in favou?r|in favou?r|any objections|action (for|point)|we (have )?decided|i propose|i move that|"
    r"resolved|motion|second(ed)? (the|that)|let'?s vote|put (it )?to (a|the) vote|carried|approved|"
    r"any other business|apologies)\b",
    re.IGNORECASE,
)
WINDOW_SECONDS = 120
CATCHUP_SECONDS = 180
THROTTLED_TICK_SECONDS = 30
RECONCILE_SECONDS = 20
LEASE_TTL = 30
LEASE_RENEW_SECONDS = 10
ECHO_WINDOW_SECONDS = 10
TOPIC_MIN = 0.6
TOPIC_SURE = 0.8
ADVANCE_AUTO = 0.85
PROPOSAL_MIN = 0.6
# Topic-based move: a later item that wins LATER_HITS of the last LATER_WINDOW
# ticks (about 40 s of talk) becomes live even when no tick was sure of it —
# provided a cue for it was heard (an `advance` of at least ADVANCE_HINT_MIN in
# the last ADVANCE_HINT_SECONDS: "number four", "on to the budget"). Talk
# drifting to a later item's subject without a cue (a member previewing the
# next point's subject under the current one) moves only after SILENT_MOVE_TICKS
# consecutive ticks (about 5 minutes): the chair moved on without saying so.
LATER_WINDOW = 4
LATER_HITS = 3
ADVANCE_HINT_MIN = 0.5
ADVANCE_HINT_SECONDS = 150
SILENT_MOVE_TICKS = 25
NOT_NOW_SECONDS = 180
# A cue phrase ticks at once, but not more often than this.
CUE_MIN_SPACING_SECONDS = 3
ACTIVE_STATUSES = ("running", "paused")

# sid -> (announcement text, monotonic time) for the echo guard.
_last_announce: dict[str, tuple[str, float]] = {}


# ─── helpers ────────────────────────────────────────────────────────────────


def chair_identities(meeting: Meeting | None) -> list[str]:
    if meeting is None:
        return []
    from app.routes.meetings import _cohost_set

    subs = [meeting.owner_user_id, *sorted(_cohost_set(meeting))]
    return [f"user-{s}" for s in subs if s]


def _room(db, session: MeetppSession) -> str | None:
    meeting = db.get(Meeting, session.meeting_id)
    return meeting.room_name if meeting else None


def _seg_time(s: MeetppSegment):
    return util.aware(s.t_start or s.t_end or s.created_at)


def _topic_state(session: MeetppSession) -> dict:
    return util.loads(session.topic_state_json, {})


def _save_topic_state(session: MeetppSession, st: dict) -> None:
    session.topic_state_json = util.dumps(st)


def _echo(sid: str, text: str) -> bool:
    last = _last_announce.get(sid)
    if not last or time.monotonic() - last[1] > ECHO_WINDOW_SECONDS:
        return False
    return util.jaccard(text, last[0]) >= 0.6 or (util.norm_name(last[0]) in util.norm_name(text) and len(text) < 2 * len(last[0]))


def identity_map(db, session_id: str) -> dict[str, str]:
    """LiveKit identity → person_key (attendee identities and consent posts)."""
    out: dict[str, str] = {}
    for a in db.query(MeetppAttendee).filter_by(session_id=session_id).all():
        for ident in util.loads(a.identities_json, []):
            out[str(ident)] = a.person_key
    for c in db.query(MeetppConsent).filter_by(session_id=session_id).all():
        out.setdefault(c.identity, c.person_key)
    return out


def accepted_identities(db, session_id: str) -> list[str]:
    consents = {c.person_key: c for c in db.query(MeetppConsent).filter_by(session_id=session_id).all()}
    out: set[str] = set()
    for key, c in consents.items():
        if c.decision != "accept":
            continue
        out.add(c.identity)
    for a in db.query(MeetppAttendee).filter_by(session_id=session_id).all():
        c = consents.get(a.person_key)
        if c is not None and c.decision == "accept" and not a.opted_out:
            out.update(str(i) for i in util.loads(a.identities_json, []))
    return sorted(out)


def glossary(db, session: MeetppSession) -> str:
    words: list[str] = []
    o = outline_mod.load(db, session.id)
    for s in o.flat:
        if s.source != "template":
            words.append(s.title)
        if s.presenter:
            words.append(s.presenter)
    for a in db.query(MeetppAttendee).filter_by(session_id=session.id).all():
        words.append(a.display_name)
    for r in db.query(MeetppRoster).filter_by(series_id=session.series_id, active=True).all():
        words.append(r.display_name)
    seen: list[str] = []
    for w in words:
        w = (w or "").strip()
        if w and w not in seen:
            seen.append(w)
    return ", ".join(seen)[:600]


def _user_profile(db, sub: str) -> dict:
    """Display name, username and e-mail of a signed-in participant."""
    user = None
    if sub.startswith("m:"):
        try:
            user = db.query(User).filter_by(id=int(sub[2:]), kind="native").first()
        except ValueError:
            user = None
    else:
        user = db.query(User).filter_by(external_id=sub, kind="sso").first()
    if user is None:
        return {}
    return {"name": user.name, "username": user.username, "email": user.email}


def _merge_attendee(db, keep: MeetppAttendee, other: MeetppAttendee) -> None:
    ids = util.loads(keep.identities_json, [])
    for i in util.loads(other.identities_json, []):
        if i not in ids:
            ids.append(i)
    keep.identities_json = util.dumps(ids)
    keep.talk_seconds = float(keep.talk_seconds or 0) + float(other.talk_seconds or 0)
    keep.online = keep.online or other.online
    keep.first_joined_at = keep.first_joined_at or other.first_joined_at
    if keep.status in ("not_registered", "absent") and other.status == "present":
        keep.status = "present"
    db.query(MeetppSegment).filter_by(session_id=keep.session_id, person_key=other.person_key).update(
        {MeetppSegment.person_key: keep.person_key}, synchronize_session=False
    )
    db.delete(other)


def _has_established_voting_members(db, session: MeetppSession) -> bool:
    """Voting members the series had before this meeting: seeded from the
    previous report or first seen in an earlier session."""
    return (
        db.query(MeetppRoster)
        .filter(
            MeetppRoster.series_id == session.series_id,
            MeetppRoster.voting.is_(True),
            or_(MeetppRoster.first_session_id.is_(None), MeetppRoster.first_session_id != session.id),
        )
        .first()
        is not None
    )


def upsert_roster(db, session: MeetppSession, key: str, name: str, *, username: str | None = None, email: str | None = None) -> MeetppRoster | None:
    """Roster row for a participant (FDD §8.6). Seeded rows ("name:" keys from
    the previous report) are matched by username or name and re-keyed."""
    if not (key.startswith("sub:") or key.startswith("guest:")):
        return None
    r = db.query(MeetppRoster).filter_by(series_id=session.series_id, person_key=key).first()
    if r is None:
        needle = util.norm_name(name)
        for cand in db.query(MeetppRoster).filter(
            MeetppRoster.series_id == session.series_id, MeetppRoster.person_key.like("name:%")
        ).all():
            if (username and cand.username and cand.username.lstrip("@").lower() == username.lstrip("@").lower()) or (
                needle and util.norm_name(cand.display_name) == needle
            ):
                cand.person_key = key
                r = cand
                break
    if r is None:
        r = MeetppRoster(
            id=util.ulid(),
            series_id=session.series_id,
            person_key=key,
            display_name=(name or key)[:200],
            username=username,
            email=email,
            # Signed-in participants vote by default only while the series has
            # no voting members from before this meeting (its first meeting:
            # everyone signed in); later newcomers (guests with an account,
            # invitees) start non-voting until the chair toggles them (FDD §8.6).
            voting=key.startswith("sub:") and not _has_established_voting_members(db, session),
            first_session_id=session.id,
            active=True,
        )
        db.add(r)
    else:
        r.last_seen_at = util.now()
        if username and not r.username:
            r.username = username
        if email and not r.email:
            r.email = email
    db.flush()
    return r


def find_attendee(db, session: MeetppSession, *, key: str | None, identity: str | None, name: str | None) -> MeetppAttendee | None:
    q = db.query(MeetppAttendee).filter_by(session_id=session.id)
    if key:
        a = q.filter_by(person_key=key).first()
        if a is not None:
            return a
    if identity:
        for a in q.all():
            if identity in util.loads(a.identities_json, []):
                return a
    return None


def _seeded_match(db, session: MeetppSession, name: str | None, username: str | None) -> MeetppAttendee | None:
    needle = util.norm_name(name)
    for a in db.query(MeetppAttendee).filter(
        MeetppAttendee.session_id == session.id, MeetppAttendee.person_key.like("name:%")
    ).all():
        if (username and a.username and a.username.lstrip("@").lower() == username.lstrip("@").lower()) or (
            needle and util.norm_name(a.display_name) == needle
        ):
            return a
    return None


def ensure_attendee(db, session: MeetppSession, *, key: str, identity: str | None, name: str | None) -> MeetppAttendee:
    """Attendee row for a person, merging a provisional identity row or a
    seeded roster row into the person's key."""
    profile = _user_profile(db, key[4:]) if key.startswith("sub:") else {}
    display = (name or profile.get("name") or key)[:200]
    a = db.query(MeetppAttendee).filter_by(session_id=session.id, person_key=key).first()
    provisional = None
    if identity:
        provisional = db.query(MeetppAttendee).filter_by(session_id=session.id, person_key=f"id:{identity}").first()
    seeded = _seeded_match(db, session, display, profile.get("username")) if a is None else None
    if a is None and seeded is not None:
        seeded.person_key = key
        a = seeded
    if a is None and provisional is not None:
        provisional.person_key = key
        a = provisional
        provisional = None
    created = False
    if a is None:
        a = MeetppAttendee(
            id=util.ulid(),
            session_id=session.id,
            person_key=key,
            display_name=display,
            identities_json="[]",
            status="present",
            voting=key.startswith("sub:"),
        )
        db.add(a)
        db.flush()
        created = True
    if provisional is not None and provisional.id != a.id:
        _merge_attendee(db, a, provisional)
    if not a.username and profile.get("username"):
        a.username = profile["username"]
    if not a.email and profile.get("email"):
        a.email = profile["email"]
    roster = upsert_roster(db, session, key, a.display_name, username=a.username, email=a.email)
    if roster is not None:
        a.roster_id = roster.id
        if created:
            a.voting = bool(roster.voting)
        if not a.email and roster.email:
            a.email = roster.email
        if not a.username and roster.username:
            a.username = roster.username
    if identity:
        ids = util.loads(a.identities_json, [])
        if identity not in ids:
            ids.append(identity)
            a.identities_json = util.dumps(ids[-20:])
    db.flush()
    return a


def expected_attendees(db, session: MeetppSession) -> set[str]:
    """Roster members get an attendee row (status not_registered) so absentees
    and the quorum are known before anyone joins (contract §1a)."""
    changed: set[str] = set()
    have = {a.person_key: a for a in db.query(MeetppAttendee).filter_by(session_id=session.id).all()}
    for r in db.query(MeetppRoster).filter_by(series_id=session.series_id, active=True).all():
        if r.person_key in have:
            continue
        a = MeetppAttendee(
            id=util.ulid(),
            session_id=session.id,
            person_key=r.person_key,
            roster_id=r.id,
            display_name=r.display_name,
            username=r.username,
            email=r.email,
            identities_json="[]",
            status="not_registered",
            voting=bool(r.voting),
        )
        db.add(a)
        changed.add(a.id)
    db.flush()
    return changed


# ─── announcements and moves ───────────────────────────────────────────────


async def announce(
    session_id: str,
    *,
    kind: str,
    title: str,
    subtitle: str = "",
    speech: str | None = None,
    destination: list[str] | None = None,
) -> dict:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            return {}
        room = _room(db, session)
        speak = ops._settings(session)["speak"]
    finally:
        db.close()
    audio_url = None
    if speech and speak:
        clip = await agent_mod.client.tts(speech, settings.meetpp_tts_voice)
        if clip and re.fullmatch(r"[0-9a-f]{16,64}", str(clip.get("hash") or "")):
            audio_url = f"/api/v1/meetpp/tts/{clip['hash']}.ogg"
    if speech:
        _last_announce[session_id] = (speech, time.monotonic())
    msg = bus.message("announce", session_id, aid=util.ulid(), kind=kind, title=title[:120], subtitle=(subtitle or "")[:160], audio_url=audio_url)
    await bus.send(room, msg, destination)
    return msg


def _position_texts(o: outline_mod.Outline, target, *, back: bool) -> tuple[str, str]:
    num = o.numbers.get(target.id)
    title = f"{num} · {target.title}" if num else target.title
    if back:
        speech = f"Back to item {num}: {target.title}." if num else f"Back to {target.title}."
    else:
        speech = f"Moving on to item {num}: {target.title}." if num else f"Moving on to {target.title}."
    return title, speech


async def _after_move(db, session: MeetppSession, res: outline_mod.MoveResult, *, by: str, compose_delay: float) -> int | None:
    changes = ops.Changes(session=True, sections=set(res.changed), minutes=set(res.minutes_changed))
    changes.activate("topic", res.live_id)

    def _position(version: int) -> list[dict]:
        return [
            bus.message(
                "position",
                session.id,
                version=version,
                live_section_id=res.live_id,
                prev_section_id=res.prev_id,
                by="ai" if by == "ai" else "chair",
                undo_until=res.undo_until,
            )
        ]

    # position, then the state delta with the same version (sections + session).
    version = await bus.publish_changes(db, session, changes, lead=_position)
    o = outline_mod.load(db, session.id)
    target = o.by_id.get(res.live_id)
    prev = o.by_id.get(res.prev_id or "")
    back = prev is not None and o.later(prev, target) if target is not None else False
    if target is not None:
        title, speech = _position_texts(o, target, back=back)
        subtitle = "AI moved the meeting on — the chair can undo" if by == "ai" else ""
        asyncio.get_running_loop().create_task(
            announce(session.id, kind="position", title=title, subtitle=subtitle, speech=speech)
        )
    for sid in res.closed:
        compose.schedule_section(session.id, sid, delay=compose_delay)
    log.info("MEETPP_POSITION sid=%s from=%s to=%s by=%s version=%s", session.id, res.prev_id, res.live_id, by, version)
    return version


class PositionError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def chair_move(session_id: str, action: str, section_id: str | None = None) -> dict:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            raise PositionError(404, "session not found")
        if session.status not in ACTIVE_STATUSES:
            raise PositionError(409, f"session is {session.status}")
        o = outline_mod.load(db, session_id)
        if action == "next":
            target = outline_mod.next_target(o, session)
        elif action == "back":
            target = outline_mod.prev_target(o, session)
        elif action == "move":
            target = o.by_id.get(section_id or "")
            if target is None:
                raise PositionError(404, "section not found")
        else:
            raise PositionError(400, "action must be next, back or move")
        if target is None:
            raise PositionError(409, f"no section to move to ({action})")
        res = outline_mod.move(db, session, target, by="chair")
        version = await _after_move(db, session, res, by="chair", compose_delay=0)
        runtime.poke(session_id, "chair")
        return {"live_section_id": session.live_section_id, "version": version or session.state_version}
    finally:
        db.close()


async def undo_move(session_id: str) -> dict:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            raise PositionError(404, "session not found")
        res = outline_mod.undo(db, session)
        if res is None:
            db.commit()
            raise PositionError(409, "nothing to undo")
        version = await _after_move(db, session, res, by="chair", compose_delay=0)
        return {"live_section_id": session.live_section_id, "version": version or session.state_version}
    finally:
        db.close()


async def answer_proposal(session_id: str, pid: str, accept: bool) -> dict:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            raise PositionError(404, "session not found")
        proposal = util.loads(session.proposal_json, {})
        if not proposal or proposal.get("pid") != pid:
            raise PositionError(409, "no such proposal")
        target_id = proposal.get("to")
        if not accept:
            st = _topic_state(session)
            notnow = st.get("notnow") or {}
            notnow[target_id] = util.iso(util.now() + timedelta(seconds=NOT_NOW_SECONDS))
            st["notnow"] = notnow
            _save_topic_state(session, st)
            session.proposal_json = None
            await bus.publish_changes(db, session, ops.Changes(session=True))
            return {"ok": True}
    finally:
        db.close()
    await chair_move(session_id, "move", target_id)
    return {"ok": True}


# ─── ingest (agent → meeting-api) ──────────────────────────────────────────


async def ingest(session_id: str, body: dict) -> dict:
    """Store segments (consent per person, echo guard), refinements and gaps,
    and broadcast caption / caption-update / gap messages."""
    db = SessionLocal()
    seqs: dict[str, int] = {}
    messages: list[dict] = []
    cue = False
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            return {"ok": False, "seqs": {}}
        room = _room(db, session)
        ident_map = identity_map(db, session_id)
        consents = {c.person_key: c.decision for c in db.query(MeetppConsent).filter_by(session_id=session_id).all()}
        max_seq = db.query(func.max(MeetppSegment.seq)).filter(MeetppSegment.session_id == session_id).scalar() or 0
        accepting = session.status in ("running", "paused", "finalising")
        talk: dict[str, float] = {}
        for raw in (body.get("segments") or []) if accepting else []:
            if not isinstance(raw, dict):
                continue
            uid = str(raw.get("utterance_id") or "")[:64] or None
            if uid:
                existing = db.query(MeetppSegment).filter_by(session_id=session_id, utterance_id=uid).first()
                if existing is not None:
                    seqs[uid] = existing.seq
                    continue
            identity = str(raw.get("identity") or "")
            text = str(raw.get("text") or "").strip()
            if not identity or not text:
                continue
            key = util.person_key_for_identity(identity) or ident_map.get(identity)
            if key is None or consents.get(key) != "accept":
                log.info("meetpp: dropped segment from %s (no consent)", identity)
                continue
            if _echo(session_id, text):
                log.info("meetpp: dropped segment from %s (announcement echo)", identity)
                continue
            max_seq += 1
            t_start = util.parse_dt(raw.get("t_start")) or util.now()
            t_end = util.parse_dt(raw.get("t_end")) or t_start
            seg = MeetppSegment(
                session_id=session_id,
                seq=max_seq,
                utterance_id=uid,
                identity=identity,
                person_key=key,
                name=util.truncate(raw.get("name"), 200) or identity,
                t_start=t_start,
                t_end=t_end,
                text=text,
                tier=1,
                lang=str(raw.get("lang") or "en")[:8],
                avg_logprob=raw.get("avg_logprob") if isinstance(raw.get("avg_logprob"), (int, float)) else None,
                no_speech_prob=raw.get("no_speech_prob") if isinstance(raw.get("no_speech_prob"), (int, float)) else None,
                section_id=session.live_section_id,
            )
            db.add(seg)
            if uid:
                seqs[uid] = max_seq
            talk[key] = talk.get(key, 0.0) + max(0.0, (t_end - t_start).total_seconds())
            cue = cue or bool(CUE_RE.search(text))
            messages.append(
                bus.message(
                    "caption", session_id, seq=max_seq, identity=identity, name=seg.name, person_key=key,
                    t_start=util.iso(t_start), text=text, tier=1,
                )
            )
        for raw in body.get("refinements") or []:
            if not isinstance(raw, dict) or not raw.get("utterance_id"):
                continue
            seg = db.query(MeetppSegment).filter_by(session_id=session_id, utterance_id=str(raw["utterance_id"])[:64]).first()
            text = str(raw.get("text") or "").strip()
            if seg is None or not text or seg.is_gap:
                continue
            if seg.text_refined == text:
                continue
            seg.text_refined = text
            seg.tier = 2
            seqs[seg.utterance_id] = seg.seq
            messages.append(bus.message("caption-update", session_id, seq=seg.seq, text=text, tier=2))
        for raw in body.get("gaps") or []:
            if not isinstance(raw, dict):
                continue
            max_seq += 1
            t_from = util.parse_dt(raw.get("t_from")) or util.now()
            t_to = util.parse_dt(raw.get("t_to")) or t_from
            name = util.truncate(raw.get("name"), 200)
            reason = util.truncate(raw.get("reason"), 200) or "transcription interrupted"
            db.add(
                MeetppSegment(
                    session_id=session_id,
                    seq=max_seq,
                    identity=str(raw.get("identity") or "meetpp-agent")[:200],
                    name=name,
                    t_start=t_from,
                    t_end=t_to,
                    text="",
                    is_gap=True,
                    gap_reason=reason,
                    section_id=session.live_section_id,
                )
            )
            payload = {"seq": max_seq, "t_from": util.iso(t_from), "t_to": util.iso(t_to), "reason": reason}
            if name:
                payload["name"] = name
            messages.append(bus.message("gap", session_id, **payload))
        for key, secs in talk.items():
            a = db.query(MeetppAttendee).filter_by(session_id=session_id, person_key=key).first()
            if a is not None:
                a.talk_seconds = float(a.talk_seconds or 0.0) + secs
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            log.warning("meetpp: segment seq collision for %s; agent will retry", session_id)
            return {"ok": False, "seqs": {}}
    finally:
        db.close()
    for msg in messages:
        await bus.send(room, msg)
    if messages:
        runtime.poke(session_id, "cue" if cue else None)
    return {"ok": True, "seqs": seqs}


async def presence(session_id: str, events: list) -> None:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            return
        changes = ops.Changes()
        ident_map = identity_map(db, session_id)
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            identity = str(ev.get("identity") or "")
            kind = str(ev.get("kind") or "standard").lower()
            if kind != "standard" or util.is_system_identity(identity):
                continue
            name = util.truncate(ev.get("name"), 200) or identity
            key = util.person_key_for_identity(identity) or ident_map.get(identity)
            if key is None:
                a = find_attendee(db, session, key=None, identity=identity, name=name)
                if a is None:
                    a = MeetppAttendee(
                        id=util.ulid(), session_id=session_id, person_key=f"id:{identity}", display_name=name,
                        identities_json=util.dumps([identity]), status="present", voting=False,
                    )
                    db.add(a)
                    db.flush()
            else:
                a = ensure_attendee(db, session, key=key, identity=identity, name=name)
            at = util.parse_dt(ev.get("at")) or util.now()
            if ev.get("event") == "connected":
                a.online = True
                if a.status in ("not_registered", "absent", "excused"):
                    a.status = "present"
                a.first_joined_at = a.first_joined_at or at
            elif ev.get("event") == "disconnected":
                ids = util.loads(a.identities_json, [])
                if not ids or ids[-1] == identity:
                    a.online = False
                a.last_left_at = at
            consent = db.query(MeetppConsent).filter_by(session_id=session_id, person_key=a.person_key).first()
            a.opted_out = bool(consent and consent.decision == "opt_out")
            changes.attendees.add(a.id)
        if changes.attendees:
            changes.quorum = True
            await bus.publish_changes(db, session, changes)
        else:
            db.commit()
    finally:
        db.close()


class ConsentError(Exception):
    pass


async def set_consent(session_id: str, identity: str, decision: str, person_key: str | None, name: str | None) -> dict:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            raise ConsentError("session not found")
        key = util.person_key_for_identity(identity)
        if key is None:
            if not util.valid_guest_key(person_key):
                raise ConsentError("person_key must be guest:<id> for anonymous participants")
            key = person_key
        row = db.query(MeetppConsent).filter_by(session_id=session_id, person_key=key).first()
        if row is None:
            row = MeetppConsent(session_id=session_id, person_key=key, identity=identity, decision=decision)
            db.add(row)
        else:
            row.identity = identity
            row.decision = decision
            row.updated_at = util.now()
        a = ensure_attendee(db, session, key=key, identity=identity, name=name)
        a.opted_out = decision == "opt_out"
        a.online = True
        if a.status in ("not_registered", "absent"):
            a.status = "present"
        a.first_joined_at = a.first_joined_at or util.now()
        changes = ops.Changes(attendees={a.id}, quorum=True)
        await bus.publish_changes(db, session, changes)
        accepted = accepted_identities(db, session_id)
        status = session.status
    finally:
        db.close()
    if status in ACTIVE_STATUSES:
        await agent_mod.client.patch(session_id, {"accepted_identities": accepted})
    return {"ok": True, "person_key": key}


async def agent_status(session_id: str, body: dict) -> None:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            return
        current = util.loads(session.agent_json, {})
        speakers = [
            {"name": sp.get("name"), "ok": bool(sp.get("ok", True))}
            for sp in (body.get("speakers") or [])
            if isinstance(sp, dict)
        ]
        current.update(
            {
                "status": body.get("status") or current.get("status") or "listening",
                "backlog_s": body.get("backlog_s") if isinstance(body.get("backlog_s"), (int, float)) else current.get("backlog_s", 0),
                "speakers": speakers,
                "tier2": body.get("tier2") or current.get("tier2") or "off",
                "rtf_p50": body.get("rtf_p50"),
                "at": util.iso(util.now()),
            }
        )
        if body.get("final_pass") in ("running", "done", "failed", "skipped"):
            current["final_pass"] = body["final_pass"]
            if body["final_pass"] == "done":
                session.final_pass_done = True
        session.agent_json = util.dumps(current)
        db.commit()
        room = _room(db, session)
        msg = bus.message(
            "agent", session_id, status=current["status"], backlog_s=current["backlog_s"],
            speakers=speakers, tier2=current["tier2"],
        )
    finally:
        db.close()
    await bus.send(room, msg)
    runtime.poke(session_id)


# ─── interpretation tick ───────────────────────────────────────────────────


def _record_gap(db, session: MeetppSession, first: int, last: int, reason: str) -> None:
    db.add(
        MeetppOp(
            session_id=session.id,
            version=int(session.state_version or 0),
            op_type="interpretation.gap",
            payload_json=util.dumps({"from_seq": first, "to_seq": last}),
            actor="ai",
            status="rejected",
            reason=reason[:300],
        )
    )
    log.warning("MEETPP_TICK sid=%s interpretation gap seq %s–%s: %s", session.id, first, last, reason)


def _prompt_data(db, session: MeetppSession, o: outline_mod.Outline, window: list[MeetppSegment], context: list[MeetppSegment]) -> dict:
    series = db.get(MeetppSeries, session.series_id)
    meeting = db.get(Meeting, session.meeting_id)
    pid = o.prompt_ids()
    live = o.by_id.get(session.live_section_id or "")
    topic = o.by_id.get(session.topic_section_id or "")
    nxt = outline_mod.next_target(o, session)
    # Agenda text for the live point and the next one: enough to tell when the
    # talk has reached the next point, even when the chair names it loosely.
    shown = {x.id for x in (o.top(live) if live is not None else None, nxt) if x is not None}
    outline_lines = []
    for s in o.flat:
        num = o.numbers.get(s.id)
        indent = "  " if s.parent_id else ""
        mark = " ← LIVE" if live is not None and s.id == live.id else (" ← NEXT" if nxt is not None and s.id == nxt.id else "")
        line = f"{indent}{pid[s.id]} · {(num + ' · ') if num else ''}{s.title} · {s.status}{mark}"
        if s.body and o.top(s).id in shown:
            # The live point in full, so that a phrase from one of its later
            # sub-points ("the next item there: the handbook is not a contract")
            # is not taken for the next point; the next point only needs enough
            # to recognise its start. Both stay in the cached prefix while live.
            limit = 6000 if live is not None and o.top(s).id == o.top(live).id else 400
            line += f"\n{indent}    {util.truncate(s.body, limit)}"
        outline_lines.append(line)
    focus_tops = {o.top(x).id for x in (live, topic) if x is not None}

    def in_focus(section_id: str | None) -> bool:
        s = o.by_id.get(section_id or "")
        return s is not None and o.top(s).id in focus_tops

    members = []
    q_keys, _ = governance.voting_keys(db, session)
    for a in db.query(MeetppAttendee).filter_by(session_id=session.id).order_by(MeetppAttendee.display_name).all():
        bits = [a.status.replace("_", " ")]
        if a.person_key in q_keys:
            bits.append("voting")
        if a.represented_by:
            bits.append(f"represented by {a.represented_by}")
        members.append(f"{a.display_name} ({', '.join(bits)})")
    decisions = db.query(MeetppDecision).filter_by(session_id=session.id).order_by(MeetppDecision.created_at).all()
    pending_lines, decision_lines = [], []
    for d in decisions:
        where = pid.get(d.section_id or "", "?")
        if d.status == "pending":
            if in_focus(d.section_id):
                pending_lines.append(f"{d.ref} · {where} · {d.title}" + (f" — draft: {d.resolution}" if d.resolution else ""))
            continue
        if in_focus(d.section_id):
            decision_lines.append(
                f"{d.ref} · {where} · {d.status} · {d.title}" + (f" — {d.resolution}" if d.resolution else "")
                + (" [locked]" if d.locked else "")
            )
        else:
            decision_lines.append(f"{d.ref} · {where} · {d.status} · {util.truncate(d.title, 80)}")
    prev_lines, action_lines = [], []
    reports = {r.action_id: r for r in db.query(MeetppActionReport).filter_by(session_id=session.id).all()}
    for a in ops.session_actions(db, session):
        who = ", ".join(x.get("name", "") for x in util.loads(a.assignees_json, []) if isinstance(x, dict))
        base = f"{a.ref} · {a.title}" + (f" · {who}" if who else "") + (f" · due {a.due_date}" if a.due_date else "") + f" · {a.status}"
        if ops.is_previous_action(a, session):
            # The description helps match what is said to the right action; once
            # reported, the recorded note is shown so that the model can correct it.
            report = reports.get(a.id)
            if report is None:
                prev_lines.append(base + " · TO REVIEW" + (f" — {util.truncate(a.description, 200)}" if a.description else ""))
            else:
                prev_lines.append(base + (f" · reported: {util.truncate(report.note, 200)}" if report.note else " · reported"))
        elif in_focus(a.section_id):
            action_lines.append(base + (" [locked]" if a.locked else ""))
        else:
            action_lines.append(f"{a.ref} · {util.truncate(a.title, 80)}")
    notes = []
    if live is not None:
        m = db.query(MeetppMinute).filter_by(session_id=session.id, kind="section", section_id=o.top(live).id).first()
        if m is not None:
            notes = [f"- {n.get('text')}" for n in util.loads(m.notes_json, [])][-20:]

    def seg(s: MeetppSegment) -> dict:
        t = _seg_time(s)
        return {"seq": s.seq, "time": t.strftime("%H:%M:%S") if t else "", "name": s.name, "text": s.best_text}

    return {
        "org": settings.meetpp_org_name,
        "meeting_type_label": governance.TYPE_LABELS.get(series.meeting_type if series else "informal", "Meeting"),
        "title": meeting.display_title if meeting else "",
        "date": util.now().date().isoformat(),
        "mode": session.mode,
        "outline_lines": outline_lines,
        "live": pid.get(live.id) if live is not None else None,
        "topic": pid.get(topic.id) if topic is not None else None,
        "members": members,
        "previous_actions": prev_lines,
        "pending_decisions": pending_lines,
        "decisions": decision_lines,
        "actions": action_lines,
        "running_notes": notes,
        "context": [seg(s) for s in context],
        "window": [seg(s) for s in window],
    }


def _select_window(db, session: MeetppSession, runner: "SessionRunner | None") -> tuple[list, list, list[MeetppSegment], bool]:
    """(gap_rows_to_skip, window, context, more)."""
    pending = (
        db.query(MeetppSegment)
        .filter(MeetppSegment.session_id == session.id, MeetppSegment.seq > session.transcript_cursor)
        .order_by(MeetppSegment.seq)
        .limit(3000)
        .all()
    )
    finals = [s for s in pending if not s.is_gap]
    if not finals:
        return pending, [], [], False
    if runner is not None and runner.llm_down:
        newest = _seg_time(finals[-1])
        cutoff = newest - timedelta(seconds=CATCHUP_SECONDS)
        skipped = [s for s in finals if _seg_time(s) < cutoff]
        if skipped:
            _record_gap(db, session, skipped[0].seq, skipped[-1].seq, "LLM unavailable; covered by the section composition")
            session.transcript_cursor = skipped[-1].seq
            finals = [s for s in finals if s.seq > skipped[-1].seq]
    t0 = _seg_time(finals[0])
    window = [s for s in finals if _seg_time(s) <= t0 + timedelta(seconds=WINDOW_SECONDS)] or finals[:1]
    window = window[:80]
    more = len(window) < len(finals)
    ctx_from = t0 - timedelta(seconds=int(settings.meetpp_context_seconds))
    context = (
        db.query(MeetppSegment)
        .filter(
            MeetppSegment.session_id == session.id,
            MeetppSegment.seq < window[0].seq,
            MeetppSegment.is_gap.is_(False),
            MeetppSegment.t_end >= ctx_from,
        )
        .order_by(MeetppSegment.seq.desc())
        .limit(40)
        .all()
    )
    return pending, window, list(reversed(context)), more


def _topic_rule(session: MeetppSession, o: outline_mod.Outline, target, conf: float) -> bool:
    """Hysteresis (FDD §8.3). Returns True when the topic changed."""
    st = _topic_state(session)
    changed = False
    if target is None or conf < TOPIC_MIN or target.id == session.topic_section_id:
        st["candidate"] = None
    elif conf >= TOPIC_SURE:
        changed = True
    elif (st.get("candidate") or {}).get("sid") == target.id:
        changed = True
    else:
        st["candidate"] = {"sid": target.id, "conf": conf}
    if changed:
        session.topic_section_id = target.id
        st["candidate"] = None
    _save_topic_state(session, st)
    return changed


def _advance_decision(
    session: MeetppSession, o: outline_mod.Outline, parsed: dict, topic_target, topic_conf: float,
    discussed: set[str] | None = None,
):
    """→ (target, confidence, reason, auto) or None. `discussed`: top-level
    sections with transcript tagged to them."""
    live = o.by_id.get(session.live_section_id or "")
    if live is None:
        return None
    live_top = o.top(live)
    nxt = outline_mod.next_target(o, session)
    st = _topic_state(session)
    result = None
    adv = parsed.get("advance") if isinstance(parsed.get("advance"), dict) else None
    if adv:
        to = o.resolve(adv.get("to"))
        try:
            conf = max(0.0, min(1.0, float(adv.get("confidence") or 0)))
        except (TypeError, ValueError):
            conf = 0.0
        if to is not None:
            to_top = o.top(to)
            if to_top.id != live_top.id and o.later(to_top, live_top) and to_top.status != "skipped":
                if conf >= ADVANCE_HINT_MIN:
                    st["hint"] = {"sid": to_top.id, "at": util.iso(util.now())}
                # A sure cue moves to the next point, or further when every point
                # in between is a fixed section (an unannounced "Previous
                # actions") or was already discussed; skipping an agenda point
                # nobody talked about stays a proposal for the chair.
                between = [t for t in o.nav() if o.later(t, live_top) and o.later(to_top, t)]
                skippable = all(t.kind != "agenda" or t.id in (discussed or set()) for t in between)
                auto = conf >= ADVANCE_AUTO and ((nxt is not None and to_top.id == nxt.id) or skippable)
                if auto or conf >= PROPOSAL_MIN:
                    result = (to_top, conf, util.truncate(adv.get("reason"), 200) or "", auto)
    # Recent ticks whose topic was a later point: [{"sid", "conf"} | None, …].
    hist = list(st.get("later_hist") or [])[-(LATER_WINDOW - 1):]
    entry = None
    if topic_target is not None and topic_conf >= TOPIC_MIN:
        t_top = o.top(topic_target)
        if t_top.id != live_top.id and o.later(t_top, live_top) and t_top.status != "skipped":
            entry = {"sid": t_top.id, "conf": round(topic_conf, 2)}
    hist.append(entry)
    st["later_hist"] = hist
    run = st.get("later_run") or {}
    run = {"sid": entry["sid"], "n": int(run.get("n", 0)) + 1 if run.get("sid") == entry["sid"] else 1} if entry else None
    st["later_run"] = run
    if entry is not None and (result is None or not result[3]):
        t_top = o.by_id[entry["sid"]]
        prev = hist[-2] if len(hist) >= 2 else None
        sure = (
            entry["conf"] >= ADVANCE_AUTO and prev is not None and prev["sid"] == entry["sid"]
            and prev["conf"] >= ADVANCE_AUTO
        )
        steady = sum(1 for h in hist if h is not None and h["sid"] == entry["sid"]) >= LATER_HITS
        hint = st.get("hint") or {}
        hint_at = util.parse_dt(hint.get("at"))
        cued = (
            hint.get("sid") == entry["sid"] and hint_at is not None
            and (util.now() - hint_at).total_seconds() <= ADVANCE_HINT_SECONDS
        )
        if ((sure or steady) and cued) or run["n"] >= SILENT_MOVE_TICKS:
            result = (t_top, topic_conf, "The discussion has moved on to this item.", True)
    _save_topic_state(session, st)
    return result


async def tick(session_id: str, *, trigger: str | None = None, runner: "SessionRunner | None" = None) -> dict:
    """One interpretation tick. Never raises; never stalls the cursor except
    while the LLM is unreachable (then catch-up is capped at 3 minutes)."""
    started = time.monotonic()
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None or session.status != "running":
            return {"skipped": "status"}
        pending, window, context, more = _select_window(db, session, runner)
        if not window:
            if pending:
                session.transcript_cursor = max(s.seq for s in pending)
            db.commit()
            return {"skipped": "no speech"}
        cursor_to = window[-1].seq
        # Gap rows interleaved before the window end are consumed with it.
        if not llm.llm_configured():
            session.transcript_cursor = cursor_to
            session.last_tick_at = util.now()
            db.commit()
            return {"skipped": "llm not configured", "cursor": cursor_to}
        o = outline_mod.load(db, session_id)
        data = _prompt_data(db, session, o, window, context)
        messages = prompts.build_tick_messages(**data)
        window_seqs = {s.seq for s in window}
        context_seqs = {s.seq for s in context}
        sid_prompt = o.prompt_ids()
        db.commit()
    finally:
        db.close()

    rec = SessionLocal()
    error: Exception | None = None
    parsed: dict = {}
    result = None
    try:
        parsed, result = await llm.complete_parsed(
            db=rec, purpose="tick", messages=messages, max_tokens=2000, temperature=0.2, session_id=session_id,
        )
    except llm.LLMParseError as exc:
        error = exc
    except llm.LLMError as exc:
        error = exc
    finally:
        rec.close()

    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            return {"skipped": "deleted"}
        was_ok = llm.ai_status(session_id) == "ok"
        if error is not None and not isinstance(error, llm.LLMParseError):
            llm.mark_tick(session_id, False)
            if runner is not None:
                runner.llm_down = True
            log.warning("MEETPP_TICK sid=%s llm unavailable: %s", session_id, error)
            await bus.publish_changes(db, session, ops.Changes(session=was_ok))
            return {"error": str(error)}
        if runner is not None:
            runner.llm_down = False
        llm.mark_tick(session_id, True)
        o = outline_mod.load(db, session_id)
        live_id = session.live_section_id
        if isinstance(error, llm.LLMParseError):
            _record_gap(db, session, min(window_seqs), max(window_seqs), f"invalid output: {error}")
            db.query(MeetppSegment).filter(MeetppSegment.session_id == session_id, MeetppSegment.seq.in_(window_seqs)).update(
                {MeetppSegment.section_id: live_id}, synchronize_session=False
            )
            session.transcript_cursor = max(session.transcript_cursor, cursor_to)
            session.last_tick_at = util.now()
            db.commit()
            return {"gap": str(error), "more": more}

        pins = {label: section_id for section_id, label in sid_prompt.items()}
        o.pins = pins
        topic = parsed.get("topic") if isinstance(parsed.get("topic"), dict) else {}
        topic_section = o.resolve(topic.get("section"))
        try:
            topic_conf = max(0.0, min(1.0, float(topic.get("confidence") or 0)))
        except (TypeError, ValueError):
            topic_conf = 0.0
        sub = outline_mod.resolve_sub(o, topic_section, topic.get("sub"))
        topic_target = sub or topic_section

        ctx = ops.ApplyContext(
            db=db, session=session, actor="ai", window=window_seqs, context=context_seqs,
            topic_section_id=topic_target.id if topic_target is not None and topic_conf >= TOPIC_MIN else None,
            pins=pins,
        )
        ctx.changes.session = not was_ok
        ops.apply_ops(ctx, parsed.get("ops") or [])
        session = ctx.session
        n_notes = ops.apply_notes(ctx, parsed.get("notes") or [])
        o = outline_mod.load(db, session_id)
        o.pins = pins
        topic_section = o.resolve(topic.get("section"))
        sub = outline_mod.resolve_sub(o, topic_section, topic.get("sub"))
        topic_target = sub or topic_section
        tag = topic_target.id if topic_target is not None and topic_conf >= TOPIC_MIN else session.live_section_id
        db.query(MeetppSegment).filter(MeetppSegment.session_id == session_id, MeetppSegment.seq.in_(window_seqs)).update(
            {MeetppSegment.section_id: tag}, synchronize_session=False
        )
        if _topic_rule(session, o, topic_target, topic_conf):
            ctx.changes.session = True
            ctx.changes.activate("topic", topic_target.id)
        if sub is not None and topic_conf >= TOPIC_MIN:
            done = outline_mod.mark_subpoints_through(o, sub)
            ctx.changes.sections.update(done)
        discussed = {
            o.top(o.by_id[sid]).id
            for (sid,) in db.query(MeetppSegment.section_id)
            .filter(MeetppSegment.session_id == session_id, MeetppSegment.section_id.isnot(None))
            .distinct()
            if sid in o.by_id
        }
        decision = _advance_decision(session, o, parsed, topic_target, topic_conf, discussed)
        session.transcript_cursor = max(session.transcript_cursor, cursor_to)
        session.last_tick_at = util.now()
        version = await bus.publish_changes(db, session, ctx.changes)
        log.info(
            "MEETPP_TICK sid=%s trigger=%s window=%s-%s topic=%s conf=%.2f ops_applied=%s rejected=%s notes=%s "
            "activations=%s version=%s llm_ms=%s tokens_in=%s more=%s reasons=%s",
            session_id, trigger, min(window_seqs), max(window_seqs), sid_prompt.get(topic_target.id) if topic_target else None,
            topic_conf, ctx.applied, len(ctx.rejected), n_notes, len(ctx.changes.activations), version,
            result.latency_ms if result else None, result.prompt_tokens if result else None, more,
            "; ".join(r["reason"] for r in ctx.rejected)[:300],
        )
        if decision is not None:
            await _handle_advance(db, session, o, decision)
        for sec in ctx.compose:
            compose.schedule_section(session_id, sec)
        return {
            "applied": ctx.applied,
            "rejected": ctx.rejected,
            "notes": n_notes,
            "version": version,
            "topic": topic_target.id if topic_target is not None else None,
            "advance": decision[0].id if decision else None,
            "more": more,
            "ms": int((time.monotonic() - started) * 1000),
        }
    except Exception:  # noqa: BLE001 — a tick must never kill the runner
        log.exception("meetpp: tick failed for %s", session_id)
        try:
            db.rollback()
            session = db.get(MeetppSession, session_id)
            if session is not None:
                _record_gap(db, session, min(window_seqs), max(window_seqs), "internal error while applying the tick")
                session.transcript_cursor = max(session.transcript_cursor, cursor_to)
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return {"error": "internal"}
    finally:
        db.close()


async def _handle_advance(db, session: MeetppSession, o: outline_mod.Outline, decision) -> None:
    target, conf, reason, auto = decision
    st = _topic_state(session)
    until = util.parse_dt((st.get("notnow") or {}).get(target.id))
    if until is not None and util.now() < until:
        return
    if outline_mod.ai_moves_suppressed(session):
        return
    if auto and session.mode == "lead":
        res = outline_mod.move(db, session, target, by="ai")
        await _after_move(db, session, res, by="ai", compose_delay=outline_mod.AI_UNDO_SECONDS + 1)
        return
    current = util.loads(session.proposal_json, {})
    if current.get("to") == target.id:
        return
    proposal = {"pid": util.ulid(), "to": target.id, "reason": reason, "confidence": round(conf, 2), "created_at": util.iso(util.now())}
    session.proposal_json = util.dumps(proposal)
    await bus.publish_changes(db, session, ops.Changes(session=True))
    meeting = db.get(Meeting, session.meeting_id)
    num = o.numbers.get(target.id)
    await bus.send(
        meeting.room_name if meeting else None,
        bus.message(
            "proposal", session.id, pid=proposal["pid"], to_section_id=target.id,
            title=f"{num} · {target.title}" if num else target.title, reason=reason, confidence=proposal["confidence"],
        ),
        chair_identities(meeting),
    )


# ─── lifecycle ──────────────────────────────────────────────────────────────


async def update_room_metadata(session_id: str, active: bool, *, board: str | None = None) -> None:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            return
        meeting = db.get(Meeting, session.meeting_id)
        st = ops._settings(session)
        payload = {
            "sid": session.id,
            "active": active,
            "status": session.status,
            "board_main": True,
            "public": st["show_public"],
            "in_recordings": st["in_recordings"],
        }
        room = meeting.room_name if meeting else None
    finally:
        db.close()
    await bus.set_room_meetpp(room, payload, board=board)


async def start_session(session_id: str) -> dict:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        session.status = "running"
        session.started_at = session.started_at or util.now()
        changes = ops.Changes(session=True, quorum=True)
        changes.sections.update(outline_mod.apply_skip_rules(db, session))
        res = outline_mod.start_live(db, session)
        if res is not None:
            changes.sections.update(res.changed)
        changes.attendees.update(expected_attendees(db, session))
        changes.sections.update(s.id for s in outline_mod.load(db, session_id).flat)
        await bus.publish_changes(db, session, changes)
        room = _room(db, session)
        meta = ops.session_meta(db, session)
    finally:
        db.close()
    await bus.send(room, bus.message("session", session_id, state="started"))
    asyncio.get_running_loop().create_task(
        announce(session_id, kind="session", title="Meet++ is taking notes",
                 subtitle="Live transcript and the shared board are on.", speech="Meet plus plus is now taking notes.")
    )
    # The board is a stream window and the presenter by default (FDD v3.2).
    asyncio.get_running_loop().create_task(update_room_metadata(session_id, True, board="start"))
    runtime.start_session(session_id)
    log.info("MEETPP_SESSION sid=%s event=start", session_id)
    return meta


async def pause_session(session_id: str, paused: bool) -> dict:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None or session.status not in ACTIVE_STATUSES:
            raise PositionError(409, "session is not running")
        live = db.get(outline_mod.MeetppSection, session.live_section_id) if session.live_section_id else None
        if paused and session.status == "running":
            if live is not None:
                outline_mod.pause_timer(session, live)
            session.status = "paused"
        elif not paused and session.status == "paused":
            session.status = "running"
            if live is not None:
                outline_mod.resume_timer(session, live)
        changes = ops.Changes(session=True)
        if live is not None:
            changes.sections.add(live.id)
        await bus.publish_changes(db, session, changes)
        room = _room(db, session)
        meta = ops.session_meta(db, session)
        accepted = accepted_identities(db, session_id)
    finally:
        db.close()
    await agent_mod.client.patch(session_id, {"paused": paused, "accepted_identities": accepted})
    await bus.send(room, bus.message("session", session_id, state="paused" if paused else "resumed"))
    runtime.poke(session_id)
    return meta


JOB_ORDER = ("tier2", "compose", "final", "render")


def _jobs(session: MeetppSession) -> dict:
    jobs = util.loads(session.jobs_json, {})
    for name in JOB_ORDER:
        jobs.setdefault(name, {"status": "pending"})
    return jobs


async def end_session(session_id: str) -> dict:
    """Close the live section and start the finalisation jobs (returns at once)."""
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session is None:
            return {}
        if session.status not in ACTIVE_STATUSES:
            return {"status": session.status}
        changes = ops.Changes(session=True)
        # A paused session's timer already stopped (started_at is null).
        closed = outline_mod.close_live(db, session)
        changes.sections.update(s.id for s in outline_mod.load(db, session_id).flat)
        session.status = "finalising"
        session.ended_at = util.now()
        session.proposal_json = None
        session.undo_json = None
        session.jobs_json = util.dumps(_jobs(session))
        await bus.publish_changes(db, session, changes)
        room = _room(db, session)
    finally:
        db.close()
    _ = closed
    await bus.send(room, bus.message("session", session_id, state="ended"))
    await bus.send(room, bus.message("session", session_id, state="finalising"))
    asyncio.get_running_loop().create_task(
        announce(session_id, kind="session", title="Meet++ has stopped taking notes",
                 subtitle="The minutes follow after the chair's review.", speech="Meet plus plus has stopped taking notes.")
    )
    asyncio.get_running_loop().create_task(update_room_metadata(session_id, False, board="end"))
    runtime.start_finalisation(session_id)
    log.info("MEETPP_SESSION sid=%s event=end", session_id)
    return {"status": "finalising"}


async def _set_job(session_id: str, name: str, **fields) -> None:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        jobs = _jobs(session)
        jobs[name] = {**jobs.get(name, {}), **fields}
        session.jobs_json = util.dumps(jobs)
        await bus.publish_changes(db, session, ops.Changes(session=True))
    finally:
        db.close()


def _job_status(session_id: str) -> dict:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        return _jobs(session) if session else {}
    finally:
        db.close()


async def _job_tier2(session_id: str) -> None:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        already_done = session.final_pass_done
        agent_info = util.loads(session.agent_json, {})
    finally:
        db.close()
    if already_done:
        await _set_job(session_id, "tier2", status="done", finished_at=util.iso(util.now()))
        return
    if agent_info.get("tier2") == "off":
        await agent_mod.client.stop(session_id)
        await _set_job(session_id, "tier2", status="skipped", error="tier 2 is off")
        return
    started = util.now()
    ok = await agent_mod.client.finalize(session_id)
    if not ok:
        await _set_job(session_id, "tier2", status="skipped", error="agent unavailable")
        return
    await _set_job(session_id, "tier2", status="running", started_at=util.iso(started))
    deadline = time.monotonic() + float(settings.meetpp_final_pass_timeout_seconds)
    outcome = None
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, session_id)
            info = util.loads(session.agent_json, {})
            if session.final_pass_done or info.get("final_pass") == "done":
                outcome = "done"
            elif info.get("final_pass") in ("failed", "skipped"):
                outcome = info["final_pass"]
        finally:
            db.close()
        if outcome:
            break
        await asyncio.sleep(min(2.0, max(0.05, deadline - time.monotonic())))
    await agent_mod.client.stop(session_id)
    if outcome == "done":
        await _set_job(session_id, "tier2", status="done", finished_at=util.iso(util.now()), error=None)
    elif outcome == "failed":
        await _set_job(session_id, "tier2", status="failed", finished_at=util.iso(util.now()), error="final pass failed")
    elif outcome == "skipped":
        await _set_job(session_id, "tier2", status="skipped", finished_at=util.iso(util.now()), error="tier 2 is off")
    else:
        await _set_job(session_id, "tier2", status="skipped", finished_at=util.iso(util.now()), error="timed out after 5 minutes")


async def _job_compose(session_id: str) -> None:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        ids = compose.sections_to_compose(db, session)
    finally:
        db.close()
    await _set_job(session_id, "compose", status="running", total=len(ids), done=0, failed=0)
    failed = []
    for i, sec in enumerate(ids, start=1):
        status = await compose.compose_section(session_id, sec)
        if status == "failed":
            failed.append(sec)
        await _set_job(session_id, "compose", done=i, failed=len(failed))
    if failed:
        await _set_job(session_id, "compose", status="failed", error=f"{len(failed)} section(s) failed", sections=failed)
    else:
        await _set_job(session_id, "compose", status="done", error=None, sections=[])


def mark_absentees(db, session: MeetppSession) -> set[str]:
    changed = expected_attendees(db, session)
    for a in db.query(MeetppAttendee).filter_by(session_id=session.id).all():
        if a.status == "not_registered" and a.first_joined_at is None:
            a.status = "absent"
            changed.add(a.id)
    return changed


async def _job_final(session_id: str) -> None:
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        changed = mark_absentees(db, session)
        await bus.publish_changes(db, session, ops.Changes(attendees=changed, quorum=True))
    finally:
        db.close()
    await compose.compose_final(session_id)
    await _set_job(session_id, "final", status="done", error=None)


async def _job_render(session_id: str) -> None:
    from app.meetpp import export as export_mod

    try:
        from app.meetpp import report
    except Exception as exc:  # noqa: BLE001
        await _set_job(session_id, "render", status="failed", error=f"report renderer unavailable: {exc}"[:300])
        return
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        data = export_mod.build_export(db, session)
    finally:
        db.close()
    pdf = await asyncio.to_thread(report.render_meeting_report, data)
    if not pdf or not bytes(pdf).startswith(b"%PDF"):
        raise RuntimeError("the renderer returned no PDF")
    await _set_job(session_id, "render", status="done", error=None, bytes=len(pdf))


_JOB_FUNCS = {"tier2": _job_tier2, "compose": _job_compose, "final": _job_final, "render": _job_render}


_finalising: set[str] = set()


async def run_finalisation(session_id: str, *, retry: bool = False) -> dict:
    """Run every pending (or, on retry, failed) job in order; each failure is
    recorded on its job and the session always reaches review."""
    if session_id in _finalising:
        return _job_status(session_id)
    _finalising.add(session_id)
    try:
        return await _run_finalisation(session_id, retry=retry)
    finally:
        _finalising.discard(session_id)


async def _run_finalisation(session_id: str, *, retry: bool) -> dict:
    if retry:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, session_id)
            jobs = _jobs(session)
            for name in JOB_ORDER:
                if jobs[name].get("status") in ("failed", "running"):
                    jobs[name] = {"status": "pending"}
            session.jobs_json = util.dumps(jobs)
            db.commit()
        finally:
            db.close()
    for name in JOB_ORDER:
        status = _job_status(session_id).get(name, {}).get("status")
        if status in ("done", "skipped", "failed"):
            continue
        try:
            if name != "tier2":
                await _set_job(session_id, name, status="running", started_at=util.iso(util.now()))
            await _JOB_FUNCS[name](session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("MEETPP_FINALISE sid=%s job=%s failed", session_id, name)
            await _set_job(session_id, name, status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
        log.info("MEETPP_FINALISE sid=%s job=%s status=%s", session_id, name, _job_status(session_id).get(name, {}).get("status"))
    db = SessionLocal()
    try:
        session = db.get(MeetppSession, session_id)
        if session.status == "finalising":
            session.status = "review"
            session.finalised_at = util.now()
        jobs = _jobs(session)
        await bus.publish_changes(db, session, ops.Changes(session=True))
        room = _room(db, session)
        status = session.status
    finally:
        db.close()
    if status == "review":
        await bus.send(room, bus.message("session", session_id, state="review"))
    return jobs


# ─── runner ─────────────────────────────────────────────────────────────────


class _Lease:
    def __init__(self, session_id: str) -> None:
        self.key = f"meetpp:lease:{session_id}"
        self.token = util.ulid()

    async def _redis(self, fn):
        try:
            import redis as redis_sync

            def _do():
                r = redis_sync.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
                return fn(r)

            return await asyncio.to_thread(_do)
        except Exception:  # noqa: BLE001 — no Redis: single process, proceed
            return None

    async def acquire(self) -> bool:
        res = await self._redis(lambda r: r.set(self.key, self.token, nx=True, ex=LEASE_TTL))
        if res is None:
            held = await self._redis(lambda r: r.get(self.key))
            return held is None or (held.decode() if isinstance(held, bytes) else held) == self.token
        return bool(res)

    async def renew(self) -> bool:
        def _renew(r):
            cur = r.get(self.key)
            cur = cur.decode() if isinstance(cur, bytes) else cur
            if cur in (None, self.token):
                r.set(self.key, self.token, ex=LEASE_TTL)
                return True
            return False

        res = await self._redis(_renew)
        return res is not False

    async def release(self) -> None:
        def _rel(r):
            cur = r.get(self.key)
            cur = cur.decode() if isinstance(cur, bytes) else cur
            if cur == self.token:
                r.delete(self.key)

        await self._redis(_rel)


class SessionRunner:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.wake = asyncio.Event()
        self.stopping = False
        self.task: asyncio.Task | None = None
        self.tick_task: asyncio.Task | None = None
        self.final_task: asyncio.Task | None = None
        self.trigger: str | None = None
        self.llm_down = False
        self.more = False
        self.lease = _Lease(session_id)
        self._last_renew = 0.0
        self._last_reconcile = 0.0
        self._last_timebox = 0.0
        self._agent_started = False
        self._not_connected = 0

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.stopping = False
            self.task = asyncio.get_running_loop().create_task(self.run())

    def poke(self, trigger: str | None = None) -> None:
        if trigger == "chair" or (trigger == "cue" and self.trigger != "chair"):
            self.trigger = trigger
        self.wake.set()

    async def stop(self) -> None:
        self.stopping = True
        self.wake.set()
        for t in (self.tick_task, self.task):
            if t is not None and not t.done():
                try:
                    await asyncio.wait_for(asyncio.shield(t), timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                    t.cancel()
        await self.lease.release()

    async def run(self) -> None:
        if not await self.lease.acquire():
            log.info("meetpp: session %s is held by another process", self.session_id)
            return
        self._last_renew = time.monotonic()
        try:
            while not self.stopping:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                self.wake.clear()
                if self.stopping:
                    break
                try:
                    if not await self._iteration():
                        break
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("meetpp: runner iteration failed for %s", self.session_id)
                    await asyncio.sleep(1)
        finally:
            await self.lease.release()

    async def _iteration(self) -> bool:
        now = time.monotonic()
        if now - self._last_renew >= LEASE_RENEW_SECONDS:
            self._last_renew = now
            if not await self.lease.renew():
                log.warning("meetpp: lease for %s lost", self.session_id)
                return False
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, self.session_id)
            if session is None:
                return False
            status = session.status
            due = status == "running" and self._tick_due(db, session)
        finally:
            db.close()
        if status == "finalising":
            if self.final_task is None or self.final_task.done():
                self.final_task = asyncio.get_running_loop().create_task(run_finalisation(self.session_id))
            return True
        if status not in ACTIVE_STATUSES:
            return False
        if not self._agent_started or now - self._last_reconcile >= RECONCILE_SECONDS:
            self._last_reconcile = now
            await self.reconcile_agent()
        if due:
            trigger, self.trigger = self.trigger, None
            self.tick_task = asyncio.get_running_loop().create_task(self._tick(trigger))
        if status == "running" and now - self._last_timebox >= 5:
            self._last_timebox = now
            await self._timebox()
        return True

    def _tick_due(self, db, session: MeetppSession) -> bool:
        if self.tick_task is not None and not self.tick_task.done():
            return False
        has_new = (
            db.query(MeetppSegment.id)
            .filter(MeetppSegment.session_id == session.id, MeetppSegment.seq > session.transcript_cursor)
            .first()
            is not None
        )
        if not has_new:
            return False
        last = util.aware(session.last_tick_at)
        since = (util.now() - last).total_seconds() if last is not None else None
        if self.trigger == "chair" or self.more:
            return True
        if self.trigger == "cue" and (since is None or since >= CUE_MIN_SPACING_SECONDS):
            return True
        interval = float(settings.meetpp_tick_seconds)
        if llm.over_budget(db, session.id):
            interval = THROTTLED_TICK_SECONDS
        return since is None or since >= interval

    async def _tick(self, trigger: str | None) -> None:
        try:
            res = await tick(self.session_id, trigger=trigger, runner=self)
            self.more = bool(res.get("more"))
            if self.more:
                self.wake.set()
        except Exception:  # noqa: BLE001
            log.exception("meetpp: tick task failed for %s", self.session_id)

    async def _timebox(self) -> None:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, self.session_id)
            if session is None or not ops._settings(session)["timebox_nudges"]:
                return
            section = outline_mod.timebox_overrun(db, session)
            if section is None:
                db.commit()
                return
            o = outline_mod.load(db, session.id)
            label = outline_mod.label(o, section)
            minutes = int(outline_mod.live_elapsed(session, section) // 60)
            db.commit()
            dest = chair_identities(db.get(Meeting, session.meeting_id))
        finally:
            db.close()
        await announce(
            self.session_id, kind="timebox", title=f"{label} is over its time",
            subtitle=f"{minutes} min of {section.timebox_minutes} min", destination=dest,
        )

    async def reconcile_agent(self) -> None:
        db = SessionLocal()
        try:
            session = db.get(MeetppSession, self.session_id)
            if session is None or session.status not in ACTIVE_STATUSES:
                return
            accepted = accepted_identities(db, self.session_id)
            paused = session.status == "paused"
            meeting = db.get(Meeting, session.meeting_id)
            start_payload = {
                "session_id": session.id,
                "room": meeting.room_name if meeting else "",
                "ws_url": settings.meetpp_agent_ws_url,
                "token": mint_agent_token(room_name=meeting.room_name, identity=f"meetpp-scribe-{session.id}") if meeting else "",
                "language": "en",
                "glossary": glossary(db, session),
                "accepted_identities": accepted,
                "paused": paused,
            }
        finally:
            db.close()
        health = await agent_mod.client.health()
        entry = agent_mod.session_health(health, self.session_id)
        restart = False
        if entry is None:
            restart = True
        elif entry.get("connected") is False:
            self._not_connected += 1
            restart = self._not_connected >= 2
        else:
            self._not_connected = 0
        if restart or not self._agent_started:
            if entry is not None and not restart:
                ok = await agent_mod.client.patch(self.session_id, {"accepted_identities": accepted, "paused": paused})
            else:
                ok = await agent_mod.client.start(start_payload)
                if ok and self._agent_started and restart:
                    await ingest(self.session_id, {"gaps": [{"t_from": util.iso(util.now() - timedelta(seconds=RECONCILE_SECONDS)), "t_to": util.iso(util.now()), "reason": "agent_restart"}]})
                    log.warning("meetpp: agent session %s restarted", self.session_id)
            self._agent_started = self._agent_started or ok
            self._not_connected = 0
            return
        await agent_mod.client.patch(self.session_id, {"accepted_identities": accepted, "paused": paused})


class MeetppRuntime:
    def __init__(self) -> None:
        self.runners: dict[str, SessionRunner] = {}

    def get(self, session_id: str) -> SessionRunner:
        r = self.runners.get(session_id)
        if r is None:
            r = SessionRunner(session_id)
            self.runners[session_id] = r
        return r

    def poke(self, session_id: str, trigger: str | None = None) -> None:
        r = self.runners.get(session_id)
        if r is not None:
            r.poke(trigger)

    def start_session(self, session_id: str) -> None:
        try:
            self.get(session_id).start()
        except RuntimeError:
            pass

    def start_finalisation(self, session_id: str) -> None:
        try:
            runner = self.get(session_id)
            if runner.task is None or runner.task.done():
                runner.start()
            else:
                runner.poke()
        except RuntimeError:
            pass

    async def start(self) -> None:
        """Resume running, paused and finalising sessions after a restart."""
        if not settings.meetpp_enabled:
            return
        db = SessionLocal()
        try:
            ids = [
                r[0]
                for r in db.query(MeetppSession.id).filter(MeetppSession.status.in_(("running", "paused", "finalising"))).all()
            ]
            for sid in ids:
                session = db.get(MeetppSession, sid)
                if session.status == "finalising":
                    jobs = _jobs(session)
                    for name in JOB_ORDER:
                        if jobs[name].get("status") == "running" and name != "tier2":
                            jobs[name] = {"status": "pending"}
                        if name == "tier2" and jobs[name].get("status") == "running":
                            jobs[name] = {"status": "pending"}
                    session.jobs_json = util.dumps(jobs)
            db.commit()
        finally:
            db.close()
        for sid in ids:
            await _Lease(sid)._redis(lambda r, k=f"meetpp:lease:{sid}": r.delete(k))
            self.get(sid).start()
        if ids:
            log.info("meetpp: resumed %d session(s)", len(ids))

    async def stop(self) -> None:
        for r in list(self.runners.values()):
            await r.stop()
        self.runners.clear()
        await bus.close()

    async def drop(self, session_id: str) -> None:
        r = self.runners.pop(session_id, None)
        if r is not None:
            await r.stop()


runtime = MeetppRuntime()

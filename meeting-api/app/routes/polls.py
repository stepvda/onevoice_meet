"""
Polls and Q&A endpoints. Realtime fan-out: writers publish a small
data-channel signal on the meet-polls / meet-qna topic; readers refetch
state from the API. The DB is the source of truth.

Permission model mirrors the rest of the moderation surface:
- Anyone in the room may vote / ask / upvote.
- Only host or co-host may create / close polls or mark questions answered.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ulid import ULID

from sqlalchemy import func

from app.auth import RequireUser
from app.db import get_db
from app.models import Meeting, Poll, PollVote, Question, QuestionUpvote
from app.routes.meetings import is_moderator

router = APIRouter(prefix="/v1")

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,200}$")


def _validate_identity(identity: str) -> None:
    if not _IDENTITY_RE.match(identity):
        raise HTTPException(status_code=400, detail="invalid identity")


def _meeting_for_room(room_name: str, db: Session) -> Meeting:
    m = db.query(Meeting).filter_by(room_name=room_name).first()
    if not m or not m.is_active:
        raise HTTPException(status_code=404, detail="room not found")
    return m


# ─── Polls ────────────────────────────────────────────────────────────


class CreatePollBody(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    options: list[str] = Field(min_length=2, max_length=6)


def _serialise_poll(poll: Poll, counts: list[int]) -> dict:
    options = json.loads(poll.options_json or "[]")
    # Trim/pad counts to match the option list — defensive against stale
    # votes pointing at indexes that no longer exist.
    if len(counts) < len(options):
        counts = counts + [0] * (len(options) - len(counts))
    counts = counts[: len(options)]
    return {
        "id": poll.id,
        "meeting_id": poll.meeting_id,
        "question": poll.question,
        "options": options,
        "counts": counts,
        "total_votes": sum(counts),
        "status": poll.status,
        "created_at": poll.created_at.isoformat() if poll.created_at else None,
        "closed_at": poll.closed_at.isoformat() if poll.closed_at else None,
    }


def _counts_for_poll(poll: Poll, db: Session) -> list[int]:
    """Single GROUP BY query against PollVote for one poll. Used on
    create/vote/close where only one poll is in scope."""
    options = json.loads(poll.options_json or "[]")
    counts = [0] * len(options)
    rows = (
        db.query(PollVote.option_index, func.count(PollVote.id))
        .filter(PollVote.poll_id == poll.id)
        .group_by(PollVote.option_index)
        .all()
    )
    for idx, n in rows:
        if 0 <= idx < len(counts):
            counts[idx] = n
    return counts


@router.get("/meetings/{meeting_id}/polls")
def list_polls(meeting_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """Public-by-room: a non-authenticated participant can read poll state.
    We scope to `meeting_id` (not room_name) for parity with other
    polls endpoints below; the SPA already knows the meeting_id via the
    publicRoomInfo payload."""
    polls = (
        db.query(Poll)
        .filter_by(meeting_id=meeting_id)
        .order_by(Poll.created_at.desc())
        .all()
    )
    if not polls:
        return []
    # One aggregate query for all polls' vote counts instead of N+1 selects.
    poll_ids = [p.id for p in polls]
    vote_rows = (
        db.query(PollVote.poll_id, PollVote.option_index, func.count(PollVote.id))
        .filter(PollVote.poll_id.in_(poll_ids))
        .group_by(PollVote.poll_id, PollVote.option_index)
        .all()
    )
    counts_by_poll: dict[str, dict[int, int]] = {}
    for pid, idx, n in vote_rows:
        counts_by_poll.setdefault(pid, {})[idx] = n
    out: list[dict] = []
    for p in polls:
        options = json.loads(p.options_json or "[]")
        counts = [counts_by_poll.get(p.id, {}).get(i, 0) for i in range(len(options))]
        out.append(_serialise_poll(p, counts))
    return out


@router.post("/meetings/{meeting_id}/polls", status_code=201)
def create_poll(
    meeting_id: str,
    body: CreatePollBody,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not is_moderator(m, user.sub):
        raise HTTPException(status_code=404, detail="meeting not found")
    options = [o.strip() for o in body.options if o.strip()]
    if len(options) < 2:
        raise HTTPException(status_code=400, detail="poll needs at least 2 options")
    poll = Poll(
        id=str(ULID()),
        meeting_id=m.id,
        question=body.question.strip(),
        options_json=json.dumps(options[:6]),
        status="open",
        created_by=user.sub,
    )
    db.add(poll)
    db.commit()
    db.refresh(poll)
    return _serialise_poll(poll, _counts_for_poll(poll, db))


class VoteBody(BaseModel):
    voter_identity: str = Field(min_length=1, max_length=200)
    option_index: int = Field(ge=0, le=5)


@router.post("/polls/{poll_id}/vote")
def vote_on_poll(
    poll_id: str,
    body: VoteBody,
    db: Session = Depends(get_db),
) -> dict:
    """One vote per identity per poll. Re-voting replaces the prior vote."""
    poll = db.query(Poll).filter_by(id=poll_id).first()
    if not poll:
        raise HTTPException(status_code=404, detail="poll not found")
    if poll.status != "open":
        raise HTTPException(status_code=403, detail="poll is closed")
    _validate_identity(body.voter_identity)
    options = json.loads(poll.options_json or "[]")
    if body.option_index >= len(options):
        raise HTTPException(status_code=400, detail="option_index out of range")
    # Atomic upsert: SELECT-then-INSERT loses the race when the same voter
    # double-clicks; the second INSERT would hit the uq_poll_voter
    # UniqueConstraint and 500. ON CONFLICT DO UPDATE keeps both calls
    # idempotent and converges to the latest option_index.
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    stmt = sqlite_insert(PollVote).values(
        poll_id=poll.id,
        voter_identity=body.voter_identity,
        option_index=body.option_index,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PollVote.poll_id, PollVote.voter_identity],
        set_={"option_index": stmt.excluded.option_index},
    )
    db.execute(stmt)
    db.commit()
    return _serialise_poll(poll, _counts_for_poll(poll, db))


@router.post("/polls/{poll_id}/close")
def close_poll(poll_id: str, user: RequireUser, db: Session = Depends(get_db)) -> dict:
    poll = db.query(Poll).filter_by(id=poll_id).first()
    if not poll:
        raise HTTPException(status_code=404, detail="poll not found")
    m = db.query(Meeting).filter_by(id=poll.meeting_id).first()
    if not m or not is_moderator(m, user.sub):
        raise HTTPException(status_code=403, detail="only moderators can close polls")
    poll.status = "closed"
    poll.closed_at = datetime.now(timezone.utc)
    db.commit()
    return _serialise_poll(poll, _counts_for_poll(poll, db))


# ─── Q&A ──────────────────────────────────────────────────────────────


class AskBody(BaseModel):
    asker_identity: str = Field(min_length=1, max_length=200)
    asker_name: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=600)


def _serialise_question(q: Question, upvotes: int) -> dict:
    return {
        "id": q.id,
        "meeting_id": q.meeting_id,
        "asker_identity": q.asker_identity,
        "asker_name": q.asker_name,
        "question": q.question,
        "status": q.status,
        "upvotes": upvotes,
        "created_at": q.created_at.isoformat() if q.created_at else None,
        "answered_at": q.answered_at.isoformat() if q.answered_at else None,
    }


def _upvote_count(q: Question, db: Session) -> int:
    return db.query(QuestionUpvote).filter_by(question_id=q.id).count()


@router.get("/meetings/{meeting_id}/questions")
def list_questions(meeting_id: str, db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(Question)
        .filter_by(meeting_id=meeting_id)
        .order_by(Question.created_at.desc())
        .all()
    )
    if not rows:
        return []
    # One aggregate query for all questions' upvote counts instead of N+1.
    qids = [q.id for q in rows]
    counts = dict(
        db.query(QuestionUpvote.question_id, func.count(QuestionUpvote.id))
        .filter(QuestionUpvote.question_id.in_(qids))
        .group_by(QuestionUpvote.question_id)
        .all()
    )
    return [_serialise_question(q, counts.get(q.id, 0)) for q in rows]


@router.post("/meetings/{meeting_id}/questions", status_code=201)
def ask_question(meeting_id: str, body: AskBody, db: Session = Depends(get_db)) -> dict:
    m = db.query(Meeting).filter_by(id=meeting_id).first()
    if not m or not m.is_active:
        raise HTTPException(status_code=404, detail="meeting not found")
    _validate_identity(body.asker_identity)
    q = Question(
        meeting_id=m.id,
        asker_identity=body.asker_identity,
        asker_name=body.asker_name.strip(),
        question=body.question.strip(),
        status="open",
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return _serialise_question(q, _upvote_count(q, db))


class UpvoteBody(BaseModel):
    voter_identity: str = Field(min_length=1, max_length=200)


@router.post("/questions/{question_id}/upvote")
def upvote_question(question_id: int, body: UpvoteBody, db: Session = Depends(get_db)) -> dict:
    q = db.query(Question).filter_by(id=question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="question not found")
    _validate_identity(body.voter_identity)
    existing = (
        db.query(QuestionUpvote)
        .filter_by(question_id=q.id, voter_identity=body.voter_identity)
        .first()
    )
    if existing:
        # Toggle off — second upvote from the same identity removes it.
        db.delete(existing)
    else:
        db.add(QuestionUpvote(question_id=q.id, voter_identity=body.voter_identity))
    db.commit()
    return _serialise_question(q, _upvote_count(q, db))


@router.post("/questions/{question_id}/answer")
def mark_question_answered(
    question_id: int,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(Question).filter_by(id=question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="question not found")
    m = db.query(Meeting).filter_by(id=q.meeting_id).first()
    if not m or not is_moderator(m, user.sub):
        raise HTTPException(status_code=403, detail="only moderators can mark answered")
    q.status = "answered"
    q.answered_at = datetime.now(timezone.utc)
    db.commit()
    return _serialise_question(q, _upvote_count(q, db))


@router.delete("/questions/{question_id}")
def dismiss_question(
    question_id: int,
    user: RequireUser,
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(Question).filter_by(id=question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="question not found")
    m = db.query(Meeting).filter_by(id=q.meeting_id).first()
    if not m or not is_moderator(m, user.sub):
        raise HTTPException(status_code=403, detail="only moderators can dismiss questions")
    q.status = "dismissed"
    db.commit()
    return _serialise_question(q, _upvote_count(q, db))

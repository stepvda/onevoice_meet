"""Meet++ tables.

All tables are new; two columns are added to `meetings` (meetpp_series_id,
meetpp_enabled). Imported by `app.models` so `Base.metadata.create_all`
creates them at startup, matching the rest of the app. JSON fields are TEXT
holding JSON, like `cohost_user_ids` / `options_json`.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MeetppSeries(Base):
    """Groups the sessions of a recurring meeting or a chain of meetings.

    Numbering (A-nn, D-nn) is continuous per series, so action references
    stay stable across meetings.
    """

    __tablename__ = "meetpp_series"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    owner_sub: Mapped[str] = mapped_column(String, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(300))
    # For a recurring meeting row this points at the Meeting that owns the
    # series; NULL for a series created by Meet++ ("new meeting" chains).
    meeting_id: Mapped[str | None] = mapped_column(String, index=True)
    external_ref: Mapped[str | None] = mapped_column(String(200))
    action_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    decision_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class MeetppSession(Base):
    """One AI session for one meeting occurrence."""

    __tablename__ = "meetpp_sessions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("meetpp_series.id"), nullable=False, index=True)
    created_by_user_id: Mapped[str] = mapped_column(String, nullable=False)
    # setup | running | paused | finalising | review | published | aborted
    status: Mapped[str] = mapped_column(String(20), default="setup", nullable=False, index=True)
    template: Mapped[str] = mapped_column(String(20), default="agenda")  # agenda | goal
    mode: Mapped[str] = mapped_column(String(10), default="lead")  # lead | assist
    language: Mapped[str] = mapped_column(String(8), default="en")
    task_language: Mapped[str | None] = mapped_column(String(8))  # chair's UI language for notices
    goal: Mapped[str | None] = mapped_column(String(2000))
    phase: Mapped[str] = mapped_column(String(40), default="opening")
    phase_index: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    current_item_id: Mapped[str | None] = mapped_column(String(26))
    state_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transcript_cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # JSON list of identities allowed to edit board items.
    editors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # JSON board settings: show_public, in_recordings, timebox_nudges, speak.
    settings_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # JSON phase proposal state: {"pid":…, "to":…, "auto_at":…, "suppressed_until":…}
    proposal_json: Mapped[str | None] = mapped_column(Text)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_tick_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0 = never published; increments on every re-publish. Drives ICS SEQUENCE.
    publish_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Finalisation output, JSON: {summary, next_agenda[], required_next[], changes[]}.
    final_json: Mapped[str | None] = mapped_column(Text)
    # {date_text, iso, duration_min, room, recipients[]} drafted in review.
    review_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_meetpp_sessions_meeting_status", "meeting_id", "status"),
    )


class MeetppDocument(Base):
    """Uploaded PDF (agenda or previous notes) and its extraction state."""

    __tablename__ = "meetpp_documents"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # agenda | previous_notes
    filename: Mapped[str] = mapped_column(String(400), nullable=False)
    path: Mapped[str | None] = mapped_column(String(700))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="uploaded", nullable=False)  # uploaded | parsing | done | failed
    error: Mapped[str | None] = mapped_column(String(500))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    structured_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class MeetppAgendaItem(Base):
    __tablename__ = "meetpp_agenda_items"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    presenter: Mapped[str | None] = mapped_column(String(200))
    timebox_minutes: Mapped[int | None] = mapped_column(Integer)
    desired_outcome: Mapped[str | None] = mapped_column(String(600))
    # pending | active | done | deferred
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    source: Mapped[str] = mapped_column(String(10), default="user")  # pdf | ai | user | aob
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (Index("ix_meetpp_agenda_session_position", "session_id", "position"),)


class MeetppDecision(Base):
    __tablename__ = "meetpp_decisions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    series_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    ref: Mapped[str] = mapped_column(String(20), nullable=False)
    agenda_item_id: Mapped[str | None] = mapped_column(String(26))
    text: Mapped[str] = mapped_column(String(600), nullable=False)
    rationale: Mapped[str | None] = mapped_column(String(600))
    # proposed | confirmed | rejected
    status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)
    origin: Mapped[str] = mapped_column(String(20), default="ai")  # ai | user | pdf
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("series_id", "ref", name="uq_meetpp_decision_series_ref"),
        Index("ix_meetpp_decisions_session", "session_id"),
    )


class MeetppAction(Base):
    """Series-scoped action; carried across sessions until done or dropped."""

    __tablename__ = "meetpp_actions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("meetpp_series.id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    ref: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    owner_name: Mapped[str | None] = mapped_column(String(200))
    owner_person_key: Mapped[str | None] = mapped_column(String(200))
    due_date: Mapped[str | None] = mapped_column(String(20))  # ISO date
    agenda_item_id: Mapped[str | None] = mapped_column(String(26))
    # proposed | open | in_progress | done | dropped | carried
    status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)
    # Status this action carried into the *current* session's review (phase 2).
    review_status: Mapped[str | None] = mapped_column(String(20))
    origin: Mapped[str] = mapped_column(String(20), default="ai")  # ai | user | pdf
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    carried_from_id: Mapped[str | None] = mapped_column(String(26))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("series_id", "ref", name="uq_meetpp_action_series_ref"),
        Index("ix_meetpp_actions_series_status", "series_id", "status"),
    )


class MeetppMinute(Base):
    __tablename__ = "meetpp_minutes"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    agenda_item_id: Mapped[str | None] = mapped_column(String(26))
    body_md: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)  # draft | edited | final
    origin: Mapped[str] = mapped_column(String(20), default="ai")
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "agenda_item_id", name="uq_meetpp_minute_session_item"),)


class MeetppAttendee(Base):
    __tablename__ = "meetpp_attendees"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    person_key: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(300))
    identities_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    role: Mapped[str | None] = mapped_column(String(100))
    # present | left | absent | apologies | not_transcribed
    presence: Mapped[str] = mapped_column(String(20), default="present", nullable=False)
    talk_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    required_now: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    required_next: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    required_reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "person_key", name="uq_meetpp_attendee_session_person"),)


class MeetppAttachment(Base):
    __tablename__ = "meetpp_attachments"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    agenda_item_id: Mapped[str | None] = mapped_column(String(26))
    kind: Mapped[str] = mapped_column(String(20), default="whiteboard", nullable=False)  # whiteboard | upload
    path: Mapped[str] = mapped_column(String(700), nullable=False)
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    caption: Mapped[str | None] = mapped_column(String(400))
    author: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MeetppSegment(Base):
    """Immutable transcript segment. seq is monotonic per session."""

    __tablename__ = "meetpp_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    identity: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    t_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    t_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    lang: Mapped[str | None] = mapped_column(String(8))
    avg_logprob: Mapped[float | None] = mapped_column(Float)
    no_speech_prob: Mapped[float | None] = mapped_column(Float)
    is_gap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_meetpp_segment_session_seq"),
        Index("ix_meetpp_segments_session_seq", "session_id", "seq"),
    )


class MeetppOp(Base):
    """Applied, rejected or suggested operation (audit + eval)."""

    __tablename__ = "meetpp_ops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    op_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    actor: Mapped[str] = mapped_column(String(200), default="ai", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="applied", nullable=False)  # applied | rejected | suggested
    reason: Mapped[str | None] = mapped_column(String(300))
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (Index("ix_meetpp_ops_session_version", "session_id", "version"),)


class MeetppConsent(Base):
    __tablename__ = "meetpp_consents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    identity: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), default="accept", nullable=False)  # accept | opt_out
    consent_version: Mapped[str] = mapped_column(String(40), default="v1", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "identity", name="uq_meetpp_consent_session_identity"),)


class MeetppOutput(Base):
    __tablename__ = "meetpp_outputs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # minutes_pdf | agenda_pdf | ics | email
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    path: Mapped[str | None] = mapped_column(String(700))
    filename: Mapped[str | None] = mapped_column(String(300))
    recipients_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    results_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MeetppLlmCall(Base):
    __tablename__ = "meetpp_llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String(26), index=True)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ok", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (Index("ix_meetpp_llm_session_created", "session_id", "created_at"),)

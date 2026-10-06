"""Meet++ tables (FDD v3.1).

Schema v3 replaces the Release 1 tables. Release 1 data was test data only and
is discarded: `ensure_schema()` drops every `meetpp_*` table when the stored
schema version is older than SCHEMA_VERSION and lets `create_all` rebuild them.
The two columns on `meetings` (meetpp_series_id, meetpp_enabled) are kept.

Conventions match the rest of the app: ULID string keys for user-facing rows,
integer keys for high-volume rows, JSON stored as TEXT, timezone-aware UTC
timestamps (SQLite drops the tzinfo on read; use `aware()` when comparing).
Names follow the OneVoice OM module (OrgMeeting, OrgDecision, OrgVote,
OrgVoteBallot, OrgAction, OrgActionMeeting, OrgAttendance, OrgMinutes) so the
R2 push to OM is a field mapping (see docs/meetpp-v3-contract.md §7).
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
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

SCHEMA_VERSION = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class MeetppSchema(Base):
    """Single-row marker for the Meet++ schema generation."""

    __tablename__ = "meetpp_schema"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class MeetppSeries(Base):
    """Groups the sessions of a recurring meeting or a chain of meetings.

    Holds the roster (meetpp_roster), the governance rules for formal
    meetings and the A-n / D-n counters, so references stay stable across
    meetings of the series.
    """

    __tablename__ = "meetpp_series"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    owner_sub: Mapped[str] = mapped_column(String, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(300))
    meeting_id: Mapped[str | None] = mapped_column(String, index=True)
    external_ref: Mapped[str | None] = mapped_column(String(200))
    # informal | board | general_assembly
    meeting_type: Mapped[str] = mapped_column(String(20), default="informal", nullable=False)
    # ordinary | unanimous | two_thirds | four_fifths
    majority_rule: Mapped[str] = mapped_column(String(20), default="ordinary", nullable=False)
    # NULL = majority of the voting roster (computed per meeting).
    quorum_required: Mapped[int | None] = mapped_column(Integer)
    action_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    decision_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class MeetppRoster(Base):
    """Series member, maintained automatically from Meet participants (FDD §8.6).

    person_key: "sub:<sso-or-native sub>" for signed-in users,
    "guest:<uuid>" for anonymous guests (key kept in the guest's browser).
    """

    __tablename__ = "meetpp_roster"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("meetpp_series.id"), nullable=False, index=True)
    person_key: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    username: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(300))
    # Default: signed-in = True, guest = False. Chair can toggle.
    voting: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Session in which the member first joined; null when seeded from a
    # previous report. Decides who votes by default (runtime.upsert_roster).
    first_session_id: Mapped[str | None] = mapped_column(String(26))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("series_id", "person_key", name="uq_meetpp_roster_series_person"),)


class MeetppSession(Base):
    """One Meet++ run for one meeting occurrence."""

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
    goal: Mapped[str | None] = mapped_column(String(2000))
    live_section_id: Mapped[str | None] = mapped_column(String(26))
    topic_section_id: Mapped[str | None] = mapped_column(String(26))
    # Hysteresis memory: {"candidate": section_id, "count": n, "conf": x}
    topic_state_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transcript_cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    editors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # show_public, in_recordings, timebox_nudges, speak
    settings_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # Pending proposal (assist mode / low confidence): {pid, to, reason, confidence, created_at}
    proposal_json: Mapped[str | None] = mapped_column(Text)
    # Last AI move that can be undone: {from, to, until}
    undo_json: Mapped[str | None] = mapped_column(Text)
    # AI moves are suppressed until this time (after a chair move or undo).
    ai_moves_suppressed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Finalisation jobs: {"tier2": {"status": "pending|running|done|failed|skipped", ...}, "compose": {...}, "final": {...}, "render": {...}}
    jobs_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # {summary, next_agenda[], required_next[], verify[], provenance{}}
    final_json: Mapped[str | None] = mapped_column(Text)
    # {date_iso, duration_min, room, recipients[], send_report, send_invites, ...}
    review_json: Mapped[str | None] = mapped_column(Text)
    # Last agent status for the board: {status, backlog_s, speakers[], tier2}
    agent_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    final_pass_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (Index("ix_meetpp_sessions_meeting_status", "meeting_id", "status"),)


class MeetppDocument(Base):
    """Uploaded PDF (agenda or previous notes). Listed as a paper in the report."""

    __tablename__ = "meetpp_documents"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # agenda | previous_notes
    filename: Mapped[str] = mapped_column(String(400), nullable=False)
    title: Mapped[str | None] = mapped_column(String(400))
    path: Mapped[str | None] = mapped_column(String(700))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="uploaded", nullable=False)  # uploaded | parsing | done | failed
    error: Mapped[str | None] = mapped_column(String(500))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    structured_json: Mapped[str | None] = mapped_column(Text)
    # DocSummary (contract §2.1): {format, agenda_points, subpoints,
    # open_actions, decisions, roster_members, decisions_to_take}
    summary_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class MeetppSection(Base):
    """One row of the meeting outline (FDD §4.2).

    kind: opening | previous_actions | agenda | aob | new_actions | closing
          | goal | deliverables | next_steps | planning (goal template)
    Sub-points are agenda rows with parent_id set (kind "agenda").
    """

    __tablename__ = "meetpp_sections"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(26))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    presenter: Mapped[str | None] = mapped_column(String(200))
    timebox_minutes: Mapped[int | None] = mapped_column(Integer)
    # pending | live | done | deferred | skipped
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    source: Mapped[str] = mapped_column(String(10), default="template")  # template | pdf | user | ai
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Accumulated live seconds (sections can be reopened with Back).
    elapsed_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (Index("ix_meetpp_sections_session_position", "session_id", "position"),)


class MeetppDecision(Base):
    __tablename__ = "meetpp_decisions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    series_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    ref: Mapped[str] = mapped_column(String(20), nullable=False)  # D-n
    section_id: Mapped[str | None] = mapped_column(String(26))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text)  # "that …"
    how_taken: Mapped[str | None] = mapped_column(String(600))
    # pending (to decide, prefilled from the agenda PDF) | proposed | adopted
    # | rejected | withdrawn
    status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_seq: Mapped[int | None] = mapped_column(Integer)
    # Chair confirmed the record (AI decisions start unconfirmed).
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    origin: Mapped[str] = mapped_column(String(20), default="ai")  # ai | user | pdf
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("series_id", "ref", name="uq_meetpp_decision_series_ref"),)


class MeetppVote(Base):
    """Vote record of a decision (OrgVote)."""

    __tablename__ = "meetpp_votes"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("meetpp_decisions.id"), nullable=False, unique=True)
    # voice | show_of_hands | assent | consensus | roll_call
    method: Mapped[str] = mapped_column(String(20), default="assent", nullable=False)
    question: Mapped[str | None] = mapped_column(String(500))
    tally_for: Mapped[int | None] = mapped_column(Integer)
    tally_against: Mapped[int | None] = mapped_column(Integer)
    tally_abstain: Mapped[int | None] = mapped_column(Integer)
    eligible_count: Mapped[int | None] = mapped_column(Integer)
    present_count: Mapped[int | None] = mapped_column(Integer)
    quorum_required: Mapped[int | None] = mapped_column(Integer)
    quorum_met: Mapped[bool | None] = mapped_column(Boolean)
    result: Mapped[str | None] = mapped_column(String(20))  # adopted | rejected
    outcome_note: Mapped[str | None] = mapped_column(Text)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class MeetppBallot(Base):
    """Per-member vote (OrgVoteBallot)."""

    __tablename__ = "meetpp_ballots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vote_id: Mapped[str] = mapped_column(ForeignKey("meetpp_votes.id"), nullable=False, index=True)
    person_key: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    choice: Mapped[str] = mapped_column(String(20), nullable=False)  # for | against | abstain | not_recorded
    cast_by: Mapped[str | None] = mapped_column(String(200))
    proxy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MeetppAction(Base):
    """Series-scoped action; carried across sessions until done or cancelled."""

    __tablename__ = "meetpp_actions"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("meetpp_series.id"), nullable=False, index=True)
    # Session where the action was raised.
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    ref: Mapped[str] = mapped_column(String(20), nullable=False)  # A-n
    section_id: Mapped[str | None] = mapped_column(String(26))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # JSON list of {"name": str, "person_key": str|null}
    assignees_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    due_date: Mapped[str | None] = mapped_column(String(20))  # ISO date
    # proposed | open | in_progress | done | cancelled
    status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(26))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_note: Mapped[str | None] = mapped_column(Text)
    progress_notes: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(20), default="ai")  # ai | user | pdf
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("series_id", "ref", name="uq_meetpp_action_series_ref"),
        Index("ix_meetpp_actions_series_status", "series_id", "status"),
    )


class MeetppActionReport(Base):
    """What was reported about an action at a given meeting (OrgActionMeeting)."""

    __tablename__ = "meetpp_action_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("meetpp_actions.id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status_at_report: Mapped[str | None] = mapped_column(String(20))
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("action_id", "session_id", name="uq_meetpp_action_report"),)


class MeetppMinute(Base):
    """Minutes part (FDD §8.7).

    kind: section (section_id set) | opening | adjournment | voting_record | provenance
    status: notes | composing | composed | failed | edited
    """

    __tablename__ = "meetpp_minutes"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="section", nullable=False)
    section_id: Mapped[str | None] = mapped_column(String(26))
    # JSON list of {"text": str, "evidence": [seq], "at": iso}
    notes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    narrative_md: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="notes", nullable=False)
    source_tier: Mapped[str | None] = mapped_column(String(10))  # live | refined | mixed
    verify_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    composed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "kind", "section_id", name="uq_meetpp_minute_part"),)


class MeetppMinutesVersion(Base):
    """Snapshot of the whole minutes document at publish / re-publish."""

    __tablename__ = "meetpp_minutes_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MeetppAttendee(Base):
    """Attendance of one person in one session (OrgAttendance)."""

    __tablename__ = "meetpp_attendees"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    person_key: Mapped[str] = mapped_column(String(200), nullable=False)
    roster_id: Mapped[str | None] = mapped_column(String(26))
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    username: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(300))
    # Current LiveKit identities of this person (reconnects add new ones).
    identities_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # present | represented | absent | excused | not_registered
    status: Mapped[str] = mapped_column(String(20), default="present", nullable=False)
    online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    voting: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    represented_by: Mapped[str | None] = mapped_column(String(200))
    mandate_ref: Mapped[str | None] = mapped_column(String(300))
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    talk_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    required_next: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    required_reason: Mapped[str | None] = mapped_column(String(300))
    first_joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "person_key", name="uq_meetpp_attendee_session_person"),)


class MeetppAttachment(Base):
    __tablename__ = "meetpp_attachments"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    section_id: Mapped[str | None] = mapped_column(String(26))
    kind: Mapped[str] = mapped_column(String(20), default="whiteboard", nullable=False)  # whiteboard | upload
    path: Mapped[str] = mapped_column(String(700), nullable=False)
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    caption: Mapped[str | None] = mapped_column(String(400))
    author: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MeetppSegment(Base):
    """Transcript segment. seq is monotonic per session and assigned by meeting-api.

    `text` is the tier-1 (live) text; `text_refined` the tier-2 text when it
    arrives. `utterance_id` is the agent's id used to match refinements.
    """

    __tablename__ = "meetpp_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    utterance_id: Mapped[str | None] = mapped_column(String(64), index=True)
    identity: Mapped[str] = mapped_column(String(200), nullable=False)
    person_key: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str | None] = mapped_column(String(200))
    t_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    t_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    text_refined: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # best tier available
    lang: Mapped[str | None] = mapped_column(String(8))
    avg_logprob: Mapped[float | None] = mapped_column(Float)
    no_speech_prob: Mapped[float | None] = mapped_column(Float)
    is_gap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    gap_reason: Mapped[str | None] = mapped_column(String(200))
    # Outline section the segment belongs to: the live section at ingest,
    # re-tagged to the detected topic by the tick (used for composition).
    section_id: Mapped[str | None] = mapped_column(String(26))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_meetpp_segment_session_seq"),)

    @property
    def best_text(self) -> str:
        return self.text_refined or self.text or ""


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
    """Consent per person (not per LiveKit identity, which changes on reconnect)."""

    __tablename__ = "meetpp_consents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    person_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # Last identity that posted this consent; identities are tracked on the attendee row.
    identity: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), default="accept", nullable=False)  # accept | opt_out
    consent_version: Mapped[str] = mapped_column(String(40), default="v3", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("session_id", "person_key", name="uq_meetpp_consent_session_person"),)


class MeetppOutput(Base):
    __tablename__ = "meetpp_outputs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("meetpp_sessions.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # report_pdf | agenda_pdf | ics | email
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
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (Index("ix_meetpp_llm_session_created", "session_id", "created_at"),)


MEETPP_TABLES = [
    t for t in Base.metadata.sorted_tables if t.name.startswith("meetpp_") and t.name != "meetpp_schema"
]


def ensure_schema(engine) -> None:
    """Drop Release 1 meetpp_* tables once (schema < 3) and create the v3 schema.

    Called from the app lifespan *before* Base.metadata.create_all. Idempotent.
    """
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    current = 0
    if "meetpp_schema" in existing:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version FROM meetpp_schema WHERE id = 1")).fetchone()
            current = int(row[0]) if row else 0
    if current >= SCHEMA_VERSION:
        _add_missing_columns(engine)
        return
    legacy = [name for name in existing if name.startswith("meetpp_") and name != "meetpp_schema"]
    with engine.begin() as conn:
        if legacy:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            for name in legacy:
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
            conn.execute(text("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM meetpp_schema"))
        conn.execute(text("INSERT INTO meetpp_schema (id, version) VALUES (1, :v)"), {"v": SCHEMA_VERSION})


def _add_missing_columns(engine) -> None:
    """Additive schema changes within v3: add nullable columns that exist on
    the models but not yet in the database (SQLite ALTER TABLE ADD COLUMN)."""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table in MEETPP_TABLES:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have or not col.nullable:
                continue
            ddl_type = col.type.compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'))

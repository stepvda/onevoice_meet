from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_name: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    display_title: Mapped[str] = mapped_column(String, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    owner_email: Mapped[str | None] = mapped_column(String)
    # Snapshot of the owner's preferred display name from one.witysk.org,
    # refreshed on every owner action (create / token mint). Used to show
    # "Hosted by …" to other viewers who can't fetch one.witysk.org's
    # /api/auth/me on the owner's behalf.
    owner_name: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_participants: Mapped[int] = mapped_column(Integer, default=50)
    require_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_hash: Mapped[str | None] = mapped_column(String)
    recording_mode: Mapped[str] = mapped_column(String, default="manual")  # manual | auto_on_start | off
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Owner has removed this meeting from their list (soft-delete). Recordings
    # belonging to it stay reachable so historical files don't disappear.
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional branding image displayed in the lobby and meeting top bar.
    # Stored under /var/lib/meet/branding/<id>.<ext>; served via the public
    # /api/v1/rooms/{room_name}/branding endpoint.
    branding_image_path: Mapped[str | None] = mapped_column(String)
    # Discoverability — opt-in. Default: only the owner sees the meeting on
    # their Home page. When list_for_authenticated=True, any signed-in user
    # of meet.witysk.org sees it in their Discover list. When
    # list_for_anonymous=True, even unauthenticated visitors see it on the
    # public landing page (implies authenticated visibility too).
    list_for_authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    list_for_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)

    participants: Mapped[list["MeetingParticipant"]] = relationship(back_populates="meeting")
    recordings: Mapped[list["Recording"]] = relationship(back_populates="meeting")


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    livekit_identity: Mapped[str] = mapped_column(String, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String)
    is_authenticated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_owner: Mapped[bool] = mapped_column(Boolean, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    meeting: Mapped[Meeting] = relationship(back_populates="participants")


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    egress_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    file_path: Mapped[str | None] = mapped_column(String)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False)  # running | completed | failed | deleted

    # YouTube manual publish — set when an owner clicks "Publish to YouTube".
    # `youtube_status`: null (never tried) | 'uploading' | 'published' | 'failed'
    youtube_url: Mapped[str | None] = mapped_column(String)
    youtube_video_id: Mapped[str | None] = mapped_column(String)
    youtube_status: Mapped[str | None] = mapped_column(String)
    youtube_error: Mapped[str | None] = mapped_column(String)

    meeting: Mapped[Meeting] = relationship(back_populates="recordings")


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    language: Mapped[str | None] = mapped_column(String)
    # True once the user picks a language in Settings. While False, the client
    # is free to follow the browser's preferred language on each load.
    language_set_manually: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ModerationAudit(Base):
    __tablename__ = "moderation_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    actor_user_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)  # mute | kick | presenter | recording_start | ...
    target_identity: Mapped[str | None] = mapped_column(String)
    details: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

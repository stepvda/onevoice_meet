from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
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

    # Composite index for the discover/public-meetings list — both endpoints
    # filter `is_active=True AND hidden=False AND list_for_*` and order by
    # created_at. Without this they full-scan once the table grows.
    __table_args__ = (
        Index(
            "ix_meetings_discover",
            "is_active",
            "hidden",
            "list_for_anonymous",
            "list_for_authenticated",
        ),
    )


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


class User(Base):
    """Unified user account.

    Two `kind` values:
      - "sso": auto-provisioned on first authenticated request from a one.witysk.org
        SSO user. `external_id` holds the one.witysk.org `user_id` (string). They
        always have admin rights (meeting creation). Their `password_hash` is null
        and they upload no local facepic — the SPA shows one.witysk.org's facepic
        for them and meet has no upload UI for SSO users.
      - "native": signed up directly on meet.witysk.org via /v1/auth/signup.
        Has `password_hash`. Gets a 10-day one-time trial on signup
        (trial_started_at set, trial_used flips True). After trial expires,
        meeting creation is blocked unless they hold a valid voucher entitlement
        or paid PayPal subscription (Phase 3).

    Authorization is computed lazily by `User.is_admin_now(now)` rather than
    persisted, so trial / voucher / subscription expiry doesn't need a cron
    sweep to reflect in the running app.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "sso" | "native"
    # SSO: one.witysk.org user_id. Native: null.
    external_id: Mapped[str | None] = mapped_column(String, unique=True, index=True)
    # Login email — required for native, optional for SSO (we may not see it
    # in the JWT today; the SPA can PATCH /v1/me to fill it in later).
    email: Mapped[str | None] = mapped_column(String, unique=True, index=True)
    # Optional handle for native users. SSO users keep their one.witysk.org
    # username displayed via Facepic, so this stays null for them.
    username: Mapped[str | None] = mapped_column(String, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String)
    password_hash: Mapped[str | None] = mapped_column(String)  # argon2; null for SSO
    # Local facepic upload — native users only. SSO users always render the
    # facepic served by one.witysk.org's /api/files/<facepic_path>, fetched
    # from the SPA with the user's bearer token (see Facepic.tsx).
    facepic_path: Mapped[str | None] = mapped_column(String)
    # Trial: 10-day window starting at signup. Each native user gets one shot.
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Voucher / PayPal entitlement. When granted, `entitlement_kind` is set
    # to "voucher", "paypal_monthly", or "paypal_annual" and the expiry is
    # absolute. Cleared when revoked or after a clean lapse.
    entitlement_kind: Mapped[str | None] = mapped_column(String)
    entitlement_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # TOTP 2FA — native users only. The secret is stored as base32 (the
    # natural format for TOTP); `totp_enabled` flips True after the user
    # confirms with a fresh code. Recovery codes are argon2-hashed inside
    # a JSON array; we delete a hash on use so each code is single-use.
    totp_secret: Mapped[str | None] = mapped_column(String)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    totp_recovery_hashes: Mapped[str | None] = mapped_column(String)
    # Email-OTP 2FA — alternative second factor that mails a 6-digit code
    # instead of using an authenticator app. Codes are stored in Redis with
    # a 5-minute TTL; only the on/off flag lives here.
    email_otp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    def is_admin_now(self, now: datetime) -> bool:
        """Compute meeting-creation rights at this instant.

        SSO is always admin. Native is admin while *any* of: trial active,
        voucher entitlement still valid, paid subscription still valid.
        Computed on demand so we never have to sweep stale flags."""
        if self.kind == "sso":
            return True
        if self.trial_started_at is not None:
            ends = self.trial_started_at + timedelta(days=10)
            if _aware(ends) > now:
                return True
        if self.entitlement_expires_at is not None and _aware(self.entitlement_expires_at) > now:
            return True
        return False


class Voucher(Base):
    """Single-use admin-rights voucher. Issued by privileged users (currently
    one.witysk.org user_ids 1 and 404), redeemed once by a native account, grants
    a 30-day entitlement.

    The `code` is a short human-friendly slug; we also store an HMAC of the
    code at issuance time so that even if the row is forged or imported from
    another system, redemption verifies authenticity. The HMAC key lives in
    `.env` as VOUCHER_SIGNING_KEY and never leaves the server."""
    __tablename__ = "vouchers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    code_hmac: Mapped[str] = mapped_column(String, nullable=False)  # hex(HMAC_SHA256(secret, code))
    issued_by: Mapped[str] = mapped_column(String, nullable=False)  # JWT sub of the issuer
    duration_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    note: Mapped[str | None] = mapped_column(String)
    redeemed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Issued vouchers expire 3 months after the issue date — after that they
    # can't be redeemed even if unused. Set explicitly at issue time so any
    # future change to the policy doesn't retroactively shift old vouchers.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: utcnow() + timedelta(days=90),
        nullable=False,
    )


class PaypalSubscription(Base):
    """Tracks an active PayPal subscription on a meet user. We store enough
    to reconcile webhook events without having to re-fetch the subscription
    each time. `status` mirrors PayPal's: ACTIVE, SUSPENDED, CANCELLED,
    EXPIRED. Only ACTIVE counts as a live entitlement."""
    __tablename__ = "paypal_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # I-XXXX from PayPal. Unique because PayPal won't reuse them.
    paypal_subscription_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    paypal_payer_id: Mapped[str | None] = mapped_column(String)
    plan: Mapped[str] = mapped_column(String, nullable=False)  # "monthly" | "annual"
    status: Mapped[str] = mapped_column(String, nullable=False)  # mirrors PayPal
    next_billing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class PaypalOrder(Base):
    """One-shot PayPal Order — used for the bill-once €2/month and €20/year
    purchases. Distinguished by `kind`: "monthly" grants 30 days,
    "annual" grants 365 days at capture time. Neither auto-renews."""
    __tablename__ = "paypal_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    paypal_order_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    amount: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    # "monthly" (bill-once 30d) or "annual" (bill-once 365d). Older rows
    # written before this column existed default to "annual" — that was the
    # only one-shot path at the time.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="annual")
    status: Mapped[str] = mapped_column(String, nullable=False)  # CREATED, APPROVED, COMPLETED, VOIDED
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PasswordResetToken(Base):
    """Single-use token mailed to the user when they request a password
    reset. Stored as a hex digest so a DB leak doesn't expose redeemable
    tokens — we hash the random secret on issue, mail the secret, and
    look up by digest at confirm time."""
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo on read; treat naive datetimes as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class ModerationAudit(Base):
    __tablename__ = "moderation_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    actor_user_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)  # mute | kick | presenter | recording_start | ...
    target_identity: Mapped[str | None] = mapped_column(String)
    details: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ChatMessage(Base):
    """Persistent meeting chat. Source of truth for all chat content (text,
    replies, attachments). Real-time fan-out is handled by a small "refetch"
    signal sent over LiveKit's data channel by the writing client; readers
    pull state from this table.

    Mirrors the shape of onevoice's `messages` table (DM) so the on-screen
    behaviour matches what users already know from one.witysk.org."""
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    sender_identity: Mapped[str] = mapped_column(String, nullable=False)
    sender_name: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(String, nullable=False, default="")
    reply_to_id: Mapped[int | None] = mapped_column(ForeignKey("chat_messages.id"), nullable=True, index=True)
    attachment_path: Mapped[str | None] = mapped_column(String)
    attachment_type: Mapped[str | None] = mapped_column(String)
    attachment_name: Mapped[str | None] = mapped_column(String)
    attachment_size: Mapped[int | None] = mapped_column(Integer)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    # History query is `WHERE meeting_id=? ORDER BY sent_at DESC LIMIT 1000`.
    # The single-column indexes above each cover one half; this composite is
    # a covering index for the actual hot path.
    __table_args__ = (
        Index("ix_chat_messages_meeting_sent", "meeting_id", "sent_at"),
    )


class ChatReaction(Base):
    """Per-message reaction. One reaction per (message, reactor_identity);
    re-reacting with a different emoji replaces the previous one (matches
    onevoice's MessageReaction unique-constraint behaviour)."""
    __tablename__ = "chat_reactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    reactor_identity: Mapped[str] = mapped_column(String, nullable=False)
    reactor_name: Mapped[str] = mapped_column(String, nullable=False)
    emoji: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("message_id", "reactor_identity", name="uq_chat_reaction_msg_user"),
    )

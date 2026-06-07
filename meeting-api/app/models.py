from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
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
    # Public view-only stream. When `public_enabled` is True, anyone hitting
    # /public/<public_slug> can subscribe as a hidden LiveKit viewer (no
    # publish, not in the participant panel, unlimited count). The slug is
    # unique across all meetings; the owner picks it from the in-meeting
    # settings panel.
    public_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    public_slug: Mapped[str | None] = mapped_column(String(80), unique=True, index=True, nullable=True)
    # ── Moderation policy (set at meeting creation from owner's prefs) ───
    # `waiting_room_enabled` and `auto_admit_authenticated` are stored but
    # not yet enforced server-side — the waiting-room workflow is a separate
    # feature. The remaining six policies are enforced at token-issuance
    # time and on every chat write.
    auto_admit_authenticated: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    require_name_on_join: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_mute_new_joiners: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_disable_camera_for_new: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    waiting_room_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lock_room_after_start: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_participant_screenshare: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allow_participant_chat: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Optional greeting/welcome message shown to participants in the lobby
    # before they join. Limited to a short paragraph; no markdown rendering.
    lobby_greeting: Mapped[str | None] = mapped_column(String(2000))
    # JSON-encoded list of authenticated user_ids (`user.sub`) the owner has
    # promoted to co-host. Co-hosts get moderator powers (mute / kick /
    # presenter / waiting-room admit / chat pin / lower-hand). They need to
    # rejoin the meeting after promotion to receive a fresh moderator token.
    cohost_user_ids: Mapped[str] = mapped_column(String, default="[]", nullable=False)
    # Optional iCalendar RRULE string (e.g. `FREQ=WEEKLY` or
    # `FREQ=DAILY;COUNT=10`). When set, the meeting's .ics export contains
    # a recurring VEVENT. Stored verbatim; we don't expand occurrences.
    recurrence_rule: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Meeting duration in minutes — used by the .ics generator. Defaults to
    # 60 min when omitted. Independent of `ends_at`, which is an
    # absolute-time cutoff.
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Free-form collaborative notes (plain text). Last writer wins; the
    # client debounces writes and refreshes on a data-channel hint.
    notes: Mapped[str] = mapped_column(String, default="", nullable=False)
    # Live-stream-to-X.com configuration. When `livestream_enabled` is True,
    # the in-meeting toolbar shows a Start/Stop streaming button that pipes
    # the room composite to an RTMP(S) endpoint. The URL + key are stored
    # plain because LiveKit egress needs the original credentials and only
    # the meeting owner can read or write them.
    # X.com (formerly Twitter) destination. Historical name `livestream_*`
    # without an `_x_` suffix kept for migration friendliness — semantically
    # it's the X.com slot. A meeting can stream to X, Substack, both, or
    # neither; each destination has its own enable toggle + RTMP creds.
    livestream_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    livestream_rtmps_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    livestream_stream_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Substack destination.
    livestream_substack_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    livestream_substack_rtmps_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    livestream_substack_stream_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # YouTube Live destination.
    livestream_youtube_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    livestream_youtube_rtmps_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    livestream_youtube_stream_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # YouTube live streaming has two modes:
    #   "rtmp" (default) — owner pastes the RTMP URL + stream key from
    #                      studio.youtube.com into the two columns above.
    #   "api"            — owner connects their channel via OAuth; Meet
    #                      provisions a persistent `liveStream` and rotates
    #                      `liveBroadcast` resources. The API-provisioned
    #                      ingest URL + key live in
    #                      `livestream_youtube_api_ingest_url` /
    #                      `livestream_youtube_api_ingest_key`, leaving the
    #                      manual columns untouched so switching back to
    #                      "rtmp" preserves the hand-pasted credentials.
    livestream_youtube_mode: Mapped[str] = mapped_column(String(10), default="rtmp", nullable=False)
    # OAuth refresh token (long-lived) and channel display info captured
    # during the consent callback. The access token is short-lived and
    # NOT stored — it's exchanged from the refresh token on demand.
    livestream_youtube_refresh_token: Mapped[str | None] = mapped_column(String(500), nullable=True)
    livestream_youtube_channel_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    livestream_youtube_channel_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Provisioned `liveStream` resource — reusable across many broadcasts.
    # Created once on first reconcile, reused forever.
    livestream_youtube_stream_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    livestream_youtube_api_ingest_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    livestream_youtube_api_ingest_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Currently-active `liveBroadcast` resource. Rotated every ~11h30m
    # because YouTube hard-caps single broadcasts at 12h. Watch URL is
    # the public `https://www.youtube.com/watch?v=<broadcastId>` for the
    # active broadcast; surfaced to the owner UI for sharing.
    livestream_youtube_broadcast_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    livestream_youtube_broadcast_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    livestream_youtube_watch_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Facebook Live destination.
    livestream_facebook_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    livestream_facebook_rtmps_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    livestream_facebook_stream_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Rumble destination.
    livestream_rumble_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    livestream_rumble_rtmps_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    livestream_rumble_stream_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Currently active stream egress id (set on start, cleared on stop or on
    # `egress_ended` webhook). Used by the frontend to render the right
    # toolbar button state on page reload and to refuse a second concurrent
    # start.
    livestream_egress_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Layout that the current egress was started with. Needed so a stop-one-
    # keep-other toggle (record off / stream on, or vice versa) can restart
    # the egress with the same layout the user originally picked instead of
    # silently switching to "speaker".
    current_egress_layout: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Picture-in-Picture egress layout. When enabled, recordings and
    # livestream output use a custom Web template that renders the main
    # source (active screenshare or playback participant, falling back to
    # the active speaker) full-bleed with a small corner overlay of the
    # chosen participant's camera. `pip_overlay_identity` is the LiveKit
    # participant identity whose camera goes in the corner; NULL means
    # the host hasn't picked one yet (overlay is hidden in that case).
    pip_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pip_overlay_identity: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Room-wide composition layout, applied identically to live viewers,
    # recordings, and livestreams (see PresenterSpotlight + EgressLayoutPiP
    # on the frontend). One of "single-speaker" | "speaker" | "grid".
    # Persisted so rejoiners after a room recycle see the host's last
    # choice instead of falling back to the default. The host changes it
    # via POST /meetings/{id}/layout, which updates this column AND pushes
    # the value to LiveKit room metadata so every client observes the
    # change in real time. PiP / composite-track overlay logic remains
    # independent and overrides this when active.
    # Default "grid" — fits the typical multi-participant case; the host
    # flips to speaker / single-speaker via the toolbar picker.
    room_layout: Mapped[str] = mapped_column(String(40), default="grid", nullable=False)
    # Video playback: host uploads MP4s into a playlist (PlaybackItem rows
    # below) and clicks Play to ingest them as a participant track via
    # LiveKit Ingress (URL_INPUT). `playback_enabled` is the per-meeting
    # toggle that gates whether the Play button appears in the toolbar at
    # all; `playback_ingress_id` is set while a video is actively playing.
    playback_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # When True, the last playlist item wraps back to position 0 instead of
    # ending playback. Toggleable from the same Video-playback panel.
    playback_loop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # When True, a 35-second "What's up next" rundown slide auto-plays right
    # before any playlist item whose duration > 5 minutes (auto-advance,
    # manual click, or loop-wrap — all eligible). The slide is a transient
    # MP4 generated on the fly; `playback_pending_item_id` below tracks the
    # real item that should follow the slide.
    playback_whats_up_next: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # When a "What's up next" slide is the current ingress, this holds the
    # ID of the real PlaybackItem that should start once the slide ends —
    # so `advance_after_ingress_ended` plays that item instead of the
    # next-by-position. NULL whenever no slide is in flight.
    playback_pending_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    playback_ingress_id: Mapped[str | None] = mapped_column(String, nullable=True)
    playback_current_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # When the current ingress was started — frontend computes elapsed
    # time from this for the playlist progress bar. NULL when no
    # playback is running.
    playback_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Snapshot of `current_egress_layout` taken right before playback
    # forced the egress to "single-speaker" so the playback participant
    # owns the composite. Restored when playback ends; NULL otherwise.
    layout_before_playback: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # When non-NULL, playback is paused at this offset into the current
    # item. The active ingress is a "freeze-frame" stream (a single frame
    # at this offset, looped) rather than the real item, so every viewer
    # sees the same frozen image. `playback_started_at` is NULL while
    # paused so the SPA's elapsed-time progress bar doesn't advance.
    # Resume rebuilds the original ingress at this offset and clears the
    # column.
    playback_paused_offset_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

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
    # Whisper transcript — generated automatically after egress finishes.
    # `transcript_status` flows: null → pending → processing → completed |
    # failed. The plain-text transcript is stored next to the .mp4 with a
    # `.txt` extension.
    transcript_path: Mapped[str | None] = mapped_column(String)
    transcript_status: Mapped[str | None] = mapped_column(String)
    transcript_error: Mapped[str | None] = mapped_column(String)
    transcript_summary: Mapped[str | None] = mapped_column(String)

    meeting: Mapped[Meeting] = relationship(back_populates="recordings")


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    language: Mapped[str | None] = mapped_column(String)
    # True once the user picks a language in Settings. While False, the client
    # is free to follow the browser's preferred language on each load.
    language_set_manually: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Privacy preferences (mirror of `privacy.*` in the client store). Stored
    # here so server-side enforcement (anonymising stored emails, future
    # IP suppression in logs, etc.) can read them at request time without the
    # client having to resend them on every call.
    anonymise_email_in_join_log: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dont_log_my_ip: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    # Platform admin — can manage other users, view the IDS panel, and edit
    # the IP blocklist. Distinct from the dynamic `is_admin_now()` which
    # gates meeting-creation. Bootstrapped from PLATFORM_ADMIN_EMAILS in
    # `.env` at app startup; can be toggled from the admin panel after that.
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Soft-disable: when True, all auth (login, token validation) is rejected
    # for this user. Used by the admin panel to suspend abusive accounts
    # without deleting them outright (deletion would lose audit history).
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    disable_reason: Mapped[str | None] = mapped_column(String)
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


class Poll(Base):
    """Live poll inside a meeting. Created by a host/co-host with 2-6 fixed
    options; participants vote once per poll. Results are visible to everyone
    while the poll is open."""
    __tablename__ = "polls"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    question: Mapped[str] = mapped_column(String, nullable=False)
    # JSON-encoded list of option strings.
    options_json: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")  # open | closed
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PollVote(Base):
    __tablename__ = "poll_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[str] = mapped_column(ForeignKey("polls.id", ondelete="CASCADE"), nullable=False, index=True)
    voter_identity: Mapped[str] = mapped_column(String, nullable=False)
    option_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("poll_id", "voter_identity", name="uq_poll_voter"),
    )


class Question(Base):
    """Q&A queue — anyone asks; participants upvote; host marks answered."""
    __tablename__ = "qa_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    asker_identity: Mapped[str] = mapped_column(String, nullable=False)
    asker_name: Mapped[str] = mapped_column(String, nullable=False)
    question: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")  # open | answered | dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuestionUpvote(Base):
    __tablename__ = "qa_upvotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("qa_questions.id", ondelete="CASCADE"), nullable=False, index=True)
    voter_identity: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("question_id", "voter_identity", name="uq_question_voter"),
    )


class WhiteboardShape(Base):
    """Rectangle / ellipse / text box on the shared whiteboard. Unlike the
    stroke history (append-only), shapes are addressable by `id` so they
    can be moved, resized and edited. The full set per meeting is fetched
    on tab open; live updates fan out via the data channel."""
    __tablename__ = "whiteboard_shapes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    # "rect" | "ellipse" | "text"
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # All coordinates normalised to [0,1] of the canvas, like strokes.
    x: Mapped[float] = mapped_column(nullable=False)
    y: Mapped[float] = mapped_column(nullable=False)
    w: Mapped[float] = mapped_column(nullable=False)
    h: Mapped[float] = mapped_column(nullable=False)
    color: Mapped[str] = mapped_column(String, nullable=False, default="#fbbf24")
    stroke_width: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    text: Mapped[str | None] = mapped_column(String, nullable=True)
    # Font size in CSS pixels at 720p canvas height (scales with canvas).
    font_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class WhiteboardStroke(Base):
    """One stroke (or `clear` marker) on the shared whiteboard. Persisted
    so late joiners can replay the board exactly as it stands.

    Stored as a JSON blob matching the data-channel packet format the
    whiteboard already uses, so the same shape feeds both the broadcast
    path and the replay path."""
    __tablename__ = "whiteboard_strokes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PlaybackItem(Base):
    """One MP4 in the in-meeting video-playback playlist. The host uploads
    files in advance; when Play is clicked, the items are streamed in
    `position` order via LiveKit Ingress (URL_INPUT) as a participant
    track everyone in the room sees.

    Files live under /var/lib/meet/playback/<meeting_id>/<id>.mp4 — the
    container path is shared between meeting-api (writer) and the
    livekit-ingress container (reader).
    """
    __tablename__ = "playback_items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    # 0-based ordering; lower = plays first. Rewritten on reorder.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # Display name shown in the playlist UI. Stored separately from the
    # on-disk filename so renaming the upload doesn't break disk lookups.
    filename: Mapped[str] = mapped_column(String, nullable=False)
    # When `source_item_id` is set this row is an ALIAS: it has no file
    # of its own and reads `file_path` / `file_size_bytes` / `mime_type`
    # from the source row at playback time. Aliases let the host place
    # the same video at multiple positions in the playlist without
    # duplicating the MP4 on disk. Aliases never chain — when an alias
    # is created from another alias we resolve to the root source first
    # so a single hop is always enough at playback time. file_path on
    # alias rows is an empty string ("") rather than NULL to keep the
    # column NOT NULL invariant.
    source_item_id: Mapped[str | None] = mapped_column(String, ForeignKey("playback_items.id"), nullable=True)
    file_path: Mapped[str] = mapped_column(String, nullable=False, default="")
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # MIME type captured at upload time — we cap to video/mp4 today but
    # storing the value keeps a path open for webm/ogv later.
    mime_type: Mapped[str] = mapped_column(String, nullable=False, default="video/mp4")
    # Duration in seconds, populated by the SPA on upload via
    # HTMLVideoElement.duration metadata. NULL for legacy items — the
    # frontend renders an indeterminate progress bar in that case.
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_playback_items_meeting_position", "meeting_id", "position"),
    )


class LivestreamDestinationState(Base):
    """Per-platform "is the RTMP push to this destination currently
    healthy?" snapshot. Updated by the LiveKit `egress_updated` webhook
    every time the egress reports per-URL `stream_results[]`. Read by
    the frontend (`GET /v1/meetings/{id}/stream/destinations`) so the
    host can see at a glance which destinations are streaming and
    which are failing, with the egress's own error string.

    Status vocabulary mirrors LiveKit's `StreamInfo.Status`:
      - "idle"       : we have credentials but no egress has touched this URL yet
      - "streaming"  : egress is actively pushing bytes (status=ACTIVE in LK)
      - "failed"     : egress couldn't connect / was rejected
      - "complete"   : egress finished sending (typically when the stream stops)
    """
    __tablename__ = "livestream_destination_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    # "x" / "substack" / "youtube" / "facebook" / "rumble". The same id
    # surfaces in LIVESTREAM_DESTINATIONS in egress_mgr and in the
    # frontend's per-platform metadata.
    platform_id: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="idle")
    # When `status=failed` this carries the human-readable reason
    # straight from LiveKit (e.g. "Failed to connect: 'publish' cmd
    # failed: connection closed remotely"). NULL otherwise.
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    # Concurrent viewer count + when it was last polled. Populated by the
    # YouTube supervisor for `platform_id="youtube"` rows when the meeting
    # is in API mode. Other platforms leave these NULL (no API to query).
    viewer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    viewer_count_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("meeting_id", "platform_id", name="uq_dest_state_meeting_platform"),
    )


class MeetingFeedback(Base):
    """Post-meeting NPS / satisfaction rating. One row per submission;
    a single participant can submit at most once but we don't enforce that
    server-side — the client suppresses the modal after the first submit."""
    __tablename__ = "meeting_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), nullable=False, index=True)
    participant_identity: Mapped[str | None] = mapped_column(String, nullable=True)
    participant_name: Mapped[str | None] = mapped_column(String, nullable=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 0..10
    comment: Mapped[str | None] = mapped_column(String, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


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
    # Owner-only pinning. When `pinned_at` is set, the message is rendered
    # at the top of the chat panel for everyone in the meeting. Multiple
    # messages may be pinned; they appear in descending pin order.
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    pinned_by: Mapped[str | None] = mapped_column(String, nullable=True)
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


class BlockedIP(Base):
    """Persistent IP / CIDR / dash-range blocklist managed by platform admins.

    `ip_address` accepts three forms — checked in this order at lookup time:
      - exact IP: "203.0.113.5"
      - CIDR: "203.0.113.0/24" or "2001:db8::/32"
      - dash range (IPv4 last octet): "203.0.113.5-50"
    """
    __tablename__ = "blocked_ips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ip_address: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    blocked_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    block_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SecurityEvent(Base):
    """IDS event log. Each row is one observed signal (auth failure, rate-limit
    hit, forbidden access, etc.) — the detector aggregates these in sliding
    windows in memory and decides when to temp-block. Stored here so the admin
    panel can show history beyond what's still in the in-memory window."""
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # info | warn | block | alert
    ip_address: Mapped[str | None] = mapped_column(String(64), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    handle: Mapped[str | None] = mapped_column(String(255))
    path: Mapped[str | None] = mapped_column(String(255))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

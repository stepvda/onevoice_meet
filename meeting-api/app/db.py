from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.split("///", 1)[-1]
    if db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _existing_columns(conn, table: str) -> set[str]:
    """SQLite-only helper: returns the set of column names on `table`."""
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def lightweight_migrate() -> None:
    """Idempotent ALTER TABLEs to bring an existing SQLite DB up to date.

    Used because we're not running Alembic. Each clause checks for missing
    columns and adds them with sensible defaults. Safe to call on every
    startup; no-op once columns exist.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        for table, additions in (
            ("recordings", (
                ("youtube_url", "ALTER TABLE recordings ADD COLUMN youtube_url TEXT"),
                ("youtube_video_id", "ALTER TABLE recordings ADD COLUMN youtube_video_id TEXT"),
                ("youtube_status", "ALTER TABLE recordings ADD COLUMN youtube_status TEXT"),
                ("youtube_error", "ALTER TABLE recordings ADD COLUMN youtube_error TEXT"),
                ("transcript_path", "ALTER TABLE recordings ADD COLUMN transcript_path TEXT"),
                ("transcript_status", "ALTER TABLE recordings ADD COLUMN transcript_status TEXT"),
                ("transcript_error", "ALTER TABLE recordings ADD COLUMN transcript_error TEXT"),
                ("transcript_summary", "ALTER TABLE recordings ADD COLUMN transcript_summary TEXT"),
            )),
            ("meetings", (
                ("hidden", "ALTER TABLE meetings ADD COLUMN hidden BOOLEAN DEFAULT 0 NOT NULL"),
                ("branding_image_path", "ALTER TABLE meetings ADD COLUMN branding_image_path TEXT"),
                ("list_for_authenticated", "ALTER TABLE meetings ADD COLUMN list_for_authenticated BOOLEAN DEFAULT 0 NOT NULL"),
                ("list_for_anonymous", "ALTER TABLE meetings ADD COLUMN list_for_anonymous BOOLEAN DEFAULT 0 NOT NULL"),
                ("owner_name", "ALTER TABLE meetings ADD COLUMN owner_name TEXT"),
                ("auto_admit_authenticated", "ALTER TABLE meetings ADD COLUMN auto_admit_authenticated BOOLEAN DEFAULT 1 NOT NULL"),
                ("require_name_on_join", "ALTER TABLE meetings ADD COLUMN require_name_on_join BOOLEAN DEFAULT 1 NOT NULL"),
                ("auto_mute_new_joiners", "ALTER TABLE meetings ADD COLUMN auto_mute_new_joiners BOOLEAN DEFAULT 0 NOT NULL"),
                ("auto_disable_camera_for_new", "ALTER TABLE meetings ADD COLUMN auto_disable_camera_for_new BOOLEAN DEFAULT 0 NOT NULL"),
                ("waiting_room_enabled", "ALTER TABLE meetings ADD COLUMN waiting_room_enabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("lock_room_after_start", "ALTER TABLE meetings ADD COLUMN lock_room_after_start BOOLEAN DEFAULT 0 NOT NULL"),
                ("allow_participant_screenshare", "ALTER TABLE meetings ADD COLUMN allow_participant_screenshare BOOLEAN DEFAULT 1 NOT NULL"),
                ("allow_participant_chat", "ALTER TABLE meetings ADD COLUMN allow_participant_chat BOOLEAN DEFAULT 1 NOT NULL"),
                ("lobby_greeting", "ALTER TABLE meetings ADD COLUMN lobby_greeting TEXT"),
                ("cohost_user_ids", "ALTER TABLE meetings ADD COLUMN cohost_user_ids TEXT DEFAULT '[]' NOT NULL"),
                ("recurrence_rule", "ALTER TABLE meetings ADD COLUMN recurrence_rule TEXT"),
                ("duration_minutes", "ALTER TABLE meetings ADD COLUMN duration_minutes INTEGER"),
                ("notes", "ALTER TABLE meetings ADD COLUMN notes TEXT DEFAULT '' NOT NULL"),
                ("livestream_enabled", "ALTER TABLE meetings ADD COLUMN livestream_enabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("livestream_rtmps_url", "ALTER TABLE meetings ADD COLUMN livestream_rtmps_url TEXT"),
                ("livestream_stream_key", "ALTER TABLE meetings ADD COLUMN livestream_stream_key TEXT"),
                ("livestream_egress_id", "ALTER TABLE meetings ADD COLUMN livestream_egress_id TEXT"),
                ("current_egress_layout", "ALTER TABLE meetings ADD COLUMN current_egress_layout TEXT"),
                ("livestream_substack_enabled", "ALTER TABLE meetings ADD COLUMN livestream_substack_enabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("livestream_substack_rtmps_url", "ALTER TABLE meetings ADD COLUMN livestream_substack_rtmps_url TEXT"),
                ("livestream_substack_stream_key", "ALTER TABLE meetings ADD COLUMN livestream_substack_stream_key TEXT"),
                ("livestream_youtube_enabled", "ALTER TABLE meetings ADD COLUMN livestream_youtube_enabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("livestream_youtube_rtmps_url", "ALTER TABLE meetings ADD COLUMN livestream_youtube_rtmps_url TEXT"),
                ("livestream_youtube_stream_key", "ALTER TABLE meetings ADD COLUMN livestream_youtube_stream_key TEXT"),
                ("livestream_facebook_enabled", "ALTER TABLE meetings ADD COLUMN livestream_facebook_enabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("livestream_facebook_rtmps_url", "ALTER TABLE meetings ADD COLUMN livestream_facebook_rtmps_url TEXT"),
                ("livestream_facebook_stream_key", "ALTER TABLE meetings ADD COLUMN livestream_facebook_stream_key TEXT"),
                ("livestream_rumble_enabled", "ALTER TABLE meetings ADD COLUMN livestream_rumble_enabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("livestream_rumble_rtmps_url", "ALTER TABLE meetings ADD COLUMN livestream_rumble_rtmps_url TEXT"),
                ("livestream_rumble_stream_key", "ALTER TABLE meetings ADD COLUMN livestream_rumble_stream_key TEXT"),
                ("playback_enabled", "ALTER TABLE meetings ADD COLUMN playback_enabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("playback_loop", "ALTER TABLE meetings ADD COLUMN playback_loop BOOLEAN DEFAULT 0 NOT NULL"),
                ("playback_ingress_id", "ALTER TABLE meetings ADD COLUMN playback_ingress_id TEXT"),
                ("playback_current_item_id", "ALTER TABLE meetings ADD COLUMN playback_current_item_id TEXT"),
                # Tracks when the active ingress started so the SPA can
                # compute elapsed time for the progress bar.
                ("playback_started_at", "ALTER TABLE meetings ADD COLUMN playback_started_at TIMESTAMP"),
                # Layout the egress was using right before playback
                # forced it to "single-speaker". Restored when playback
                # ends so the host's chosen recording layout sticks.
                ("layout_before_playback", "ALTER TABLE meetings ADD COLUMN layout_before_playback TEXT"),
                ("public_enabled", "ALTER TABLE meetings ADD COLUMN public_enabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("public_slug", "ALTER TABLE meetings ADD COLUMN public_slug TEXT"),
            )),
            ("chat_messages", (
                ("reply_to_id", "ALTER TABLE chat_messages ADD COLUMN reply_to_id INTEGER REFERENCES chat_messages(id)"),
                ("attachment_path", "ALTER TABLE chat_messages ADD COLUMN attachment_path TEXT"),
                ("attachment_type", "ALTER TABLE chat_messages ADD COLUMN attachment_type TEXT"),
                ("attachment_name", "ALTER TABLE chat_messages ADD COLUMN attachment_name TEXT"),
                ("attachment_size", "ALTER TABLE chat_messages ADD COLUMN attachment_size INTEGER"),
                ("pinned_at", "ALTER TABLE chat_messages ADD COLUMN pinned_at TIMESTAMP"),
                ("pinned_by", "ALTER TABLE chat_messages ADD COLUMN pinned_by TEXT"),
            )),
            ("vouchers", (
                # Backfill with a far-future date so any vouchers already
                # issued before this column existed remain redeemable.
                ("expires_at", "ALTER TABLE vouchers ADD COLUMN expires_at TIMESTAMP DEFAULT '2099-12-31 23:59:59'"),
            )),
            ("paypal_orders", (
                # Pre-existing one-shot orders were all annual; backfill
                # accordingly so capture-time entitlement granting picks
                # the right duration on legacy rows.
                ("kind", "ALTER TABLE paypal_orders ADD COLUMN kind TEXT DEFAULT 'annual' NOT NULL"),
            )),
            ("user_preferences", (
                ("anonymise_email_in_join_log", "ALTER TABLE user_preferences ADD COLUMN anonymise_email_in_join_log BOOLEAN DEFAULT 0 NOT NULL"),
                ("dont_log_my_ip", "ALTER TABLE user_preferences ADD COLUMN dont_log_my_ip BOOLEAN DEFAULT 0 NOT NULL"),
            )),
            ("playback_items", (
                # Aliases reference a source row's file. NULL = self-contained
                # item (own file on disk). Non-NULL = no file of its own; the
                # playback layer resolves to the source's file. SQLite ALTER
                # TABLE can't add a FOREIGN KEY constraint to an existing
                # table, but the model annotates it and we never join-check.
                ("source_item_id", "ALTER TABLE playback_items ADD COLUMN source_item_id TEXT"),
                # Duration in seconds — populated by the SPA on upload via
                # HTMLVideoElement.duration. NULL for legacy items.
                ("duration_seconds", "ALTER TABLE playback_items ADD COLUMN duration_seconds REAL"),
            )),
            ("users", (
                ("totp_secret", "ALTER TABLE users ADD COLUMN totp_secret TEXT"),
                ("totp_enabled", "ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("totp_recovery_hashes", "ALTER TABLE users ADD COLUMN totp_recovery_hashes TEXT"),
                ("email_otp_enabled", "ALTER TABLE users ADD COLUMN email_otp_enabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("is_platform_admin", "ALTER TABLE users ADD COLUMN is_platform_admin BOOLEAN DEFAULT 0 NOT NULL"),
                ("is_disabled", "ALTER TABLE users ADD COLUMN is_disabled BOOLEAN DEFAULT 0 NOT NULL"),
                ("disable_reason", "ALTER TABLE users ADD COLUMN disable_reason TEXT"),
            )),
        ):
            try:
                cols = _existing_columns(conn, table)
            except Exception:
                continue
            for name, ddl in additions:
                if name not in cols:
                    conn.exec_driver_sql(ddl)

        # Composite indexes on hot query paths. `metadata.create_all` doesn't
        # add indexes to pre-existing tables, so we issue these explicitly.
        # CREATE INDEX IF NOT EXISTS is a no-op once the index is in place.
        for ddl in (
            "CREATE INDEX IF NOT EXISTS ix_meetings_discover "
            "ON meetings (is_active, hidden, list_for_anonymous, list_for_authenticated)",
            "CREATE INDEX IF NOT EXISTS ix_chat_messages_meeting_sent "
            "ON chat_messages (meeting_id, sent_at)",
            # MyMeetings page: filter owner_user_id + hidden, ORDER BY created_at DESC.
            "CREATE INDEX IF NOT EXISTS ix_meetings_owner_created "
            "ON meetings (owner_user_id, created_at)",
            # Hot path in egress_mgr._current_state and recordings listing —
            # filter by meeting_id then status. Without this it scans the
            # recordings table on every record-start / stream-start call.
            "CREATE INDEX IF NOT EXISTS ix_recordings_meeting_status "
            "ON recordings (meeting_id, status)",
            # Webhook handler: filter_by(livestream_egress_id=info.egress_id)
            # on every LiveKit egress event.
            "CREATE INDEX IF NOT EXISTS ix_meetings_livestream_egress "
            "ON meetings (livestream_egress_id)",
            # Public-page slug must be globally unique; lookup is hot on
            # the /public/<slug> endpoints (info + viewer-token).
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_meetings_public_slug "
            "ON meetings (public_slug) WHERE public_slug IS NOT NULL",
        ):
            try:
                conn.exec_driver_sql(ddl)
            except Exception:
                continue

        # Partial unique index needs a clean slate. Older rows from before
        # the dedupe logic was added may have duplicates that block the
        # CREATE; collapse those to one active row per (meeting, identity)
        # by marking the older copies as left=now, then create the index.
        try:
            conn.exec_driver_sql(
                """
                UPDATE meeting_participants
                SET left_at = CURRENT_TIMESTAMP
                WHERE id IN (
                    SELECT mp.id
                    FROM meeting_participants mp
                    JOIN meeting_participants newer
                      ON newer.meeting_id = mp.meeting_id
                     AND newer.livekit_identity = mp.livekit_identity
                     AND newer.left_at IS NULL
                     AND newer.id > mp.id
                    WHERE mp.left_at IS NULL
                )
                """
            )
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_meeting_participants_active "
                "ON meeting_participants (meeting_id, livekit_identity) "
                "WHERE left_at IS NULL"
            )
        except Exception:
            pass

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
            )),
            ("chat_messages", (
                ("reply_to_id", "ALTER TABLE chat_messages ADD COLUMN reply_to_id INTEGER REFERENCES chat_messages(id)"),
                ("attachment_path", "ALTER TABLE chat_messages ADD COLUMN attachment_path TEXT"),
                ("attachment_type", "ALTER TABLE chat_messages ADD COLUMN attachment_type TEXT"),
                ("attachment_name", "ALTER TABLE chat_messages ADD COLUMN attachment_name TEXT"),
                ("attachment_size", "ALTER TABLE chat_messages ADD COLUMN attachment_size INTEGER"),
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
        ):
            try:
                conn.exec_driver_sql(ddl)
            except Exception:
                continue

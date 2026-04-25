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
            )),
            ("chat_messages", (
                ("reply_to_id", "ALTER TABLE chat_messages ADD COLUMN reply_to_id INTEGER REFERENCES chat_messages(id)"),
                ("attachment_path", "ALTER TABLE chat_messages ADD COLUMN attachment_path TEXT"),
                ("attachment_type", "ALTER TABLE chat_messages ADD COLUMN attachment_type TEXT"),
                ("attachment_name", "ALTER TABLE chat_messages ADD COLUMN attachment_name TEXT"),
                ("attachment_size", "ALTER TABLE chat_messages ADD COLUMN attachment_size INTEGER"),
            )),
        ):
            try:
                cols = _existing_columns(conn, table)
            except Exception:
                continue
            for name, ddl in additions:
                if name not in cols:
                    conn.exec_driver_sql(ddl)

"""Daily Meet++ retention job.

Transcript segments 30 days after publish/end; uploaded PDFs 90 days;
LLM call metadata 180 days; TTS cache 7-day LRU with a 200 MB cap.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.meetpp.models import MeetppDocument, MeetppLlmCall, MeetppSegment, MeetppSession

log = logging.getLogger("app.meetpp")

TTS_MAX_BYTES = 200 * 1024 * 1024


def run_retention() -> dict:
    result = {"segments": 0, "documents": 0, "llm_calls": 0, "tts_files": 0}
    now = datetime.now(timezone.utc)
    transcript_cutoff = now - timedelta(days=settings.meetpp_transcript_retention_days)
    upload_cutoff = now - timedelta(days=settings.meetpp_upload_retention_days)
    llm_cutoff = now - timedelta(days=180)
    db = SessionLocal()
    try:
        # Transcript segments for sessions finished before the cutoff.
        done = (
            db.query(MeetppSession.id)
            .filter(
                MeetppSession.status.in_(("review", "published", "aborted")),
                MeetppSession.ended_at.isnot(None),
                MeetppSession.ended_at < transcript_cutoff,
            )
            .all()
        )
        ids = [r[0] for r in done]
        if ids:
            result["segments"] = (
                db.query(MeetppSegment).filter(MeetppSegment.session_id.in_(ids)).delete(synchronize_session=False)
            )
        # Uploaded documents past their retention, plus their files.
        old_docs = db.query(MeetppDocument).filter(MeetppDocument.created_at < upload_cutoff).all()
        for doc in old_docs:
            if doc.path:
                try:
                    Path(doc.path).unlink(missing_ok=True)
                except OSError:
                    pass
            db.delete(doc)
            result["documents"] += 1
        result["llm_calls"] = (
            db.query(MeetppLlmCall).filter(MeetppLlmCall.created_at < llm_cutoff).delete(synchronize_session=False)
        )
        db.commit()
    except Exception:  # noqa: BLE001
        log.exception("meetpp: retention job failed")
        db.rollback()
    finally:
        db.close()

    result["tts_files"] = _prune_tts_cache()
    return result


def _prune_tts_cache() -> int:
    base = Path(settings.meetpp_data_dir) / "tts"
    if not base.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    files = []
    total = 0
    for f in base.glob("*"):
        if not f.is_file():
            continue
        stat = f.stat()
        if stat.st_mtime < cutoff:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        total += stat.st_size
        files.append((stat.st_mtime, stat.st_size, f))
    removed = 0
    if total > TTS_MAX_BYTES:
        files.sort()
        for _mtime, size, f in files:
            try:
                f.unlink()
                total -= size
                removed += 1
            except OSError:
                pass
            if total <= TTS_MAX_BYTES:
                break
    return removed

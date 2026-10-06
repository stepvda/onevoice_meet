"""Daily Meet++ retention job.

- per-utterance audio (MEETPP_DATA_DIR/<sid>/audio/) of sessions not
  published within MEETPP_AUDIO_RETENTION_DAYS after their end (published
  sessions lose it at publish);
- transcript segments 30 days after the end of finished sessions;
- uploaded PDFs after 90 days; LLM call metadata after 180 days;
- the TTS clip cache: 7-day LRU with a 200 MB cap.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.meetpp.models import MeetppDocument, MeetppLlmCall, MeetppSegment, MeetppSession

log = logging.getLogger("app.meetpp")

TTS_MAX_BYTES = 200 * 1024 * 1024
TTS_MAX_AGE_DAYS = 7


def run_retention() -> dict:
    result = {"segments": 0, "documents": 0, "llm_calls": 0, "tts_files": 0, "audio_dirs": 0}
    now = datetime.now(timezone.utc)
    transcript_cutoff = now - timedelta(days=settings.meetpp_transcript_retention_days)
    upload_cutoff = now - timedelta(days=settings.meetpp_upload_retention_days)
    audio_cutoff = now - timedelta(days=settings.meetpp_audio_retention_days)
    llm_cutoff = now - timedelta(days=180)
    db = SessionLocal()
    try:
        finished = db.query(MeetppSession).filter(
            MeetppSession.status.in_(("finalising", "review", "published", "aborted")),
            MeetppSession.ended_at.isnot(None),
        ).all()
        old_ids = []
        for s in finished:
            ended = s.ended_at if s.ended_at.tzinfo else s.ended_at.replace(tzinfo=timezone.utc)
            if ended < audio_cutoff and purge_audio(s.id):
                result["audio_dirs"] += 1
            if ended < transcript_cutoff and s.status in ("review", "published", "aborted"):
                old_ids.append(s.id)
        if old_ids:
            result["segments"] = (
                db.query(MeetppSegment).filter(MeetppSegment.session_id.in_(old_ids)).delete(synchronize_session=False)
            )
        for doc in db.query(MeetppDocument).filter(MeetppDocument.created_at < upload_cutoff).all():
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
    result["tts_files"] = prune_tts_cache()
    return result


def purge_audio(session_id: str) -> bool:
    base = Path(settings.meetpp_data_dir) / session_id / "audio"
    if not base.exists():
        return False
    shutil.rmtree(base, ignore_errors=True)
    return True


def prune_tts_cache() -> int:
    base = Path(settings.meetpp_data_dir) / "tts"
    if not base.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - TTS_MAX_AGE_DAYS * 86400
    files = []
    total = 0
    removed = 0
    for f in base.glob("*"):
        if not f.is_file():
            continue
        st = f.stat()
        # Recently played clips count as used (atime when available).
        used = max(st.st_mtime, getattr(st, "st_atime", 0.0))
        if used < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
            continue
        total += st.st_size
        files.append((used, st.st_size, f))
    if total > TTS_MAX_BYTES:
        files.sort()
        for _used, size, f in files:
            try:
                f.unlink()
                total -= size
                removed += 1
            except OSError:
                pass
            if total <= TTS_MAX_BYTES:
                break
    return removed

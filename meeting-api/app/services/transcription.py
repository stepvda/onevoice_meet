"""
Self-hosted transcription pipeline.

Triggered from the LiveKit `egress_ended` webhook handler once the MP4 is
on disk. The job runs in a `BackgroundTasks` slot so the webhook returns
immediately; on completion the transcript text is persisted next to the
.mp4, the Recording row is updated, and an email is sent to every
participant whose address we captured.

How it works:
  1. `ffmpeg` decodes the recording to a 16 kHz mono WAV. whisper.cpp
     expects this exact format and refuses anything else.
  2. The WAV is POSTed (multipart/form-data) to the local whisper.cpp
     server at `settings.whisper_url`. Default model is `ggml-base.en`.
  3. The plain-text transcript is saved as `<basename>.txt` next to the
     MP4 and the Recording row is flipped to `transcript_status="completed"`.
  4. If at least one participant email is on file, that transcript is
     emailed to all of them with the .txt attached.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import httpx

from app.config import settings
from app.db import SessionLocal
from app.models import Meeting, MeetingParticipant, Recording

log = logging.getLogger(__name__)


def _transcript_path_for(mp4: Path) -> Path:
    return mp4.with_suffix(".txt")


async def _decode_to_wav(mp4: Path, wav: Path) -> None:
    """Run ffmpeg to convert the MP4 to a 16 kHz mono WAV (the format
    whisper.cpp's server requires). Raises if ffmpeg exits non-zero."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        "-i", str(mp4),
        "-vn",                  # drop video
        "-ac", "1",             # mono
        "-ar", "16000",         # 16 kHz
        "-acodec", "pcm_s16le", # uncompressed 16-bit
        str(wav),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}): {err.decode(errors='ignore')[:300]}")


async def _whisper_transcribe(wav: Path) -> str:
    """POST the WAV to whisper.cpp's `/inference` endpoint and return the
    plain-text transcript. Retries on transient connection failures with
    exponential backoff; surfaces HTTP errors otherwise."""
    backoff = 1.0
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
                with open(wav, "rb") as fh:
                    files = {
                        "file": (wav.name, fh, "audio/wav"),
                        "response_format": (None, "text"),
                        "temperature": (None, "0.0"),
                    }
                    r = await client.post(settings.whisper_url, files=files)
                if r.status_code >= 500 or r.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"status {r.status_code}", request=r.request, response=r
                    )
                r.raise_for_status()
                return r.text
        except (httpx.HTTPError, httpx.NetworkError, OSError) as e:
            last_err = e
            if attempt == 3:
                break
            await asyncio.sleep(backoff)
            backoff *= 2
    raise last_err if last_err else RuntimeError("transcription failed")


async def transcribe_recording(recording_id: str) -> None:
    """Decode + transcribe a completed recording, write `<basename>.txt`
    next to the MP4, and email it to participants."""
    with SessionLocal() as db:
        rec = db.query(Recording).filter_by(id=recording_id).first()
        if not rec or not rec.file_path:
            log.warning("TRANSCRIBE_SKIP reason=missing recording=%s", recording_id)
            return
        mp4 = Path(rec.file_path)
        if not mp4.exists():
            log.warning("TRANSCRIBE_SKIP reason=file_missing recording=%s path=%s", recording_id, mp4)
            return
        rec.transcript_status = "processing"
        rec.transcript_error = None
        db.commit()

    # Decode to a temp WAV. NamedTemporaryFile auto-cleans on close.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = Path(tmp.name)

    try:
        await _decode_to_wav(mp4, wav_path)
        text = await _whisper_transcribe(wav_path)
    except Exception as e:  # noqa: BLE001
        log.error("TRANSCRIBE_FAIL recording=%s err=%s", recording_id, e)
        with SessionLocal() as db:
            rec = db.query(Recording).filter_by(id=recording_id).first()
            if rec:
                rec.transcript_status = "failed"
                rec.transcript_error = str(e)[:500]
                db.commit()
        return
    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass

    txt_path = _transcript_path_for(mp4)
    try:
        txt_path.write_text(text, encoding="utf-8")
    except OSError as e:
        log.error("TRANSCRIBE_WRITE_FAIL recording=%s err=%s", recording_id, e)
        with SessionLocal() as db:
            rec = db.query(Recording).filter_by(id=recording_id).first()
            if rec:
                rec.transcript_status = "failed"
                rec.transcript_error = f"write: {e}"[:500]
                db.commit()
        return

    with SessionLocal() as db:
        rec = db.query(Recording).filter_by(id=recording_id).first()
        if rec:
            rec.transcript_path = str(txt_path)
            rec.transcript_status = "completed"
            db.commit()
    log.info("TRANSCRIBE_OK recording=%s bytes=%d", recording_id, len(text))

    # Best-effort: mail the transcript to every participant we have an
    # address for. Failures here don't roll back the transcript — the user
    # still gets the .txt download in the Recordings list.
    try:
        await email_transcript_to_participants(recording_id)
    except Exception as e:  # noqa: BLE001
        log.error("TRANSCRIPT_MAIL_FAIL recording=%s err=%s", recording_id, e)


def _participant_emails(meeting_id: str) -> list[str]:
    """Distinct, sane-looking email addresses from MeetingParticipant rows
    plus the meeting owner. Skips obviously-anonymised forms like
    `j***@example.com`."""
    out: set[str] = set()
    with SessionLocal() as db:
        m = db.query(Meeting).filter_by(id=meeting_id).first()
        if not m:
            return []
        if m.owner_email and "*" not in m.owner_email and "@" in m.owner_email:
            out.add(m.owner_email.strip().lower())
        rows = db.query(MeetingParticipant).filter_by(meeting_id=meeting_id).all()
        for r in rows:
            if not r.email:
                continue
            addr = r.email.strip().lower()
            if "*" in addr or "@" not in addr:
                continue
            out.add(addr)
    return sorted(out)


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def email_transcript_to_participants(recording_id: str) -> None:
    """Send the raw transcript to every captured participant email. No LLM
    summary — just the text and the attachment."""
    from app.services.email import send_email

    with SessionLocal() as db:
        rec = db.query(Recording).filter_by(id=recording_id).first()
        if not rec or not rec.transcript_path:
            return
        meeting = db.query(Meeting).filter_by(id=rec.meeting_id).first()
        if not meeting:
            return
        meeting_id = meeting.id
        meeting_title = meeting.display_title

    try:
        transcript_text = Path(rec.transcript_path).read_text(encoding="utf-8")
    except OSError:
        return
    if not transcript_text.strip():
        return

    addrs = _participant_emails(meeting_id)
    if not addrs:
        log.info("TRANSCRIPT_MAIL_NO_RECIPIENTS recording=%s", recording_id)
        return

    # Body: a short heading + the first ~2 000 chars of the transcript
    # inline (so the email is useful even if the user can't open the
    # attachment), then the full transcript as a .txt attachment.
    preview = transcript_text.strip()
    if len(preview) > 2000:
        preview = preview[:2000].rsplit(" ", 1)[0] + "…"
    body_html = (
        f"<p>The transcript of the meeting <strong>{_escape(meeting_title)}</strong> is attached.</p>"
        f"<pre style=\"white-space:pre-wrap;font-family:system-ui,sans-serif;line-height:1.4;\">{_escape(preview)}</pre>"
        f"<p style=\"color:#888;font-size:12px;margin-top:24px;\">"
        f"Generated automatically from the meeting recording. The full transcript is attached as a text file."
        f"</p>"
    )
    body_text = f"Transcript of {meeting_title}\n\n{preview}\n\n— Full transcript attached as .txt —"

    import base64 as _b64
    transcript_attachment = {
        "filename": Path(rec.transcript_path).name,
        "content": _b64.b64encode(transcript_text.encode("utf-8")).decode("ascii"),
        "content_type": "text/plain",
    }
    for addr in addrs:
        try:
            await send_email(
                to=addr,
                subject=f"Transcript: {meeting_title}",
                html=body_html,
                text=body_text,
                attachments=[transcript_attachment],
            )
        except Exception as e:  # noqa: BLE001
            log.error("TRANSCRIPT_MAIL_FAIL recording=%s to=%s err=%s", recording_id, addr, e)

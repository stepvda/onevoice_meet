"""
Whisper-based post-recording transcription.

Triggered from the LiveKit `egress_ended` webhook handler once the MP4 is
on disk. The job runs in a `BackgroundTasks` slot so the webhook returns
immediately; on completion the transcript text is persisted next to the
.mp4 and the Recording row is updated.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.config import settings
from app.db import SessionLocal
from app.models import Meeting, MeetingParticipant, Recording

log = logging.getLogger(__name__)

_API_URL = "https://api.openai.com/v1/audio/transcriptions"


def _transcript_path_for(mp4: Path) -> Path:
    return mp4.with_suffix(".txt")


async def transcribe_recording(recording_id: str) -> None:
    """Whisper-transcribe a completed recording, write `<basename>.txt`
    next to the MP4 and persist the path. Safe to call when the API key
    is empty (no-ops with a logged warning)."""
    if not settings.openai_api_key:
        log.info("TRANSCRIBE_SKIP reason=no_api_key recording=%s", recording_id)
        return

    # Look up the recording in its own session so this runs cleanly from a
    # background task (it doesn't share the request-scoped session).
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

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            with open(mp4, "rb") as fh:
                files = {
                    "file": (mp4.name, fh, "video/mp4"),
                    "model": (None, settings.openai_whisper_model),
                    "response_format": (None, "text"),
                }
                r = await client.post(
                    _API_URL,
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    files=files,
                )
                r.raise_for_status()
                text = r.text
    except Exception as e:  # noqa: BLE001
        log.error("TRANSCRIBE_FAIL recording=%s err=%s", recording_id, e)
        with SessionLocal() as db:
            rec = db.query(Recording).filter_by(id=recording_id).first()
            if rec:
                rec.transcript_status = "failed"
                rec.transcript_error = str(e)[:500]
                db.commit()
        return

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

    # Best-effort: summarise + email participants. Failures here don't roll
    # back the transcript — the user still gets the .txt download.
    try:
        await summarise_and_email(recording_id)
    except Exception as e:  # noqa: BLE001
        log.error("SUMMARY_FAIL recording=%s err=%s", recording_id, e)


_SUMMARY_PROMPT = (
    "You are a meeting-summary assistant. Given a transcript of a video "
    "meeting, produce a short summary (3–6 sentences), followed by a "
    "bulleted list of decisions and action items. Use Markdown headings "
    "(## Summary, ## Decisions, ## Action items). Do not invent details — "
    "only include items the transcript supports. If the transcript is too "
    "short or trivial to summarise, reply with: 'No notable content to "
    "summarise.'"
)


async def _summarise(text: str) -> str | None:
    if not settings.openai_api_key:
        return None
    # Whisper transcripts can be very long. Cap input to the model's
    # context window with a conservative byte cut — ~12 000 words covers
    # most 1-hour meetings; longer ones get the head + tail truncated.
    MAX_CHARS = 60_000
    if len(text) > MAX_CHARS:
        head = text[: MAX_CHARS // 2]
        tail = text[-MAX_CHARS // 2 :]
        text = f"{head}\n\n[... transcript truncated for length ...]\n\n{tail}"
    payload = {
        "model": settings.openai_summary_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": _SUMMARY_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            return (data["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as e:  # noqa: BLE001
        log.error("SUMMARY_OPENAI_FAIL err=%s", e)
        return None


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


async def summarise_and_email(recording_id: str) -> None:
    """Generate a meeting summary from the saved transcript and email it
    (with the transcript attached) to every participant whose address we
    captured. Safe to call when OPENAI_API_KEY is empty (logs + returns)."""
    if not settings.openai_api_key:
        return
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

    summary_md = await _summarise(transcript_text)
    if not summary_md:
        return

    # Persist the summary for later display.
    with SessionLocal() as db:
        rec = db.query(Recording).filter_by(id=recording_id).first()
        if rec:
            rec.transcript_summary = summary_md
            db.commit()

    addrs = _participant_emails(meeting_id)
    if not addrs:
        log.info("SUMMARY_NO_RECIPIENTS recording=%s", recording_id)
        return

    summary_html = _markdown_to_minimal_html(summary_md)
    body_html = (
        f"<p>Here's a summary of the meeting <strong>{_escape(meeting_title)}</strong>:</p>"
        f"<div style=\"font-family:system-ui,sans-serif;line-height:1.5;\">{summary_html}</div>"
        f"<p style=\"color:#888;font-size:12px;margin-top:24px;\">"
        f"Generated automatically from the meeting transcript. "
        f"The full transcript is attached as a text file."
        f"</p>"
    )
    body_text = summary_md + "\n\n---\nFull transcript attached."

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
                subject=f"Summary: {meeting_title}",
                html=body_html,
                text=body_text,
                attachments=[transcript_attachment],
            )
        except Exception as e:  # noqa: BLE001
            log.error("SUMMARY_EMAIL_FAIL recording=%s to=%s err=%s", recording_id, addr, e)


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _markdown_to_minimal_html(md: str) -> str:
    """Cheap Markdown → HTML for the summary body. Just headings, bullets,
    and paragraphs — that's all the prompt produces. Avoids pulling a full
    markdown lib into the API container."""
    out: list[str] = []
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if line.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{_escape(line[3:].strip())}</h3>")
        elif line.lstrip().startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = line.lstrip()[2:]
            out.append(f"<li>{_escape(item)}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{_escape(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)

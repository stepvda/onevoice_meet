"""Meet → One Voice Mail webhooks (docs/MAIL_INTEGRATION.md §Webhook).

Events, for meetings created by Mail (`meetings.external_source = "mail"`):
  - `meetpp.status` — a Meet++ session reached running, review, published or
    aborted;
  - `meetpp.next_agenda` — the chair sent the draft agenda of the next sitting.

Body `{event, meeting_id, session_id, occurrence_ref, status, at}`, signed as
Mail verifies it (apps/services/src/meet/webhook.ts in Mail):
`X-Meet-Timestamp: <unix seconds>` and
`X-Meet-Signature: v1=<hex HMAC-SHA256(secret, timestamp + "." + raw body)>`.
Mail refuses a timestamp more than 300 s away, so every attempt is re-signed.

Delivery runs on a daemon thread — three attempts (now, +30 s, +5 min) — and
never blocks or fails the request that caused it; a lost event is logged and
only delays Mail, which also polls.

Every session status change is caught in one place: an ORM listener on
`MeetppSession.status` notes the change on the DB session, and the deliveries
start once that transaction commits (a rolled-back change sends nothing).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time

import httpx
from sqlalchemy import event
from sqlalchemy.orm import object_session

from app.config import settings
from app.db import SessionLocal
from app.meetpp import util
from app.meetpp.models import MeetppSession
from app.models import Meeting

log = logging.getLogger(__name__)

STATUS_EVENT = "meetpp.status"
NEXT_AGENDA_EVENT = "meetpp.next_agenda"
# Session statuses Mail is told about (finalising is internal to Meet).
WEBHOOK_STATUSES = ("running", "review", "published", "aborted")
RETRY_DELAYS_SECONDS = (0, 30, 300)
TIMEOUT_SECONDS = 10.0
_PENDING_KEY = "mail_webhook_pending"


def enabled() -> bool:
    return bool(settings.mail_webhook_url and settings.mail_webhook_secret)


def sign(secret: str, timestamp: int, raw_body: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}.{raw_body}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v1={digest}"


def encode(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_event(name: str, *, meeting_id: str, session_id: str, occurrence_ref: str | None, status: str, at: str | None = None) -> dict:
    return {
        "event": name,
        "meeting_id": meeting_id,
        "session_id": session_id,
        "occurrence_ref": occurrence_ref,
        "status": status,
        "at": at or util.iso(util.now()),
    }


def _post(url: str, raw_body: str, headers: dict) -> int:
    """One HTTP attempt; returns the status code (tests replace this)."""
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        resp = client.post(url, content=raw_body.encode("utf-8"), headers=headers)
    return resp.status_code


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def deliver(payload: dict) -> bool:
    """Send one event, retrying on failure. Blocking: run it off the request."""
    raw = encode(payload)
    what = f"{payload.get('event')} for session {payload.get('session_id')}"
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        if delay:
            _sleep(delay)
        if not enabled():
            return False
        ts = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "X-Meet-Timestamp": str(ts),
            "X-Meet-Signature": sign(settings.mail_webhook_secret, ts, raw),
        }
        try:
            status = _post(settings.mail_webhook_url, raw, headers)
        except Exception as exc:  # noqa: BLE001 — network errors are retried
            log.warning("mail webhook %s: attempt %d failed: %s", what, attempt, exc)
            continue
        if 200 <= status < 300:
            log.info("mail webhook %s delivered (attempt %d)", what, attempt)
            return True
        log.warning("mail webhook %s: attempt %d answered HTTP %s", what, attempt, status)
    log.error("mail webhook %s not delivered after %d attempts", what, len(RETRY_DELAYS_SECONDS))
    return False


def _start(fn, *args) -> None:
    """Run `fn(*args)` in the background (tests make this synchronous)."""
    threading.Thread(target=fn, args=args, name="mail-webhook", daemon=True).start()


def _for_mail_meeting(meeting_id: str) -> bool:
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, meeting_id)
        return meeting is not None and meeting.external_source == "mail"
    finally:
        db.close()


def _deliver_session_event(name: str, meeting_id: str, session_id: str, occurrence_ref: str | None, status: str, at: str) -> bool:
    if not _for_mail_meeting(meeting_id):
        return False
    return deliver(
        build_event(name, meeting_id=meeting_id, session_id=session_id, occurrence_ref=occurrence_ref, status=status, at=at)
    )


def send_session_event(name: str, session: MeetppSession) -> None:
    """Queue an event about `session` (no-op when the webhook is not
    configured or the meeting was not created by Mail)."""
    if not enabled():
        return
    _start(
        _deliver_session_event, name, session.meeting_id, session.id, session.external_ref, session.status,
        util.iso(util.now()),
    )


# ─── Status changes (one hook for every place that sets a status) ──────────


@event.listens_for(MeetppSession.status, "set", active_history=True)
def _status_set(target: MeetppSession, value, oldvalue, _initiator) -> None:
    if value == oldvalue or value not in WEBHOOK_STATUSES or not enabled():
        return
    db = object_session(target)
    if db is None:
        return
    db.info.setdefault(_PENDING_KEY, []).append(
        (STATUS_EVENT, target.meeting_id, target.id, target.external_ref, value, util.iso(util.now()))
    )


@event.listens_for(SessionLocal, "after_commit")
def _after_commit(db) -> None:
    for item in db.info.pop(_PENDING_KEY, None) or []:
        _start(_deliver_session_event, *item)


@event.listens_for(SessionLocal, "after_rollback")
def _after_rollback(db) -> None:
    db.info.pop(_PENDING_KEY, None)

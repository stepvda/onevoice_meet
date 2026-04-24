"""
LiveKit webhook receiver.

LiveKit POSTs events here with a signed JWT in the Authorization header. We
verify the signature using the LiveKit WebhookReceiver, then update our DB.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from livekit import api
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Meeting, MeetingParticipant, Recording

router = APIRouter(prefix="/v1")

_receiver = api.WebhookReceiver(
    api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/webhooks/livekit")
async def livekit_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    body = (await request.body()).decode("utf-8")
    auth_header = request.headers.get("Authorization", "")
    try:
        event = _receiver.receive(body, auth_header)
    except Exception as exc:  # noqa: BLE001 — livekit-api raises generic errors
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid webhook: {exc}") from exc

    etype = event.event

    if etype == "participant_joined" and event.participant and event.room:
        m = db.query(Meeting).filter_by(room_name=event.room.name).first()
        if m:
            existing = (
                db.query(MeetingParticipant)
                .filter_by(meeting_id=m.id, livekit_identity=event.participant.identity, left_at=None)
                .first()
            )
            if existing is None:
                db.add(
                    MeetingParticipant(
                        meeting_id=m.id,
                        livekit_identity=event.participant.identity,
                        display_name=event.participant.name or event.participant.identity,
                        is_authenticated=not event.participant.identity.startswith("anon-"),
                        is_owner=False,
                    )
                )
                db.commit()

    elif etype == "participant_left" and event.participant and event.room:
        m = db.query(Meeting).filter_by(room_name=event.room.name).first()
        if m:
            row = (
                db.query(MeetingParticipant)
                .filter_by(meeting_id=m.id, livekit_identity=event.participant.identity, left_at=None)
                .order_by(MeetingParticipant.id.desc())
                .first()
            )
            if row:
                row.left_at = _now()
                db.commit()

    elif etype == "room_finished" and event.room:
        m = db.query(Meeting).filter_by(room_name=event.room.name).first()
        if m and m.is_active:
            m.is_active = False
            m.closed_at = _now()
            db.commit()

    elif etype in ("egress_started", "egress_updated", "egress_ended") and event.egress_info:
        rec = db.query(Recording).filter_by(egress_id=event.egress_info.egress_id).first()
        if rec:
            if etype == "egress_ended":
                rec.status = "completed" if event.egress_info.status == 3 else "failed"  # EGRESS_COMPLETE=3 in proto
                rec.ended_at = _now()
                rec.expires_at = _now() + timedelta(days=settings.recording_retention_days)
                # Populate size/duration from egress_info.file if present.
                if event.egress_info.file:
                    rec.file_size_bytes = event.egress_info.file.size or None
                if event.egress_info.duration:
                    rec.duration_seconds = int(event.egress_info.duration / 1_000_000_000)  # ns → s
            db.commit()

    return {"ok": True, "event": etype}

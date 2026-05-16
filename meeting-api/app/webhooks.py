"""
LiveKit webhook receiver.

LiveKit POSTs events here with a signed JWT in the Authorization header. We
verify the signature using the LiveKit WebhookReceiver, then update our DB.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from livekit import api
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import LivestreamDestinationState, Meeting, MeetingParticipant, Recording
from app.routes.ti_cafe import (
    clear_room as ticafe_clear_room,
    is_ti_cafe_room,
    mark_joined as ticafe_mark_joined,
    mark_left as ticafe_mark_left,
)
from app.livekit_client import livekit_api

router = APIRouter(prefix="/v1")

_receiver = api.WebhookReceiver(
    api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _update_destination_states(db, info, etype: str) -> None:
    """Walk the egress_info.stream_results entries and write a status row
    per (meeting_id, platform_id). Idempotent — runs on every
    egress_started / egress_updated / egress_ended event so the latest
    snapshot always wins.

    Per LiveKit `StreamInfo.Status` proto (values may differ across SDK
    versions; we match by name string to be forward-compatible):
      ACTIVE     → "streaming"
      FINISHED   → "complete"
      FAILED     → "failed"
      anything else → "idle"
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from app.services.egress_mgr import enabled_url_by_platform
    from datetime import datetime, timezone

    stream_results = list(getattr(info, "stream_results", []) or [])
    if not stream_results:
        return

    m = db.query(Meeting).filter_by(livestream_egress_id=info.egress_id).first()
    if m is None:
        # Recording-only egress or an egress we don't track for streaming —
        # nothing to record. (Recording state lives on the Recording row.)
        return

    url_to_platform = {v: k for k, v in enabled_url_by_platform(m).items()}

    for sr in stream_results:
        url = getattr(sr, "url", None) or ""
        platform = url_to_platform.get(url)
        if not platform:
            # URL doesn't match any currently-enabled destination — could
            # be a destination that was removed mid-stream via the
            # update-stream call, or a key rotation. Either way no state
            # row to maintain for it.
            continue

        raw_status = getattr(sr, "status", None)
        status_name = (
            type(raw_status).Name(raw_status) if hasattr(type(raw_status), "Name") else str(raw_status)
        ).upper() if raw_status is not None else ""
        if "ACTIVE" in status_name:
            status = "streaming"
        elif "FINISHED" in status_name or "COMPLETE" in status_name:
            status = "complete"
        elif "FAILED" in status_name:
            status = "failed"
        else:
            status = "idle"
        # If the whole egress ended, anything still listed as streaming
        # transitions to complete so the UI doesn't show a green dot
        # against a dead egress.
        if etype == "egress_ended" and status == "streaming":
            status = "complete"

        err = (getattr(sr, "error", "") or "").strip()[:500] or None

        stmt = sqlite_insert(LivestreamDestinationState).values(
            meeting_id=m.id,
            platform_id=platform,
            status=status,
            error=err,
            updated_at=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[LivestreamDestinationState.meeting_id, LivestreamDestinationState.platform_id],
            set_={
                "status": stmt.excluded.status,
                "error": stmt.excluded.error,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        try:
            db.execute(stmt)
        except Exception:
            # Per-destination state is a UI nicety — never fail the
            # whole webhook because of a write hiccup here.
            db.rollback()
            continue
    db.commit()


async def _clear_recording_metadata(room_name: str) -> None:
    """Flip the `recording_active` / `streaming_active` flags back to False
    on the LiveKit room metadata when an egress ends outside our normal
    stop-endpoint flow (natural end, egress crash, max-duration cutoff).
    The SPA listens to `RoomMetadataChanged` to drive the in-meeting
    indicator pills — without this update they would stay stuck "on"
    until the user refreshes."""
    import json
    lk = livekit_api()
    try:
        rooms = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
        if not rooms.rooms:
            return
        try:
            current = json.loads(rooms.rooms[0].metadata or "{}")
        except ValueError:
            current = {}
        if not current.get("recording_active") and not current.get("streaming_active"):
            return
        current["recording_active"] = False
        current["streaming_active"] = False
        await lk.room.update_room_metadata(
            api.UpdateRoomMetadataRequest(room=room_name, metadata=json.dumps(current))
        )
    finally:
        await lk.aclose()


@router.post("/webhooks/livekit")
async def livekit_webhook(
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    body = (await request.body()).decode("utf-8")
    auth_header = request.headers.get("Authorization", "")
    try:
        event = _receiver.receive(body, auth_header)
    except Exception as exc:  # noqa: BLE001 — livekit-api raises generic errors
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid webhook: {exc}") from exc

    etype = event.event

    if etype == "participant_joined" and event.participant and event.room:
        if is_ti_cafe_room(event.room.name):
            ticafe_mark_joined(event.participant.identity)
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
                # LiveKit can deliver `participant_joined` more than once for
                # the same session (network blip → retry). The partial-unique
                # index `ux_meeting_participants_active` enforces "one
                # active row per (meeting, identity)"; we catch and ignore
                # the conflict so the retry is a no-op.
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()

    elif etype == "participant_left" and event.participant and event.room:
        if is_ti_cafe_room(event.room.name):
            ticafe_mark_left(event.participant.identity)
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
        if is_ti_cafe_room(event.room.name):
            ticafe_clear_room(event.room.name)
        m = db.query(Meeting).filter_by(room_name=event.room.name).first()
        if m and m.is_active:
            m.is_active = False
            m.closed_at = _now()
            db.commit()

    elif etype in ("egress_started", "egress_updated", "egress_ended") and event.egress_info:
        info = event.egress_info
        # Per-destination stream health: every egress event carries an
        # updated `stream_results[]` with one entry per RTMP URL. We map
        # each URL back to the platform_id that owns it (X / YouTube /
        # Facebook / etc.) and upsert a state row so the frontend can
        # render coloured status dots and surface the egress's own
        # error string when a destination is rejecting the publish.
        _update_destination_states(db, info, etype)
        # Livestreams use a separate egress (no Recording row) but still
        # need cleanup on end — clear the meeting's active egress_id so the
        # toolbar button reverts to "Start streaming" on next page load.
        if etype == "egress_ended":
            m = db.query(Meeting).filter_by(livestream_egress_id=info.egress_id).first()
            if m:
                m.livestream_egress_id = None
                m.current_egress_layout = None
                db.commit()
        rec = db.query(Recording).filter_by(egress_id=info.egress_id).first()
        if rec:
            if etype == "egress_ended":
                # Reset the room-metadata recording flag so the SPA toolbar
                # updates immediately. Run in the background so the webhook
                # doesn't block on a LiveKit roundtrip.
                meeting = db.query(Meeting).filter_by(id=rec.meeting_id).first()
                if meeting:
                    background.add_task(_clear_recording_metadata, meeting.room_name)
                # EgressStatus: 3 = EGRESS_COMPLETE; anything else means failure.
                rec.status = "completed" if info.status == 3 else "failed"
                rec.ended_at = _now()
                rec.expires_at = _now() + timedelta(days=settings.recording_retention_days)

                # Size: newer LiveKit puts this in `file_results[0]`, older in `file`.
                size = None
                fr = list(getattr(info, "file_results", []) or [])
                if fr:
                    size = getattr(fr[0], "size", None) or None
                if not size:
                    f = getattr(info, "file", None)
                    if f is not None:
                        size = getattr(f, "size", None) or None
                if size:
                    rec.file_size_bytes = size

                # Duration in seconds. The proto doesn't have a top-level
                # `duration` field; compute from started_at / ended_at (both
                # in nanoseconds since epoch).
                started_ns = getattr(info, "started_at", 0) or 0
                ended_ns = getattr(info, "ended_at", 0) or 0
                if started_ns and ended_ns and ended_ns >= started_ns:
                    rec.duration_seconds = int((ended_ns - started_ns) / 1_000_000_000)
                else:
                    # Fall back to file_results[0].duration if available.
                    if fr:
                        d_ns = getattr(fr[0], "duration", 0) or 0
                        if d_ns:
                            rec.duration_seconds = int(d_ns / 1_000_000_000)

                # If egress reported an error, surface it.
                if info.status != 3:
                    err = getattr(info, "error", "")
                    if err:
                        rec.youtube_error = (rec.youtube_error or "") + f"egress: {err[:300]}"
                # Kick off the whisper.cpp transcript in the background once
                # the MP4 has finalised on disk. Marked `pending` so the UI
                # can show "transcribing…" while the job runs.
                if rec.status == "completed" and settings.whisper_url:
                    rec.transcript_status = "pending"
                    from app.services.transcription import transcribe_recording
                    background.add_task(transcribe_recording, rec.id)
            db.commit()

    elif etype in ("ingress_started", "ingress_updated", "ingress_ended") and event.ingress_info:
        # Video-playback ingress lifecycle. We only care about the "ended"
        # event — when the current playlist item finishes (or fails), pick
        # the next one (with loop support) or end playback. The whole
        # transition runs in a background task so the webhook ack is fast.
        info = event.ingress_info
        if etype == "ingress_ended":
            from app.services.playback_mgr import advance_after_ingress_ended

            async def _advance(ingress_id: str) -> None:
                from app.db import SessionLocal
                with SessionLocal() as session:
                    try:
                        await advance_after_ingress_ended(ingress_id, session)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception(
                            "playback: advance_after_ingress_ended failed"
                        )

            background.add_task(_advance, info.ingress_id)

    return {"ok": True, "event": etype}

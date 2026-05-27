"""
LiveKit webhook receiver.

LiveKit POSTs events here with a signed JWT in the Authorization header. We
verify the signature using the LiveKit WebhookReceiver, then update our DB.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from livekit import api
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

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
from app.services.playback_mgr import PLAYBACK_IDENTITY

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

    Match strategy is **URL prefix**, NOT full URL equality: LiveKit
    egress *redacts* the stream-key portion of `stream_results[].url`
    (security feature so keys don't leak in webhook payloads — comes
    out as `rtmps://host/path/{abc...xyz}`). The configured per-meeting
    URL (`livestream_<platform>_rtmps_url`) IS a prefix of what egress
    reports, so prefix matching is the reliable signal.

    LiveKit `StreamInfo.Status` int values:
      1 ACTIVE   → "streaming"
      2 FAILED   → "failed"
      3 FINISHED → "complete"
      else       → "idle"
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from app.services.egress_mgr import LIVESTREAM_DESTINATIONS
    from datetime import datetime, timezone

    stream_results = list(getattr(info, "stream_results", []) or [])
    # Diagnostic: log every event so we can see why the per-destination
    # dot stays grey when streaming is actually live. If
    # `stream_results` is empty on egress_started, it's normal — the
    # streams haven't been opened yet. If it stays empty on
    # egress_updated / egress_ended, the egress build isn't reporting
    # stream health at all and we need a different signal.
    log.info(
        "egress event %s eg=%s status=%s stream_results=%d sample=%s",
        etype,
        getattr(info, "egress_id", "?"),
        getattr(info, "status", "?"),
        len(stream_results),
        [
            {
                "url": (getattr(sr, "url", "") or "")[:60],
                "status": getattr(sr, "status", None),
                "error": (getattr(sr, "error", "") or "")[:80],
            }
            for sr in stream_results[:3]
        ],
    )
    if not stream_results:
        return

    m = db.query(Meeting).filter_by(livestream_egress_id=info.egress_id).first()
    if m is None:
        # Recording-only egress or an egress we don't track for streaming —
        # nothing to record. (Recording state lives on the Recording row.)
        log.info("egress %s: not a livestream egress (no Meeting match)", getattr(info, "egress_id", "?"))
        return

    # Build a list of (configured_url_prefix, platform_id) from the
    # meeting's currently-enabled destinations. Longest prefix wins so
    # platforms with similar hosts don't collide.
    prefixes: list[tuple[str, str]] = []
    for platform, en_attr, url_attr, _key_attr in LIVESTREAM_DESTINATIONS:
        if not bool(getattr(m, en_attr, False)):
            continue
        url = getattr(m, url_attr, None)
        if url:
            prefixes.append((url.rstrip("/"), platform))
    prefixes.sort(key=lambda p: len(p[0]), reverse=True)

    def match_platform(reported_url: str) -> str | None:
        for prefix, platform in prefixes:
            if reported_url.startswith(prefix):
                return platform
        return None

    wrote_any = False
    for sr in stream_results:
        url = getattr(sr, "url", None) or ""
        platform = match_platform(url)
        if not platform:
            continue

        raw_status = getattr(sr, "status", None)
        # LiveKit StreamInfo.Status enum (verified against the installed
        # `livekit.protocol.egress.StreamInfo.Status`):
        #   ACTIVE = 0, FINISHED = 1, FAILED = 2.
        # An earlier revision of this code assumed 1=ACTIVE/2=FAILED/3=FINISHED,
        # which silently mapped every successful push to "idle" (the
        # default branch) — so the modal's green dot never lit up for
        # platforms that *worked*, only the red dot for Facebook (which
        # happened to line up on 2=FAILED by coincidence).
        try:
            status_int = int(raw_status) if raw_status is not None else -1
        except (TypeError, ValueError):
            status_int = -1
        if status_int == 0:
            status = "streaming"
        elif status_int == 1:
            status = "complete"
        elif status_int == 2:
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
            wrote_any = True
        except Exception:
            # Per-destination state is a UI nicety — never fail the
            # whole webhook because of a write hiccup here.
            db.rollback()
            continue
    if wrote_any:
        db.commit()


async def _mute_track_for_playback(room_name: str, identity: str, track_sid: str) -> None:
    """Server-mute a freshly-published track when video playback is active.

    Without this, a real user who joins AFTER playback started will publish
    their mic / camera and the LiveKit `single-speaker` egress template
    will swap the recording / livestream composite away from the playback
    participant to whoever just joined. Mirrors the bulk mute applied at
    playback start (`_mute_all_other_participants`) for late joiners.
    Best-effort: a failure here just risks a brief layout flicker.
    """
    lk = livekit_api()
    try:
        await lk.room.mute_published_track(
            api.MuteRoomTrackRequest(
                room=room_name, identity=identity, track_sid=track_sid, muted=True
            )
        )
    except Exception:
        log.exception(
            "playback: failed to mute new track %s on %s in %s",
            track_sid,
            identity,
            room_name,
        )
    finally:
        await lk.aclose()


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

    elif etype == "track_published" and event.participant and event.room and event.track:
        # If video playback is active in this meeting, immediately
        # server-mute the freshly-published track so the playback
        # participant remains the sole speaker. Skip the playback
        # ingress itself and any non-standard participants (other
        # ingresses, egress workers, agents) so we never accidentally
        # silence the playback feed or some future workload.
        m = db.query(Meeting).filter_by(room_name=event.room.name).first()
        if (
            m
            and m.playback_ingress_id
            and event.participant.identity != PLAYBACK_IDENTITY
            and getattr(event.participant, "kind", None) == api.ParticipantInfo.Kind.STANDARD
        ):
            background.add_task(
                _mute_track_for_playback,
                event.room.name,
                event.participant.identity,
                event.track.sid,
            )

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
        log.info(
            "ingress event %s id=%s state=%s",
            etype,
            getattr(info, "ingress_id", "?"),
            getattr(getattr(info, "state", None), "status", None),
        )
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

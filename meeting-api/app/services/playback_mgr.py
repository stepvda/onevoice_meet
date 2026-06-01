"""In-meeting video playback orchestration.

The host uploads MP4s into a per-meeting playlist (`PlaybackItem` rows).
When the host clicks Play, this module:
  1. Picks the first item.
  2. Calls LiveKit Ingress (URL_INPUT) with a signed local HTTP URL so
     the ingress container streams the file out as a "playback" participant
     track every viewer sees.
  3. Server-side mutes every other participant's mic + camera.
  4. Sends a `playback-start` data-channel signal so the SPA hides the
     grid and pins the playback participant for everyone.

When the current ingress ends (the LiveKit `ingress_ended` webhook calls
`advance_to_next`), we pick the next item by `position`; if there is none,
we end playback. If `Meeting.playback_loop` is True we wrap back to the
first item instead.

Stopping is the reverse: delete the ingress, broadcast `playback-end`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from livekit import api
from sqlalchemy.orm import Session

from app.config import settings
from app.livekit_client import livekit_api
from app.models import Meeting, ModerationAudit, PlaybackItem

log = logging.getLogger(__name__)

# The participant identity / name the ingress publishes as. Reserved — we
# refuse to issue tokens with this identity to real users (see tokens.py
# guard if added) and the SPA can rely on this string to find the track.
PLAYBACK_IDENTITY = "playback"


def _vp8_video_options() -> "api.IngressVideoOptions":
    """Output spec for URL_INPUT ingresses — VP8 instead of LiveKit's
    H.264 default.

    Why: Firefox on Linux can't decode H.264 unless OpenH264 (or
    system gstreamer/ffmpeg) is correctly installed. Many distro
    builds — Firefox Snap and several Flatpak channels in particular —
    ship without working H.264 support, so a viewer on a public
    `/public/<slug>` page would see a frozen/black video while the
    Opus audio kept playing. VP8 is universally supported in every
    browser without plugins, at the cost of slightly heavier encode
    on the ingress container (negligible at 720p30 / 1.5 Mbps).

    720p30 / 1.5 Mbps matches the egress encoding profile in
    egress_mgr._encoding_options() so the recorded MP4, the
    livestream RTMP push, and what the live viewers see all look the
    same.
    """
    return api.IngressVideoOptions(
        source=api.TrackSource.CAMERA,
        options=api.IngressVideoEncodingOptions(
            video_codec=api.VideoCodec.VP8,
            frame_rate=30,
            layers=[
                api.VideoLayer(
                    quality=api.VideoQuality.HIGH,
                    width=1280,
                    height=720,
                    bitrate=1_500_000,
                ),
            ],
        ),
    )

# Internal URL the ingress container fetches the file from. Same docker
# network — caddy isn't involved. The ?token=… is an HMAC-signed expiry
# so the URL isn't a free-floating credential.
_INGRESS_INTERNAL_BASE = "http://meeting-api:8080"

# How long the signed download URL stays valid. Plenty of time for ingress
# to fetch the file plus some buffer for retries.
_PLAYBACK_URL_TTL_SECONDS = 30 * 60


def _sign_playback_url(item_id: str) -> str:
    """Build a token that only the ingress container needs to verify back —
    we sign with the existing JWT secret so we don't need a new env var.
    Format: `<expiry_unix>.<hex32>`."""
    exp = int(time.time()) + _PLAYBACK_URL_TTL_SECONDS
    payload = f"playback:{item_id}:{exp}".encode()
    mac = hmac.new(settings.jwt_secret_key.encode(), payload, hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{mac}"


def verify_playback_url(item_id: str, token: str) -> bool:
    """Used by the file-serving route to validate ?token=…"""
    try:
        exp_str, mac = token.split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    payload = f"playback:{item_id}:{exp}".encode()
    expected = hmac.new(settings.jwt_secret_key.encode(), payload, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(mac, expected)


def _build_signed_url(item: PlaybackItem, t_seconds: float = 0.0) -> str:
    """Build the URL ingress fetches for this item. When `t_seconds`
    > 0 the URL includes a `&t=<seconds>` parameter; the internal
    file-fetch endpoint then pipes the file from that timestamp via
    ffmpeg (MPEG-TS stream-copy). Used by drag-to-seek."""
    token = _sign_playback_url(item.id)
    base = f"{_INGRESS_INTERNAL_BASE}/api/v1/internal/playback/{item.id}?token={token}"
    if t_seconds and t_seconds > 0:
        base += f"&t={t_seconds:.3f}"
    return base


def _build_signed_freeze_url(item: PlaybackItem, t_seconds: float) -> str:
    """Build the URL ingress fetches for a "paused" item — an endless
    MPEG-TS stream holding the single frame at offset T. Reuses the
    same signed token as the regular playback URL; the `&freeze=1` flag
    on the internal endpoint flips it from the seek path to the
    single-frame-loop path."""
    token = _sign_playback_url(item.id)
    return (
        f"{_INGRESS_INTERNAL_BASE}/api/v1/internal/playback/{item.id}"
        f"?token={token}&t={max(0.0, float(t_seconds)):.3f}&freeze=1"
    )


async def _mute_all_other_participants(lk: "api.LiveKitAPI", room_name: str) -> None:
    """Server-side mute the published mic + camera tracks of every
    participant in the room EXCEPT the playback identity. We use the
    LiveKit MuteRoomTrackRequest which forces the SFU to stop forwarding
    the track; the SPA also receives the data-channel signal and toggles
    its own button state so the UI stays consistent."""
    try:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
    except Exception:
        log.exception("playback: failed to list participants for mute-all")
        return
    for p in res.participants:
        if p.identity == PLAYBACK_IDENTITY:
            continue
        # Only mute real human participants. Egress / Ingress / SIP / agent
        # workers also show up here and don't have user-controlled mics —
        # touching them risks suppressing the wrong track.
        if p.kind != api.ParticipantInfo.Kind.STANDARD:
            continue
        for t in p.tracks:
            if t.muted:
                continue
            try:
                await lk.room.mute_published_track(
                    api.MuteRoomTrackRequest(
                        room=room_name, identity=p.identity, track_sid=t.sid, muted=True
                    )
                )
            except Exception:
                # Best-effort — if one participant's mute fails (e.g. they
                # left between list_participants and mute) we keep going.
                log.exception("playback: mute_published_track failed for %s", p.identity)


async def _broadcast(room_name: str, signal: str, payload: dict) -> None:
    """Send a small JSON packet on the LiveKit data channel so the SPA can
    react to playback-start / playback-end without re-polling. The packet
    rides the room's reliable transport so it survives brief disconnects."""
    body = {"v": 1, "type": signal, **payload}
    lk = livekit_api()
    try:
        await lk.room.send_data(
            api.SendDataRequest(
                room=room_name,
                data=json.dumps(body).encode("utf-8"),
                kind=api.DataPacket.Kind.RELIABLE,
                topic="meet-playback",
            )
        )
    except Exception:
        log.exception("playback: broadcast %s failed", signal)
    finally:
        await lk.aclose()


PLAYBACK_LAYOUT = "single-speaker"


async def _force_single_speaker_layout(m: Meeting, db: Session) -> None:
    """If an egress is running (recording or livestream), restart it
    with `single-speaker` so the playback participant — the only
    unmuted speaker after `_mute_all_other_participants` — owns the
    full composite frame. The host's prior layout is saved on the
    meeting so `_restore_layout_after_playback` can switch back when
    playback ends. No-op when no egress is active; in that case the
    `start_recording` / `start_stream` paths force single-speaker on
    their next call (see playback-guard in those handlers)."""
    # Defer import to dodge the egress_mgr → playback_mgr circular import.
    from app.services.egress_mgr import _current_state, reconcile_egress

    cur_egress_id, cur_has_file, cur_has_stream = _current_state(m, db)
    if not cur_egress_id:
        # Remember the user's most-recently-picked layout (if any) so
        # an egress started later — during playback — can restore the
        # right post-playback layout. Falls back to "speaker" later.
        if m.layout_before_playback is None and m.current_egress_layout:
            m.layout_before_playback = m.current_egress_layout
            db.commit()
        return
    if m.current_egress_layout == PLAYBACK_LAYOUT:
        return  # already on the right template; nothing to do
    if m.layout_before_playback is None:
        m.layout_before_playback = m.current_egress_layout or "speaker"
        db.commit()
    await reconcile_egress(
        m,
        want_file=cur_has_file,
        want_stream=cur_has_stream,
        layout=PLAYBACK_LAYOUT,
        user_sub="playback",
        db=db,
    )


async def _restore_layout_after_playback(m: Meeting, db: Session) -> None:
    """Inverse of `_force_single_speaker_layout`. Called from the two
    code paths that end playback (`stop_playback`, natural-end branch in
    `advance_after_ingress_ended`). Restarts the active egress (if any)
    with the host's pre-playback layout and clears the snapshot."""
    from app.services.egress_mgr import _current_state, reconcile_egress

    saved = m.layout_before_playback
    m.layout_before_playback = None
    db.commit()
    if not saved:
        return
    cur_egress_id, cur_has_file, cur_has_stream = _current_state(m, db)
    if not cur_egress_id:
        return  # no egress to switch; just clearing the snapshot is enough
    if m.current_egress_layout == saved:
        return  # already on the right template
    await reconcile_egress(
        m,
        want_file=cur_has_file,
        want_stream=cur_has_stream,
        layout=saved,  # type: ignore[arg-type]
        user_sub="playback",
        db=db,
    )


async def _start_ingress_for_item(
    m: Meeting,
    item: PlaybackItem,
    db: Session,
    *,
    initial: bool,
    from_seconds: float = 0.0,
    skip_slide: bool = False,
) -> str:
    """Create a fresh LiveKit ingress for `item`, store its id on the
    meeting, and (only on the very first item of a play session) mute all
    other participants, force the egress layout to single-speaker, and
    broadcast the playback-start signal. Returns the new ingress_id.

    `from_seconds` > 0 starts the source mid-file via ffmpeg-seek (used
    by `seek_playback` for drag-on-slider). The `playback_started_at`
    stamp is adjusted to `now - from_seconds` so the SPA's elapsed
    calculation lands at the seek position without an extra field.

    When the meeting's `playback_whats_up_next` toggle is on, the item's
    duration > 5 min, and we're not seeking mid-file, this function
    instead points the ingress at a ~35-s rundown slide. The real item
    is remembered in `playback_pending_item_id` and gets started by
    `advance_after_ingress_ended` once the slide ends. Set
    `skip_slide=True` from inside that detour to break the recursion."""
    # Aliases (playlist links) inherit duration from their source row, but
    # historical aliases born while the source's duration was still NULL
    # never got it propagated — read through to the source so those rows
    # still pass the slide eligibility check.
    effective_duration: Optional[float] = item.duration_seconds
    if effective_duration is None and item.source_item_id:
        src_row = db.query(PlaybackItem).filter_by(id=item.source_item_id).first()
        if src_row is not None:
            effective_duration = src_row.duration_seconds

    slide_url: Optional[str] = None
    if (
        not skip_slide
        and from_seconds == 0
        and m.playback_whats_up_next
        and (effective_duration or 0) > 300
    ):
        from app.services.whats_next_slide import (
            build_slide_data,
            render_slide,
            build_signed_slide_url,
            slide_key_from_path,
        )

        data = build_slide_data(db, m.id, real_item_id=item.id)
        if data is not None:
            try:
                slide_path = await render_slide(data)
                slide_url = build_signed_slide_url(slide_key_from_path(slide_path))
            except Exception:
                log.exception(
                    "whats_next: render failed for meeting %s — falling back to direct play",
                    m.id,
                )
                slide_url = None

    url = slide_url if slide_url else _build_signed_url(item, t_seconds=from_seconds)
    ingress_name = (
        f"whatsnext-{item.id}" if slide_url else f"playback-{item.id}"
    )
    participant_name = (
        "What's up next…" if slide_url else item.filename
    )

    if initial:
        # Switch the egress layout BEFORE creating the ingress so the
        # new (single-speaker) egress is already running by the time the
        # playback track lands — avoids a brief flash of the old layout
        # on the recording / livestream.
        await _force_single_speaker_layout(m, db)
    lk = livekit_api()
    try:
        ingress = await lk.ingress.create_ingress(
            api.CreateIngressRequest(
                input_type=api.IngressInput.URL_INPUT,
                name=ingress_name,
                room_name=m.room_name,
                participant_identity=PLAYBACK_IDENTITY,
                participant_name=participant_name,
                url=url,
                video=_vp8_video_options(),
            )
        )
        if initial:
            await _mute_all_other_participants(lk, m.room_name)
    finally:
        await lk.aclose()

    from datetime import datetime, timedelta, timezone
    m.playback_ingress_id = ingress.ingress_id
    # Always set `playback_current_item_id` to the real item — the SPA
    # surfaces this in the playlist UI, and we want the "What's up next"
    # bumper to look like part of the same upcoming item, not a phantom.
    m.playback_current_item_id = item.id
    # Stamp the start time so the SPA can compute elapsed playback for
    # the progress bar (`elapsed = now - playback_started_at`). When
    # the egress is starting mid-file via ffmpeg-seek we anchor the
    # stamp `from_seconds` back so the bar's elapsed value matches the
    # actual playhead.
    m.playback_started_at = datetime.now(timezone.utc) - timedelta(seconds=from_seconds)
    if slide_url:
        m.playback_pending_item_id = item.id
    else:
        m.playback_pending_item_id = None
    db.commit()

    await _broadcast(
        m.room_name,
        "playback-start" if initial else "playback-next",
        {
            "item_id": item.id,
            "filename": item.filename,
            "slide": bool(slide_url),
        },
    )
    return ingress.ingress_id


async def start_playback(m: Meeting, user_sub: str, db: Session) -> dict:
    """Public entrypoint called by POST /playback:start."""
    if not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")
    if not m.playback_enabled:
        raise HTTPException(status_code=400, detail="video playback not enabled for this meeting")
    if m.playback_ingress_id:
        raise HTTPException(status_code=409, detail="playback already in progress")
    # Recording + playback may run concurrently on the rescaled host
    # (was mutually exclusive on the original 2-vCPU box). The recording
    # egress composes the playback participant alongside other
    # participants and writes both file_outputs and stream_outputs in
    # one egress slot, same as before — only the host-side gate is gone.
    first = (
        db.query(PlaybackItem)
        .filter_by(meeting_id=m.id)
        .order_by(PlaybackItem.position.asc())
        .first()
    )
    if not first:
        raise HTTPException(status_code=400, detail="playlist is empty")

    ingress_id = await _start_ingress_for_item(m, first, db, initial=True)
    db.add(
        ModerationAudit(
            meeting_id=m.id,
            actor_user_id=user_sub,
            action="playback_start",
            details=f"{first.id}:{ingress_id}",
        )
    )
    db.commit()
    return {"ok": True, "ingress_id": ingress_id, "item_id": first.id}


async def stop_playback(m: Meeting, user_sub: str, db: Session) -> dict:
    """Public entrypoint called by POST /playback:stop."""
    ingress_id = m.playback_ingress_id
    if not ingress_id:
        # Idempotent — see the stop_recording / stop_stream pattern.
        return {"ok": True, "already_stopped": True}

    lk = livekit_api()
    try:
        try:
            await lk.ingress.delete_ingress(api.DeleteIngressRequest(ingress_id=ingress_id))
        except Exception:
            # Even if delete fails (e.g. ingress already ended), clear our
            # state and broadcast end so the UI doesn't get stuck.
            log.exception("playback: delete_ingress failed for %s", ingress_id)
    finally:
        await lk.aclose()

    m.playback_ingress_id = None
    m.playback_current_item_id = None
    m.playback_started_at = None
    m.playback_pending_item_id = None
    m.playback_paused_offset_seconds = None
    db.commit()

    # Flip the egress back to the host's pre-playback layout before
    # broadcasting the end signal — otherwise the SPA would surface
    # the participant grid while the egress is still on single-speaker.
    await _restore_layout_after_playback(m, db)

    await _broadcast(m.room_name, "playback-end", {})
    db.add(
        ModerationAudit(
            meeting_id=m.id,
            actor_user_id=user_sub,
            action="playback_stop",
            details=ingress_id,
        )
    )
    db.commit()
    return {"ok": True}


async def pause_playback(m: Meeting, user_sub: str, db: Session) -> dict:
    """Public entrypoint called by POST /playback:pause.

    Replaces the running ingress with one that publishes an endless
    single-frame loop at the current playback offset, so every viewer
    sees the frame that was on screen the moment pause was clicked. The
    Meeting's `playback_paused_offset_seconds` is set to that offset and
    `playback_started_at` is cleared so the SPA's progress bar stops
    advancing. Idempotent — calling pause while already paused is a
    no-op.

    Implementation notes:
      - The freeze-frame ingress runs under the same `playback` identity
        and `playback_current_item_id` row as the real item, so the
        playlist UI keeps the correct row highlighted.
      - There IS a brief gap (~1-2s) between deleting the old ingress
        and the new freeze-frame ingress publishing — LiveKit Ingress's
        URL_INPUT pipeline needs to negotiate with GStreamer first. We
        can't avoid that without a custom ingest path, which we're not
        going to build.
      - We do NOT restore the egress layout while paused (the playback
        composition stays single-speaker)."""
    if not m.playback_ingress_id:
        raise HTTPException(status_code=409, detail="nothing is playing")
    if m.playback_paused_offset_seconds is not None:
        # Idempotent — already paused.
        return {"ok": True, "already_paused": True}
    if not m.playback_current_item_id:
        raise HTTPException(status_code=409, detail="no current item")
    item = db.query(PlaybackItem).filter_by(id=m.playback_current_item_id).first()
    if not item:
        raise HTTPException(status_code=409, detail="current item missing")

    # Compute the offset to freeze at — same formula the SPA uses for
    # its progress bar. `playback_started_at` may be NULL while a
    # "What's up next" slide is in flight; refuse to pause then (we'd
    # freeze the slide, not the real content).
    if not m.playback_started_at:
        raise HTTPException(status_code=409, detail="can't pause during the rundown slide")
    started = m.playback_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    offset = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())

    # Tear down the running (real-content) ingress.
    old_ingress_id = m.playback_ingress_id
    lk = livekit_api()
    try:
        try:
            await lk.ingress.delete_ingress(api.DeleteIngressRequest(ingress_id=old_ingress_id))
        except Exception:
            log.exception("playback: delete_ingress failed for %s on pause", old_ingress_id)
    finally:
        await lk.aclose()

    # Resolve aliases to the file-owning row — same as the regular play path.
    src_item = item
    if item.source_item_id:
        resolved = db.query(PlaybackItem).filter_by(id=item.source_item_id).first()
        if resolved is not None:
            src_item = resolved

    freeze_url = _build_signed_freeze_url(src_item, offset)
    lk = livekit_api()
    try:
        ingress = await lk.ingress.create_ingress(
            api.CreateIngressRequest(
                input_type=api.IngressInput.URL_INPUT,
                name=f"playback-paused-{item.id}",
                room_name=m.room_name,
                participant_identity=PLAYBACK_IDENTITY,
                participant_name=item.filename,
                url=freeze_url,
                video=_vp8_video_options(),
            )
        )
    finally:
        await lk.aclose()

    m.playback_ingress_id = ingress.ingress_id
    m.playback_paused_offset_seconds = offset
    # NULL'ing started_at freezes the SPA's elapsed-time calc — the bar
    # holds at `offset` (surfaced via the state endpoint).
    m.playback_started_at = None
    db.add(
        ModerationAudit(
            meeting_id=m.id,
            actor_user_id=user_sub,
            action="playback_pause",
            details=f"{item.id}:{offset:.3f}",
        )
    )
    db.commit()
    await _broadcast(m.room_name, "playback-paused", {"offset_seconds": offset})
    return {"ok": True, "ingress_id": ingress.ingress_id, "offset_seconds": offset}


async def resume_playback(m: Meeting, user_sub: str, db: Session) -> dict:
    """Public entrypoint called by POST /playback:resume. Inverse of
    `pause_playback`: tear down the freeze-frame ingress and restart
    the real ingress at the saved offset. Idempotent — calling resume
    when not paused is a 409."""
    if m.playback_paused_offset_seconds is None:
        raise HTTPException(status_code=409, detail="playback is not paused")
    if not m.playback_current_item_id:
        raise HTTPException(status_code=409, detail="no current item")
    item = db.query(PlaybackItem).filter_by(id=m.playback_current_item_id).first()
    if not item:
        raise HTTPException(status_code=409, detail="current item missing")

    offset = float(m.playback_paused_offset_seconds)

    # Tear down the freeze-frame ingress before starting the real one,
    # otherwise the new ingress competes for the same `playback`
    # identity and LiveKit rejects with already-published.
    if m.playback_ingress_id:
        old_ingress_id = m.playback_ingress_id
        lk = livekit_api()
        try:
            try:
                await lk.ingress.delete_ingress(api.DeleteIngressRequest(ingress_id=old_ingress_id))
            except Exception:
                log.exception("playback: delete_ingress failed for %s on resume", old_ingress_id)
        finally:
            await lk.aclose()
        m.playback_ingress_id = None
    # Clear paused state BEFORE starting the new ingress so a webhook
    # that fires mid-restart doesn't think we're still paused.
    m.playback_paused_offset_seconds = None
    db.commit()

    ingress_id = await _start_ingress_for_item(
        m, item, db, initial=False, from_seconds=offset, skip_slide=True,
    )
    db.add(
        ModerationAudit(
            meeting_id=m.id,
            actor_user_id=user_sub,
            action="playback_resume",
            details=f"{item.id}:{offset:.3f}",
        )
    )
    db.commit()
    await _broadcast(m.room_name, "playback-resumed", {"offset_seconds": offset})
    return {"ok": True, "ingress_id": ingress_id, "offset_seconds": offset}


async def advance_after_ingress_ended(
    ingress_id: str,
    db: Session,
) -> Optional[str]:
    """Webhook hook: the ingress just ended on its own. Look up the meeting
    that owns this ingress, pick the next playlist item (or loop back),
    start a fresh ingress for it. Return the new ingress_id or None if we
    actually ended playback.

    Special case: if the ingress that just ended was a "What's up next"
    slide, start the real item it was announcing (stored in
    `playback_pending_item_id`) — DON'T insert another slide before it.
    Pre-gen for the NEXT eligible slide kicks off in the background."""
    m = db.query(Meeting).filter_by(playback_ingress_id=ingress_id).first()
    # Don't auto-advance while paused. If the freeze-frame ingress
    # naturally ends (its long-but-finite ffmpeg loop wound down because
    # the user has been paused for hours), restart the same freeze ingress
    # at the saved offset so the room stays on the frozen frame.
    if m is not None and m.playback_paused_offset_seconds is not None:
        offset = float(m.playback_paused_offset_seconds)
        m.playback_ingress_id = None
        db.commit()
        cur_item = (
            db.query(PlaybackItem).filter_by(id=m.playback_current_item_id).first()
            if m.playback_current_item_id else None
        )
        if cur_item is None:
            # No item to freeze on — fall back to ending playback.
            m.playback_paused_offset_seconds = None
            m.playback_current_item_id = None
            db.commit()
            return None
        src_item = cur_item
        if cur_item.source_item_id:
            resolved = db.query(PlaybackItem).filter_by(id=cur_item.source_item_id).first()
            if resolved is not None:
                src_item = resolved
        freeze_url = _build_signed_freeze_url(src_item, offset)
        lk = livekit_api()
        try:
            ingress = await lk.ingress.create_ingress(
                api.CreateIngressRequest(
                    input_type=api.IngressInput.URL_INPUT,
                    name=f"playback-paused-{cur_item.id}",
                    room_name=m.room_name,
                    participant_identity=PLAYBACK_IDENTITY,
                    participant_name=cur_item.filename,
                    url=freeze_url,
                    video=_vp8_video_options(),
                )
            )
        finally:
            await lk.aclose()
        m.playback_ingress_id = ingress.ingress_id
        db.commit()
        return ingress.ingress_id

    if m is not None and m.playback_pending_item_id:
        pending_id = m.playback_pending_item_id
        m.playback_pending_item_id = None
        # Don't commit yet — the next call commits its own state.
        pending = db.query(PlaybackItem).filter_by(id=pending_id).first()
        if pending is not None:
            try:
                new_ingress = await _start_ingress_for_item(
                    m, pending, db, initial=False, skip_slide=True,
                )
            except Exception:
                log.exception(
                    "whats_next: failed to start real item %s after slide",
                    pending_id,
                )
            else:
                from app.services.whats_next_slide import schedule_pre_generation
                schedule_pre_generation(m.id)
                return new_ingress
        # Pending item gone (host removed it during the slide?) — fall
        # through to the standard next-by-position logic below.
    if not m:
        return None  # not a playback ingress, or already cleaned up

    cur = (
        db.query(PlaybackItem).filter_by(id=m.playback_current_item_id).first()
        if m.playback_current_item_id
        else None
    )

    next_item: Optional[PlaybackItem] = None
    if cur:
        next_item = (
            db.query(PlaybackItem)
            .filter(PlaybackItem.meeting_id == m.id, PlaybackItem.position > cur.position)
            .order_by(PlaybackItem.position.asc())
            .first()
        )
    if not next_item and m.playback_loop:
        next_item = (
            db.query(PlaybackItem)
            .filter_by(meeting_id=m.id)
            .order_by(PlaybackItem.position.asc())
            .first()
        )

    if not next_item:
        # No next item and not looping — end the playback cleanly.
        m.playback_ingress_id = None
        m.playback_current_item_id = None
        m.playback_started_at = None
        m.playback_paused_offset_seconds = None
        db.commit()
        await _restore_layout_after_playback(m, db)
        await _broadcast(m.room_name, "playback-end", {})
        return None

    new_ingress = await _start_ingress_for_item(m, next_item, db, initial=False)
    # Warm the next slide while the current item plays — so when this
    # item ends, the slide is already encoded and ready.
    try:
        from app.services.whats_next_slide import schedule_pre_generation
        schedule_pre_generation(m.id)
    except Exception:
        log.exception("whats_next: schedule_pre_generation failed")
    return new_ingress


# LiveKit IngressState.status values: 0=INACTIVE, 1=BUFFERING, 2=PUBLISHING,
# 3=ERROR, 4=COMPLETE. Anything outside the live (1,2) set means the
# handler has stopped publishing media.
_INGRESS_HEALTHY_STATUSES = {1, 2}
# Don't flag a freshly-started ingress as stale — it takes a few seconds
# to negotiate with the source and transition into BUFFERING/PUBLISHING.
_WATCHDOG_GRACE_SECONDS = 25
# Wall-clock margin beyond the item's known duration before treating the
# ingress as stale on duration alone. LiveKit's list_ingress was observed
# returning healthy status (ENDPOINT_BUFFERING/PUBLISHING) for tens of
# minutes after the ingress actually finished — the wall-clock check is
# the belt-and-suspenders fallback for that case.
_WATCHDOG_DURATION_OVERRUN_SECONDS = 60


async def watchdog_check_stale_ingresses(db: Session) -> int:
    """Safety net for ingress handlers that die without firing the
    `ingress_ended` webhook — e.g. GStreamer asserts (we observed
    SIGTRAP / core dump on a video file with declared 1366x720 vs
    coded 1368x720). When that happens the playlist would otherwise
    stall on the dead ingress indefinitely.

    For every meeting with `playback_ingress_id` set, ask LiveKit
    whether the ingress is still publishing. If it's gone, errored,
    or completed, run the same advance path the webhook would have
    run, so the next item starts. Returns the number of meetings we
    recovered."""
    candidates = (
        db.query(Meeting)
        .filter(Meeting.playback_ingress_id.is_not(None))
        .all()
    )
    if not candidates:
        return 0

    lk = livekit_api()
    recovered = 0
    try:
        for m in candidates:
            age: Optional[float] = None
            if m.playback_started_at:
                started = m.playback_started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - started).total_seconds()
                if age < _WATCHDOG_GRACE_SECONDS:
                    continue

                # Wall-clock duration-overrun fallback. When LiveKit's
                # ingress server holds onto a dead ingress in a healthy
                # state (we observed this on a clean EOS where the
                # `ingress_ended` webhook was lost, leaving the playlist
                # stalled for ~52 min), the list_ingress check below
                # never flags it. The item's own `duration_seconds`
                # tells us when wall-clock has clearly outrun reality.
                if m.playback_current_item_id:
                    cur_item = (
                        db.query(PlaybackItem)
                        .filter_by(id=m.playback_current_item_id)
                        .first()
                    )
                    if cur_item is not None:
                        eff_duration: Optional[float] = cur_item.duration_seconds
                        # Alias items have no duration of their own;
                        # borrow from the source row.
                        if eff_duration is None and cur_item.source_item_id:
                            src = (
                                db.query(PlaybackItem)
                                .filter_by(id=cur_item.source_item_id)
                                .first()
                            )
                            if src is not None:
                                eff_duration = src.duration_seconds
                        if (
                            eff_duration is not None
                            and age > eff_duration + _WATCHDOG_DURATION_OVERRUN_SECONDS
                        ):
                            log.warning(
                                "playback watchdog: meeting %s ingress %s overran"
                                " expected duration (age=%.0fs duration=%.0fs)"
                                " — advancing playlist",
                                m.id, m.playback_ingress_id, age, eff_duration,
                            )
                            try:
                                await advance_after_ingress_ended(
                                    m.playback_ingress_id, db
                                )
                                recovered += 1
                            except Exception:
                                log.exception(
                                    "playback watchdog: duration-overrun"
                                    " advance failed for %s",
                                    m.playback_ingress_id,
                                )
                            continue

            ingress_id = m.playback_ingress_id
            stale_reason: Optional[str] = None
            try:
                resp = await lk.ingress.list_ingress(
                    api.ListIngressRequest(ingress_id=ingress_id)
                )
                items = list(getattr(resp, "items", []) or [])
                if not items:
                    stale_reason = "not_found"
                else:
                    state = getattr(items[0], "state", None)
                    status = getattr(state, "status", None)
                    if status not in _INGRESS_HEALTHY_STATUSES:
                        stale_reason = f"status={status}"
            except api.TwirpError as e:
                if getattr(e, "code", None) == "not_found":
                    stale_reason = "not_found"
                else:
                    log.warning(
                        "playback watchdog: list_ingress refused for %s: %s",
                        ingress_id, e,
                    )
                    continue
            except Exception:
                log.exception(
                    "playback watchdog: list_ingress failed for %s",
                    ingress_id,
                )
                continue

            if not stale_reason:
                continue

            log.warning(
                "playback watchdog: ingress %s for meeting %s is stale (%s)"
                " — advancing playlist",
                ingress_id, m.id, stale_reason,
            )
            try:
                await advance_after_ingress_ended(ingress_id, db)
                recovered += 1
            except Exception:
                log.exception(
                    "playback watchdog: advance failed for %s",
                    ingress_id,
                )
    finally:
        await lk.aclose()

    return recovered


async def play_specific_item(
    m: Meeting,
    item: PlaybackItem,
    user_sub: str,
    db: Session,
) -> dict:
    """Click-to-play: switch the currently-playing item (or start
    playback if nothing's playing) to the chosen one. Implementation
    mirrors `advance_after_ingress_ended` — stop the running ingress
    (if any), then start a fresh one for `item`. The "initial" flag
    is True only when nothing was playing — that's the case where we
    need the global mute-all + playback-start broadcast."""
    if not m.is_active:
        raise HTTPException(status_code=403, detail="meeting closed")
    if not m.playback_enabled:
        raise HTTPException(status_code=400, detail="video playback not enabled for this meeting")
    # Recording + playback can run concurrently — see start_playback.

    was_playing = bool(m.playback_ingress_id)
    if was_playing:
        # Stop the current ingress before starting the new one. We do
        # this directly (rather than via stop_playback) because we
        # don't want to broadcast "playback-end" — the SPA would close
        # its spotlight; we want a seamless item-switch instead.
        old_ingress_id = m.playback_ingress_id
        lk = livekit_api()
        try:
            try:
                await lk.ingress.delete_ingress(api.DeleteIngressRequest(ingress_id=old_ingress_id))
            except Exception:
                log.exception("playback: delete_ingress failed for %s on item switch", old_ingress_id)
        finally:
            await lk.aclose()
        m.playback_ingress_id = None
        m.playback_current_item_id = None
        m.playback_started_at = None
        m.playback_pending_item_id = None
        m.playback_paused_offset_seconds = None
        db.commit()

    ingress_id = await _start_ingress_for_item(m, item, db, initial=not was_playing)
    db.add(
        ModerationAudit(
            meeting_id=m.id,
            actor_user_id=user_sub,
            action="playback_play_item",
            details=f"{item.id}:{ingress_id}",
        )
    )
    db.commit()
    return {"ok": True, "ingress_id": ingress_id, "item_id": item.id}


async def seek_playback(
    m: Meeting,
    position_seconds: float,
    user_sub: str,
    db: Session,
) -> dict:
    """Drag-to-seek: stop the running ingress and restart the SAME
    item at `position_seconds`. The internal file-fetch endpoint
    receives `?t=<seconds>` and pipes ffmpeg-seek output (MPEG-TS,
    stream-copy → cheap). No-op if no playback is running."""
    if not m.playback_ingress_id or not m.playback_current_item_id:
        raise HTTPException(status_code=409, detail="no playback in progress")
    cur = db.query(PlaybackItem).filter_by(id=m.playback_current_item_id).first()
    if not cur:
        raise HTTPException(status_code=404, detail="current item not found")
    position_seconds = max(0.0, float(position_seconds))

    # Stop current ingress directly (no playback-end broadcast — we
    # want a seamless seek, not a session end).
    old_ingress_id = m.playback_ingress_id
    lk = livekit_api()
    try:
        try:
            await lk.ingress.delete_ingress(api.DeleteIngressRequest(ingress_id=old_ingress_id))
        except Exception:
            log.exception("playback: delete_ingress failed for %s on seek", old_ingress_id)
    finally:
        await lk.aclose()
    m.playback_ingress_id = None
    m.playback_started_at = None
    db.commit()

    ingress_id = await _start_ingress_for_item(
        m, cur, db, initial=False, from_seconds=position_seconds, skip_slide=True,
    )
    db.add(
        ModerationAudit(
            meeting_id=m.id,
            actor_user_id=user_sub,
            action="playback_seek",
            details=f"{cur.id}:{position_seconds:.3f}:{ingress_id}",
        )
    )
    db.commit()
    return {"ok": True, "ingress_id": ingress_id, "position_seconds": position_seconds}

"""Shared egress reconciler for recording + livestreaming.

LiveKit's egress dispatcher treats each room-composite job as a worker slot
and refuses a second one on a 2-vCPU host (we hit Twirp 503 in production
when starting a recording while a stream was active). The fix is to never
run more than one egress per meeting: instead, a single `RoomCompositeEgress`
holds whichever combination of file + stream outputs the host has currently
toggled on.

LiveKit doesn't support hot-adding new *output types* to a running egress
(only adding/removing stream URLs on an already-streaming egress). So when
the user toggles the second feature on, we stop the current egress and start
a fresh one with the combined outputs. Cost: a ~2 s gap in the stream and a
split in the recording file. Benefit: only one worker slot is ever consumed,
and the second toggle stops hanging for 22 s and returning 500.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import HTTPException
from livekit import api
from sqlalchemy.orm import Session
from ulid import ULID

from app.config import settings
from app.livekit_client import livekit_api
from app.models import Meeting, ModerationAudit, Recording

if TYPE_CHECKING:
    from app.routes.recordings import RecordingLayout


# Every egress (recording + livestream) renders through our custom Web
# template, so PiP toggle / overlay-identity changes propagate live via
# room metadata without restarting the egress. The page reads its
# composition state from `RoomEvent.RoomMetadataChanged` (see
# EgressLayoutPiP.tsx) — `?overlay=` is an initial-state hint only.
_EGRESS_TEMPLATE_PATH = "/egress-layout/pip"


def _egress_custom_base_url(m: Meeting) -> str:
    """URL the egress headless Chrome fetches. LiveKit appends its own
    `url`/`token`/`room`/`layout` query params. We pass `overlay` as a
    bootstrapping hint — the page subsequently re-reads it from room
    metadata so changes during the egress don't require a restart."""
    base = f"{settings.public_url}{_EGRESS_TEMPLATE_PATH}"
    overlay = m.pip_overlay_identity or ""
    return f"{base}?overlay={quote(overlay)}" if overlay else base


def _build_stream_url(rtmps_url: str, stream_key: str) -> str:
    """`<url>/<key>` is what every major RTMP ingest expects (X/Twitter via
    studio.x.com, Substack, Twitch, YouTube Live, Facebook). Tolerate stray
    slashes."""
    return rtmps_url.rstrip("/") + "/" + stream_key.lstrip("/")


# Each entry is (platform_id, enabled_attr, url_attr, key_attr). X.com keeps
# the legacy unprefixed column names from when it was the only destination.
# Adding a new platform = three new columns on Meeting + one entry here + a
# UI block. The `platform_id` is the short stable key used by the
# LivestreamDestinationState rows and surfaced to the frontend so it can
# render the right brand label / icon.
LIVESTREAM_DESTINATIONS: list[tuple[str, str, str, str]] = [
    ("x",        "livestream_enabled",          "livestream_rtmps_url",          "livestream_stream_key"),
    ("substack", "livestream_substack_enabled", "livestream_substack_rtmps_url", "livestream_substack_stream_key"),
    ("youtube",  "livestream_youtube_enabled",  "livestream_youtube_rtmps_url",  "livestream_youtube_stream_key"),
    ("facebook", "livestream_facebook_enabled", "livestream_facebook_rtmps_url", "livestream_facebook_stream_key"),
    ("rumble",   "livestream_rumble_enabled",   "livestream_rumble_rtmps_url",   "livestream_rumble_stream_key"),
]


def _enabled_stream_urls(m: Meeting) -> list[str]:
    """Return every destination URL the meeting wants to stream to. Empty
    list means "no streaming destinations configured" — the caller treats
    this the same as `want_stream=False`."""
    urls: list[str] = []
    for _platform, en_attr, url_attr, key_attr in LIVESTREAM_DESTINATIONS:
        en = bool(getattr(m, en_attr, False))
        url = getattr(m, url_attr, None)
        key = getattr(m, key_attr, None)
        if en and url and key:
            urls.append(_build_stream_url(url, key))
    return urls


def enabled_url_by_platform(m: Meeting) -> dict[str, str]:
    """Same data as `_enabled_stream_urls` but keyed by platform_id so the
    webhook layer can map a `stream_results[].url` straight back to the
    platform that owns it."""
    out: dict[str, str] = {}
    for platform, en_attr, url_attr, key_attr in LIVESTREAM_DESTINATIONS:
        en = bool(getattr(m, en_attr, False))
        url = getattr(m, url_attr, None)
        key = getattr(m, key_attr, None)
        if en and url and key:
            out[platform] = _build_stream_url(url, key)
    return out


def _encoding_options() -> "api.EncodingOptions":
    """Encoding profile shared by recording, livestreaming, and combined egress.

    A 2-second key-frame interval is mandatory for the livestream path:
    Mux (used by Substack), YouTube Live, Facebook, Rumble and most other
    RTMP ingests buffer to the next IDR before decoding anything. With a
    4-second GOP a viewer who joins late waits up to 4 seconds for picture,
    and some ingests (Substack/Mux in particular) drop the stream silently
    — observable symptom: "both audio and video black on the player".
    2 s also bounds recording-side replay seek latency to a comfortable
    range without measurably affecting bitrate efficiency at H.264 Main."""
    if settings.recording_preset_1080p:
        return api.EncodingOptions(
            width=1920, height=1080, framerate=30,
            video_codec=api.VideoCodec.H264_MAIN, video_bitrate=3000,
            audio_codec=api.AudioCodec.AAC, audio_bitrate=128, audio_frequency=48000,
            key_frame_interval=2.0,
        )
    return api.EncodingOptions(
        width=1280, height=720, framerate=30,
        video_codec=api.VideoCodec.H264_MAIN, video_bitrate=1500,
        audio_codec=api.AudioCodec.AAC, audio_bitrate=128, audio_frequency=48000,
        key_frame_interval=2.0,
    )


def _current_state(m: Meeting, db: Session) -> tuple[str | None, bool, bool]:
    """Returns (egress_id, has_file, has_stream) for the meeting's current
    egress, if any. has_file/has_stream describe which outputs the active
    egress was started with."""
    running_rec = (
        db.query(Recording)
        .filter_by(meeting_id=m.id, status="running")
        .first()
    )
    stream_egress = m.livestream_egress_id
    file_egress = running_rec.egress_id if running_rec else None
    egress_id = stream_egress or file_egress
    return egress_id, bool(file_egress), bool(stream_egress)


async def reconcile_egress(
    m: Meeting,
    *,
    want_file: bool,
    want_stream: bool,
    layout: "RecordingLayout | None",
    user_sub: str,
    db: Session,
) -> dict:
    """Bring the meeting's egress state to (want_file, want_stream).

    Returns {"egress_id": str | None, "recording_id": str | None, "no_change": bool}
    where `recording_id` is the NEW Recording row id if one was created.

    Validation is the caller's responsibility — this function trusts that the
    caller has already checked owner permissions, meeting active state, and
    (for `want_stream`) that the meeting has RTMPS credentials configured.
    """
    cur_egress_id, cur_has_file, cur_has_stream = _current_state(m, db)

    # If the caller didn't pass an explicit layout (typical for stop/toggle-off
    # paths), reuse whatever the current egress was started with so a stream
    # that continues across a recording-stop doesn't suddenly switch layouts.
    if layout is None:
        layout = m.current_egress_layout or "speaker"  # type: ignore[assignment]

    # Every egress now goes through our custom React template via
    # `custom_base_url`. The `layout` arg is passed to that page as a
    # default-rendering hint (LiveKit appends it to the URL as
    # `?layout=…`) but PiP state lives in room metadata, not the layout
    # name. This means toggling `pip_enabled` or changing the overlay
    # identity never requires an egress restart — the page just
    # re-renders when room metadata changes.
    effective_layout = layout

    # Idempotent: if the current state already matches what's wanted AND
    # the layout is the same, leave the egress alone. A layout change
    # always requires a restart — LiveKit egress can't swap room
    # composite templates mid-stream.
    layout_changed = bool(cur_egress_id) and m.current_egress_layout != effective_layout
    if cur_has_file == want_file and cur_has_stream == want_stream and not layout_changed:
        return {"egress_id": cur_egress_id, "recording_id": None, "no_change": True}

    # Stop the current egress (if any). The `egress_ended` webhook is the
    # one that finalises the old Recording row's size/duration/status, so we
    # don't touch the Recording table for the stopping side — only clear
    # the Meeting pointer synchronously so subsequent reads see the new
    # state immediately.
    if cur_egress_id:
        lk = livekit_api()
        try:
            await lk.egress.stop_egress(api.StopEgressRequest(egress_id=cur_egress_id))
            # If the transition leaves NO file output running, flip the room
            # metadata's `recording_active` flag so the SPA's toolbar reverts
            # to "Start recording" instead of stranding the user on a Stop
            # button that 404s. The webhook is too late for this — the SPA
            # listens for RoomMetadataChanged immediately.
            # Write the POST-RECONCILE state directly so we don't
            # briefly publish "neither active" between the stop and the
            # subsequent start — the SPA listens to RoomMetadataChanged
            # and would otherwise blink the Recording / Streaming pills
            # off for the few hundred ms the new egress takes to spin
            # up. When the transition is a true stop (no follow-up
            # start), the next branch returns early before we'd write
            # again, so the indicators clear correctly there too.
            try:
                await _set_recording_metadata(
                    lk,
                    m.room_name,
                    recording_active=want_file,
                    streaming_active=want_stream,
                    meeting=m,
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            await lk.aclose()
        if cur_has_stream:
            m.livestream_egress_id = None
        if not want_file and not want_stream:
            m.current_egress_layout = None
        db.commit()

    # If both outputs are off after this transition, we're done.
    if not want_file and not want_stream:
        db.add(ModerationAudit(meeting_id=m.id, actor_user_id=user_sub, action="egress_stop", details=cur_egress_id or ""))
        db.commit()
        return {"egress_id": None, "recording_id": None, "no_change": False}

    # Start a fresh egress with the desired output combination.
    new_filepath: str | None = None
    new_started: datetime | None = None
    file_outputs: list[api.EncodedFileOutput] = []
    stream_outputs: list[api.StreamOutput] = []

    if want_file:
        # Lazily import to dodge the circular module load — enforce_disk_cap
        # lives in routes/recordings which imports this module indirectly.
        from app.routes.recordings import enforce_disk_cap
        enforce_disk_cap()
        Path(settings.recordings_dir).mkdir(parents=True, exist_ok=True)
        new_started = datetime.now(timezone.utc)
        new_filepath = str(
            Path(settings.recordings_dir)
            / f"{m.room_name}-{new_started.strftime('%Y%m%d-%H%M%S')}.mp4"
        )
        file_outputs.append(
            api.EncodedFileOutput(
                file_type=api.EncodedFileType.MP4,
                filepath=new_filepath,
                disable_manifest=True,
            )
        )

    if want_stream:
        urls = _enabled_stream_urls(m)
        if not urls:
            raise HTTPException(
                status_code=400,
                detail="enable at least one livestream destination (X.com or Substack) with rtmps url + key",
            )
        # One StreamOutput with multiple URLs — ffmpeg fans the same encoded
        # output to all destinations, so adding Substack on top of X.com is
        # only one extra muxer (~negligible CPU).
        stream_outputs.append(
            api.StreamOutput(protocol=api.StreamProtocol.RTMP, urls=urls)
        )

    req_kwargs: dict = {
        "room_name": m.room_name,
        "layout": effective_layout,
        "advanced": _encoding_options(),
        # Always route through our custom Web template — see comment on
        # `_egress_custom_base_url`. Egress page reads PiP state from
        # room metadata, so toggling PiP after start needs no restart.
        "custom_base_url": _egress_custom_base_url(m),
    }
    if file_outputs:
        req_kwargs["file_outputs"] = file_outputs
    if stream_outputs:
        req_kwargs["stream_outputs"] = stream_outputs

    lk = livekit_api()
    try:
        egress_info = await lk.egress.start_room_composite_egress(
            api.RoomCompositeEgressRequest(**req_kwargs)
        )
        # Surface the recording / streaming flags on room metadata — the
        # in-meeting Recording and Streaming pills are driven from these,
        # so every participant sees them update the same tick the egress
        # is (re)started.
        try:
            await _set_recording_metadata(
                lk,
                m.room_name,
                recording_active=want_file,
                streaming_active=want_stream,
                meeting=m,
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        await lk.aclose()

    new_egress_id = egress_info.egress_id
    new_recording_id: str | None = None

    if want_file:
        assert new_started is not None and new_filepath is not None
        new_recording_id = str(ULID())
        rec = Recording(
            id=new_recording_id,
            meeting_id=m.id,
            egress_id=new_egress_id,
            file_path=new_filepath,
            started_at=new_started,
            expires_at=new_started + timedelta(days=settings.recording_retention_days),
            status="running",
        )
        db.add(rec)

    if want_stream:
        m.livestream_egress_id = new_egress_id

    m.current_egress_layout = effective_layout

    db.add(
        ModerationAudit(
            meeting_id=m.id,
            actor_user_id=user_sub,
            action=f"egress_reconcile",
            details=f"file={int(want_file)} stream={int(want_stream)} egress={new_egress_id}",
        )
    )
    db.commit()
    return {"egress_id": new_egress_id, "recording_id": new_recording_id, "no_change": False}


async def _set_recording_metadata(
    lk: api.LiveKitAPI,
    room_name: str,
    recording_active: bool,
    streaming_active: bool | None = None,
    meeting: Meeting | None = None,
) -> None:
    """Update the room-metadata flags that drive the in-meeting indicator
    badges. `recording_active` flips the red Recording pill for every
    viewer; `streaming_active` (when not None) flips a similarly-styled
    Streaming pill. Passing None for streaming preserves whatever value
    is already on the room metadata, which lets the recording-only stop
    path avoid clobbering a livestream that's still running.

    When `meeting` is provided, also writes `pip_enabled` and
    `pip_overlay_identity` so a fresh egress lands on the right
    composition without a second LiveKit roundtrip."""
    import json

    rooms = await lk.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
    current: dict = {}
    if rooms.rooms:
        try:
            current = json.loads(rooms.rooms[0].metadata or "{}")
        except ValueError:
            current = {}
    current["recording_active"] = recording_active
    if streaming_active is not None:
        current["streaming_active"] = streaming_active
    if meeting is not None:
        current["pip_enabled"] = bool(meeting.pip_enabled)
        current["pip_overlay_identity"] = meeting.pip_overlay_identity or None
    await lk.room.update_room_metadata(
        api.UpdateRoomMetadataRequest(room=room_name, metadata=json.dumps(current))
    )


async def sync_compositor_session(m: Meeting) -> None:
    """Tell the compositor service to start or stop a Puppeteer session
    for this meeting based on `pip_enabled`. Mints a fresh
    publish-capable token each call so the compositor never has to
    refresh a stale one. Best-effort: a transient compositor outage
    won't fail the API call that triggered it (the host just loses the
    composite track until the next toggle).

    Idempotent on both sides: POSTing /sessions for a room that already
    has a session replaces it; DELETEing one that doesn't exist is a
    no-op."""
    import logging
    from app.livekit_client import mint_composite_token

    log = logging.getLogger(__name__)
    base = settings.compositor_url.rstrip("/")

    try:
        import httpx  # type: ignore
    except ImportError:
        log.warning(
            "sync_compositor_session: httpx not installed — compositor "
            "sessions will not be controlled. Add httpx to requirements.",
        )
        return

    try:
        if m.pip_enabled:
            token = mint_composite_token(room_name=m.room_name)
            payload = {
                "token": token,
                "livekit_url": settings.livekit_ws_url,
                "overlay_identity": m.pip_overlay_identity or "",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{base}/sessions/{m.room_name}", json=payload
                )
                r.raise_for_status()
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.delete(f"{base}/sessions/{m.room_name}")
                r.raise_for_status()
    except Exception:
        log.exception("sync_compositor_session failed for %s", m.id)


async def push_pip_metadata(m: Meeting) -> None:
    """Mirror `pip_enabled` + `pip_overlay_identity` into the LiveKit
    room metadata so every connected client (and the egress headless
    Chrome) gets the change live via `RoomEvent.RoomMetadataChanged`.
    No-op if the room isn't running on the SFU yet — the SPA will read
    the values from the meeting row on join."""
    import json

    lk = livekit_api()
    try:
        rooms = await lk.room.list_rooms(
            api.ListRoomsRequest(names=[m.room_name])
        )
        if not rooms.rooms:
            return
        try:
            current = json.loads(rooms.rooms[0].metadata or "{}")
        except ValueError:
            current = {}
        current["pip_enabled"] = bool(m.pip_enabled)
        current["pip_overlay_identity"] = m.pip_overlay_identity or None
        await lk.room.update_room_metadata(
            api.UpdateRoomMetadataRequest(
                room=m.room_name, metadata=json.dumps(current)
            )
        )
    finally:
        await lk.aclose()

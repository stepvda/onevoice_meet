import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.routes.recordings import cleanup_expired_recordings, enforce_disk_cap

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="UTC")


def _retention_job() -> None:
    count = cleanup_expired_recordings()
    log.info("retention job deleted %d expired recordings", count)


def _disk_cap_job() -> None:
    result = enforce_disk_cap()
    log.info(
        "disk-cap job: deleted=%d freed=%d before=%.2f after=%.2f cap=%.2f",
        result.get("deleted", 0),
        result.get("freed_bytes", 0),
        result.get("before_ratio", 0),
        result.get("after_ratio", 0),
        result.get("cap", 0),
    )
    # Piggyback on the disk-cap job to evict stale "What's up next" slides.
    try:
        from app.services.whats_next_slide import evict_stale_slides
        removed = evict_stale_slides()
        if removed:
            log.info("whats_next: evicted %d stale slide(s)", removed)
    except Exception:
        log.exception("whats_next: evict_stale_slides failed")


def _youtube_supervisor_job() -> None:
    """Tick the YouTube Live supervisor: provision broadcasts, rotate at
    the 12h cap, transition `ready→live` once bytes arrive, poll viewer
    counts. See app/services/youtube_live.py:supervise_all for details."""
    from app.db import SessionLocal
    from app.services.youtube_live import supervise_all

    async def _run() -> int:
        with SessionLocal() as db:
            return await supervise_all(db)

    try:
        changes = asyncio.run(_run())
    except Exception:
        log.exception("youtube supervisor job failed")
        return
    if changes:
        log.info("youtube supervisor: %d change(s)", changes)


def _playback_watchdog_job() -> None:
    # Recovers playlist playback when a LiveKit ingress handler dies
    # without firing the `ingress_ended` webhook (e.g. GStreamer SIGTRAP
    # on a malformed video stream). Without this, the playlist stalls
    # silently on the dead ingress for hours.
    from app.db import SessionLocal
    from app.services.playback_mgr import watchdog_check_stale_ingresses

    async def _run() -> int:
        with SessionLocal() as db:
            return await watchdog_check_stale_ingresses(db)

    try:
        recovered = asyncio.run(_run())
    except Exception:
        log.exception("playback watchdog job failed")
        return
    if recovered:
        log.info("playback watchdog: recovered %d stalled ingress(es)", recovered)


def _hls_segment_prune_job() -> None:
    """Delete TI-TV HLS `.ts` segments older than `hls_retention_seconds`.

    LiveKit's sliding-window *live* playlist references only the newest few
    segments but never deletes the old ones, so without this the HLS dir grows
    unbounded and fills the disk. Retention (default 180s) is far larger than
    the live window (a handful of 6s segments), so in-use segments are never
    touched. Playlists (`.m3u8`) are left alone — tiny and rewritten by egress.

    Safety guard: if the live playlist's mtime is older than
    `hls_watchdog_stale_seconds`, we skip pruning entirely. Deleting segments
    while the egress pipeline is stalled would leave a playlist that references
    non-existent files, turning the HLS stream from "stale" into "404 broken".
    The watchdog restarts the egress first; pruning resumes once the watchdog
    revives the pipeline and segments are flowing again.
    """
    import os
    import time

    from app.config import settings

    if not settings.hls_enabled:
        return
    hls_root = os.path.join(settings.recordings_dir, "hls")
    if not os.path.isdir(hls_root):
        return

    # Check if the live playlist is fresh before pruning. A stalled egress
    # means the playlist still references old segments — deleting them would
    # break the stream completely rather than just leaving it stale.
    live_m3u8 = os.path.join(hls_root, settings.titv_public_slug, "live.m3u8")
    if os.path.isfile(live_m3u8):
        playlist_age = time.time() - os.path.getmtime(live_m3u8)
        if playlist_age > settings.hls_watchdog_stale_seconds:
            return

    cutoff = time.time() - settings.hls_retention_seconds
    removed = 0
    for dirpath, _dirs, files in os.walk(hls_root):
        for name in files:
            if not name.endswith(".ts"):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    if removed:
        log.info("hls prune: deleted %d stale segment(s)", removed)


def _hls_egress_watchdog_job() -> None:
    """Detect stalled HLS egresses and restart them.

    When the LiveKit egress pipeline deadlocks (e.g. all RTMP destinations
    disconnect simultaneously and the internal Goroutine coordination
    freezes), it stops writing HLS segments but stays registered as
    ``egress_active`` — the meeting-api never receives an ``egress_ended``
    webhook, so the stream appears "running" forever while viewers see a
    frozen/broken playlist.

    This watchdog monitors the mtime of each active HLS meeting's live
    playlist. If a playlist hasn't been touched in
    ``hls_watchdog_stale_seconds`` and the meeting still thinks it's
    streaming, we call ``reconcile_egress`` to stop the dead egress and
    start a fresh one — the same thing a human operator would do by
    toggling Start/Stop twice in the dashboard.
    """
    import asyncio
    import os
    import time

    from app.config import settings
    from app.db import SessionLocal
    from app.models import Meeting
    from app.services.egress_mgr import reconcile_egress

    if not settings.hls_enabled:
        return

    async def _run() -> int:
        with SessionLocal() as db:
            stalled = db.query(Meeting).filter(
                Meeting.public_slug == settings.titv_public_slug,
                Meeting.livestream_egress_id.isnot(None),
                Meeting.livestream_enabled.is_(True),
            ).all()

            restarted = 0
            now = time.time()
            for m in stalled:
                live_m3u8 = os.path.join(
                    settings.recordings_dir, "hls", m.public_slug, "live.m3u8"
                )
                if not os.path.isfile(live_m3u8):
                    continue
                age = now - os.path.getmtime(live_m3u8)
                if age <= settings.hls_watchdog_stale_seconds:
                    continue
                log.warning(
                    "hls watchdog: egress %s stale for %.0fs (live.m3u8 age), restarting",
                    m.livestream_egress_id,
                    age,
                )
                try:
                    await reconcile_egress(
                        m,
                        want_file=False,
                        want_stream=True,
                        layout=None,
                        user_sub="hls_watchdog",
                        db=db,
                    )
                    restarted += 1
                    log.info(
                        "hls watchdog: restarted egress for %s, new egress_id=%s",
                        m.room_name,
                        m.livestream_egress_id,
                    )
                except Exception:
                    log.exception(
                        "hls watchdog: failed to restart egress for %s",
                        m.room_name,
                    )
            return restarted

    restarted = asyncio.run(_run())
    if restarted:
        log.info("hls watchdog: restarted %d stalled egress(es)", restarted)


def start() -> None:
    if scheduler.running:
        return
    # 03:00 UTC — delete recordings past their 30-day expiry.
    scheduler.add_job(
        _retention_job,
        CronTrigger(hour=3, minute=0),
        id="retention_cleanup",
        replace_existing=True,
    )
    # Every hour — evict oldest recordings if disk usage exceeds the cap (75%).
    scheduler.add_job(
        _disk_cap_job,
        CronTrigger(minute=15),
        id="disk_cap_enforcement",
        replace_existing=True,
    )
    # Every 30s — recover playlist playback if the current LiveKit
    # ingress has died without firing its `ingress_ended` webhook.
    scheduler.add_job(
        _playback_watchdog_job,
        IntervalTrigger(seconds=30),
        id="playback_watchdog",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # YouTube Live supervisor — runs at the same cadence as the playback
    # watchdog. Provisioning is no-op when no meetings are in API mode,
    # so the cost when unused is one cheap SELECT per minute.
    scheduler.add_job(
        _youtube_supervisor_job,
        IntervalTrigger(seconds=30),
        id="youtube_supervisor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Every 60s — prune stale TI-TV HLS `.ts` segments so the live stream's
    # local storage can't grow unbounded. No-op when HLS is disabled or idle.
    scheduler.add_job(
        _hls_segment_prune_job,
        IntervalTrigger(seconds=60),
        id="hls_segment_prune",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Every 15s — detect stalled HLS egresses (live.m3u8 not being updated)
    # and auto-restart them. Faster than the prune job so the watchdog has a
    # chance to revive the pipeline before pruning deletes referenced segments.
    scheduler.add_job(
        _hls_egress_watchdog_job,
        IntervalTrigger(seconds=15),
        id="hls_egress_watchdog",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)

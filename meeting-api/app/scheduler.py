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
    """
    import os
    import time

    from app.config import settings

    if not settings.hls_enabled:
        return
    hls_root = os.path.join(settings.recordings_dir, "hls")
    if not os.path.isdir(hls_root):
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
    scheduler.start()


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)

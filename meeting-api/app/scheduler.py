import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

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
    scheduler.start()


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)

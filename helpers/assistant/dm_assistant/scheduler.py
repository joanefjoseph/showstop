from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import pipeline
from .config import settings

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def start() -> None:
    global _scheduler
    tz = {"timezone": settings.timezone} if settings.timezone else {}
    _scheduler = BackgroundScheduler(**tz)
    for hhmm in settings.run_times:
        h, m = (int(x) for x in hhmm.split(":"))
        _scheduler.add_job(
            pipeline.run_scan,
            CronTrigger(hour=h, minute=m, jitter=120, **tz),  # +/-2 min so it's not robotic
            kwargs={"trigger": "scheduled"},
            id=f"scan-{h:02d}{m:02d}",
            misfire_grace_time=3600,  # laptop asleep at 9:00? still runs if it wakes before 10:00
            coalesce=True,
            max_instances=1,
        )
    _scheduler.start()
    log.info("Scheduled scans at %s", ", ".join(settings.run_times))


def next_run_times() -> list[datetime]:
    if not _scheduler:
        return []
    return sorted(j.next_run_time for j in _scheduler.get_jobs() if j.next_run_time)
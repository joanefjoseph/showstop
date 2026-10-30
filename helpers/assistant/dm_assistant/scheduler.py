from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import pipeline
from .config import settings

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None

WAKEUP_EVERY_S = 60        # re-check the clock at least this often
MISFIRE_GRACE_S = 4 * 3600 # a run missed while the laptop slept still happens on wake


def _watchdog() -> None:
    """APScheduler 3 sleeps in one long Event.wait() that does not advance while the
    machine is suspended. After a resume it would keep waiting for hours. Poking it
    every minute forces it to re-read the wall clock and fire (or misfire) jobs."""
    while True:
        time.sleep(WAKEUP_EVERY_S)
        s = _scheduler
        if s is not None and s.running:
            try:
                s.wakeup()
            except Exception:
                log.exception("Scheduler wakeup failed")


def start() -> None:
    global _scheduler
    tz = {"timezone": settings.timezone} if settings.timezone else {}
    _scheduler = BackgroundScheduler(**tz)
    for hhmm in settings.run_times:
        h, m = (int(x) for x in hhmm.split(":"))
        _scheduler.add_job(
            pipeline.run_scan,
            CronTrigger(hour=h, minute=m, jitter=120, **tz),
            kwargs={"trigger": "scheduled"},
            id=f"scan-{h:02d}{m:02d}",
            misfire_grace_time=MISFIRE_GRACE_S,
            coalesce=True,
            max_instances=1,
        )
    _scheduler.start()
    threading.Thread(target=_watchdog, name="scheduler-watchdog", daemon=True).start()
    log.info("Scheduled scans at %s", ", ".join(settings.run_times))


def next_run_times() -> list[datetime]:
    if not _scheduler:
        return []
    return sorted(j.next_run_time for j in _scheduler.get_jobs() if j.next_run_time)
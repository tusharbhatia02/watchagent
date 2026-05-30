import os
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.poller import poll_all_cities

logger = logging.getLogger(__name__)

POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poll_all_cities,
        trigger="interval",
        minutes=POLL_INTERVAL_MINUTES,
        id="poll_all_cities",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — polling every %d minute(s)", POLL_INTERVAL_MINUTES)
    return scheduler

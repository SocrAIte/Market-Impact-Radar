from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_app_config
from app.pipeline.ingest import run_ingest_once
from app.utils.logging import logger


def create_scheduler() -> AsyncIOScheduler:
    app_config = get_app_config()
    scheduler = AsyncIOScheduler(timezone="UTC")
    if app_config.scheduler.enabled:
        scheduler.add_job(
            run_ingest_once,
            trigger="interval",
            minutes=app_config.scheduler.interval_minutes,
            id="market-impact-radar-ingest",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("Scheduler configured: every {} minutes", app_config.scheduler.interval_minutes)
    return scheduler

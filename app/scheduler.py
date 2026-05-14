from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_app_config
from app.pipeline.ingest import run_ingest_once
from app.utils.logging import logger


def create_scheduler() -> AsyncIOScheduler:
    app_config = get_app_config()
    schedule = app_config.scheduler
    scheduler = AsyncIOScheduler(timezone=schedule.timezone)
    if app_config.scheduler.enabled:
        job_kwargs = {
            "id": "market-impact-radar-ingest",
            "replace_existing": True,
            "coalesce": True,
            "max_instances": 1,
        }
        if schedule.mode == "noon":
            scheduler.add_job(
                run_ingest_once,
                trigger="cron",
                hour=schedule.noon_hour,
                minute=schedule.noon_minute,
                **job_kwargs,
            )
            logger.info("Scheduler configured: daily at {:02d}:{:02d} {}", schedule.noon_hour, schedule.noon_minute, schedule.timezone)
        elif schedule.mode == "pre_open":
            scheduler.add_job(
                run_ingest_once,
                trigger="cron",
                hour=schedule.pre_open_hour,
                minute=schedule.pre_open_minute,
                **job_kwargs,
            )
            logger.info(
                "Scheduler configured: pre-open at {:02d}:{:02d} {}",
                schedule.pre_open_hour,
                schedule.pre_open_minute,
                schedule.timezone,
            )
        else:
            scheduler.add_job(
                run_ingest_once,
                trigger="interval",
                minutes=schedule.interval_minutes,
                **job_kwargs,
            )
            logger.info("Scheduler configured: every {} minutes", schedule.interval_minutes)
    return scheduler

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
        if schedule.mode == "market_daily":
            _add_daily_job(scheduler, "market-impact-radar-pre-open", schedule.pre_open_hour, schedule.pre_open_minute)
            _add_daily_job(scheduler, "market-impact-radar-noon", schedule.noon_hour, schedule.noon_minute)
            logger.info("Scheduler configured: daily at {:02d}:{:02d} {}", schedule.noon_hour, schedule.noon_minute, schedule.timezone)
            logger.info(
                "Scheduler configured: pre-open at {:02d}:{:02d} {}",
                schedule.pre_open_hour,
                schedule.pre_open_minute,
                schedule.timezone,
            )
        elif schedule.mode == "custom_time":
            _add_daily_job(scheduler, "market-impact-radar-custom-time", schedule.custom_hour, schedule.custom_minute)
            logger.info(
                "Scheduler configured: custom daily time at {:02d}:{:02d} {}",
                schedule.custom_hour,
                schedule.custom_minute,
                schedule.timezone,
            )
        elif schedule.mode == "noon":
            _add_daily_job(scheduler, "market-impact-radar-noon", schedule.noon_hour, schedule.noon_minute)
            logger.info("Scheduler configured: daily at {:02d}:{:02d} {}", schedule.noon_hour, schedule.noon_minute, schedule.timezone)
        elif schedule.mode == "pre_open":
            _add_daily_job(scheduler, "market-impact-radar-pre-open", schedule.pre_open_hour, schedule.pre_open_minute)
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
                id="market-impact-radar-interval",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            logger.info("Scheduler configured: every {} minutes", schedule.interval_minutes)
    return scheduler


def _add_daily_job(scheduler: AsyncIOScheduler, job_id: str, hour: int, minute: int) -> None:
    scheduler.add_job(
        run_ingest_once,
        trigger="cron",
        hour=hour,
        minute=minute,
        id=job_id,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

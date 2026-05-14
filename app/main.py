from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import init_db
from app.scheduler import create_scheduler
from app.utils.logging import configure_logging, logger
from app.web.routes import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_db()
    scheduler = create_scheduler()
    app.state.scheduler = scheduler
    if scheduler.get_jobs():
        scheduler.start()
        logger.info("Market Impact Radar scheduler started")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Market Impact Radar scheduler stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Market Impact Radar",
        description="全球股市影响新闻雷达：按市场影响评分排序的新闻事件监测与研究辅助工具。",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(web_router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "app": settings.app_name, "environment": settings.environment}

    return app


app = create_app()

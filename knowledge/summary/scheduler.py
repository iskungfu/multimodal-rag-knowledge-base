"""APScheduler 3.x 仅由 FastAPI lifespan 启停，导入模块无后台副作用。"""
import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from knowledge.market.models import SHANGHAI

logger = logging.getLogger(__name__)


def build_scheduler(service_factory, hour=15, minute=30):
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or (hour, minute) < (15, 15):
        raise ValueError("摘要调度时间须在 Asia/Shanghai 15:15 之后")
    scheduler = BackgroundScheduler(timezone=SHANGHAI)

    def generate():
        try:
            service_factory().generate()
        except Exception:
            logger.warning("每日摘要任务未完成，可通过页面检查状态并手动重试")

    scheduler.add_job(generate, trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute,
                      timezone=SHANGHAI), id="daily-summary", max_instances=1, coalesce=True,
                      misfire_grace_time=3600, replace_existing=True)
    return scheduler


def summary_lifespan(enabled=None, service_factory=None):
    @asynccontextmanager
    async def lifespan(app):
        from knowledge.api.summary_api import get_summary_service
        factory = service_factory or get_summary_service
        active = enabled if enabled is not None else os.getenv("DAILY_SUMMARY_ENABLED", "1").lower() in {"1", "true", "yes"}
        scheduler = None
        app.state.summary_scheduler = None
        if active:
            scheduler = build_scheduler(factory, int(os.getenv("DAILY_SUMMARY_HOUR", "15")),
                                        int(os.getenv("DAILY_SUMMARY_MINUTE", "30")))
            scheduler.start()
            app.state.summary_scheduler = scheduler
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
    return lifespan

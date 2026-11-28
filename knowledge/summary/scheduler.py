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
        from knowledge.api.market_collection_api import get_collection_service
        factory = service_factory or get_summary_service
        active = enabled if enabled is not None else os.getenv("DAILY_SUMMARY_ENABLED", "1").lower() in {"1", "true", "yes"}
        scheduler = None
        app.state.summary_scheduler = None
        if active:
            scheduler = build_scheduler(factory, int(os.getenv("DAILY_SUMMARY_HOUR", "15")),
                                        int(os.getenv("DAILY_SUMMARY_MINUTE", "30")))
            scheduler.start()
            app.state.summary_scheduler = scheduler
        collection_scheduler = None
        collection_service = None
        collect = os.getenv("MARKET_COLLECTION_ENABLED", "1" if active else "0").lower() in {"1", "true", "yes"}
        if collect:
            def collect_market():
                nonlocal collection_service
                try:
                    collection_service = get_collection_service()
                    collection_service.start()
                except Exception:
                    logger.warning("全市场收盘采集未启动，可在页面检查并重试")

            collection_scheduler = BackgroundScheduler(timezone=SHANGHAI)
            collection_scheduler.add_job(collect_market, CronTrigger(day_of_week="mon-fri", hour=16, minute=0,
                                         timezone=SHANGHAI), id="market-collection", max_instances=1,
                                         coalesce=True, misfire_grace_time=3600)
            collection_scheduler.start()
        app.state.collection_scheduler = collection_scheduler
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
            if collection_scheduler:
                collection_scheduler.shutdown(wait=False)
            if collection_service:
                collection_service.stop.set()
            elif get_collection_service.cache_info().currsize:
                get_collection_service().stop.set()
    return lifespan

"""APScheduler 3.x 仅由 FastAPI lifespan 启停，导入模块无后台副作用。"""
import logging
import os
import threading
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from knowledge.market.models import SHANGHAI
from knowledge.market.calendar import get_trading_calendar

logger = logging.getLogger(__name__)


def verified_closed(calendar_factory=None):
    try:
        return (calendar_factory or get_trading_calendar)().status()["is_trading_day"] is False
    except Exception:
        logger.warning("交易日历未能核实，继续通过真实行情确认目标日期")
        return False


def build_scheduler(service_factory, hour=15, minute=30, *, calendar_factory=None):
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or (hour, minute) < (15, 15):
        raise ValueError("摘要调度时间须在 Asia/Shanghai 15:15 之后")
    scheduler = BackgroundScheduler(timezone=SHANGHAI)

    def generate():
        try:
            if verified_closed(calendar_factory):
                logger.info("交易日历确认休市，跳过自动摘要")
                return
            service_factory().generate()
        except Exception:
            logger.warning("每日摘要任务未完成，可通过页面检查状态并手动重试")

    scheduler.add_job(generate, trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute,
                      timezone=SHANGHAI), id="daily-summary", max_instances=1, coalesce=True,
                      misfire_grace_time=3600, replace_existing=True)
    return scheduler


def summary_lifespan(enabled=None, service_factory=None, calendar_factory=None, *, runtime=None, close_runtime=False):
    @asynccontextmanager
    async def lifespan(app):
        if runtime is not None:
            runtime.start()
        from knowledge.api.summary_api import get_summary_service
        from knowledge.api.market_collection_api import get_collection_service
        factory = service_factory or ((lambda: runtime.summary) if runtime is not None else get_summary_service)
        default_active = runtime.config.summary_enabled if runtime is not None else os.getenv("DAILY_SUMMARY_ENABLED", "1").lower() in {"1", "true", "yes"}
        active = enabled if enabled is not None else default_active
        if runtime is not None:
            active = active and "summary" in runtime.config.features
        scheduler = None
        collection_scheduler = None
        collection_service = None
        app.state.summary_scheduler = None
        app.state.collection_scheduler = None
        try:
            if active:
                hour = runtime.config.summary_hour if runtime is not None else int(os.getenv("DAILY_SUMMARY_HOUR", "15"))
                minute = runtime.config.summary_minute if runtime is not None else int(os.getenv("DAILY_SUMMARY_MINUTE", "30"))
                scheduler = build_scheduler(factory, hour, minute, calendar_factory=calendar_factory)
                if runtime is not None:
                    runtime.track_scheduler(scheduler)
                scheduler.start()
                app.state.summary_scheduler = scheduler
            collect = (runtime.config.collection_enabled and "collection" in runtime.config.features if runtime is not None
                       else os.getenv("MARKET_COLLECTION_ENABLED", "1" if active else "0").lower() in {"1", "true", "yes"})
            if collect:
                def collect_market():
                    nonlocal collection_service
                    try:
                        if verified_closed(calendar_factory):
                            logger.info("交易日历确认休市，跳过自动全市场采集")
                            return
                        collection_service = runtime.collection_job if runtime is not None else get_collection_service()
                        collection_service.start()
                    except Exception:
                        logger.warning("全市场收盘采集未启动，可在页面检查并重试")

                collection_scheduler = BackgroundScheduler(timezone=SHANGHAI)
                collection_scheduler.add_job(collect_market, CronTrigger(day_of_week="mon-fri", hour=16, minute=0,
                                             timezone=SHANGHAI), id="market-collection", max_instances=1,
                                             coalesce=True, misfire_grace_time=3600)
                if runtime is not None:
                    runtime.track_scheduler(collection_scheduler)
                collection_scheduler.start()
                app.state.collection_scheduler = collection_scheduler
            yield
        finally:
            if runtime is not None:
                result = await run_in_threadpool(runtime.close if close_runtime else runtime.stop)
                if result is False or isinstance(result, dict) and not all(result.values()):
                    logger.error("运行资源退出未完成；保留资源以免边写边删")
            else:
                if collection_service:
                    collection_service.stop.set()
                for task_scheduler in (scheduler, collection_scheduler):
                    if task_scheduler and task_scheduler.running:
                        await run_in_threadpool(task_scheduler.shutdown, wait=True)
                worker = getattr(collection_service, "worker", None)
                if isinstance(worker, threading.Thread) and worker.is_alive():
                    await run_in_threadpool(worker.join, timeout=10)
    return lifespan

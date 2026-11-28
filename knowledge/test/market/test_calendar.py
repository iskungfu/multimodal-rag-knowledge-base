import asyncio
from datetime import date, timedelta
import json
import threading
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from knowledge.api.market_calendar_api import router
from knowledge.market.baostock_source import BaoStockGateway
from knowledge.market.calendar import TradingCalendar, get_trading_calendar
from knowledge.market.models import MarketUnavailable
from knowledge.summary.scheduler import build_scheduler, summary_lifespan, verified_closed
from knowledge.test.market.test_market import NOW


def year_rows(year=2026):
    start, end = date(year, 1, 1), date(year, 12, 31)
    return [{"calendar_date": (start + timedelta(days=offset)).isoformat(),
             "is_trading_day": "0" if (start + timedelta(days=offset)).weekday() > 4 else "1"}
            for offset in range((end - start).days + 1)]


def calendar(rows=None):
    gateway = Mock()
    gateway.request.return_value = {"calendar": year_rows() if rows is None else rows}
    return TradingCalendar(gateway, now=lambda: NOW)


def test_weekday_holiday_weekend_open_day_and_cache():
    rows = year_rows()
    next(row for row in rows if row["calendar_date"] == "2026-10-01")["is_trading_day"] = "0"
    service = calendar(rows)
    holiday = service.status(date(2026, 10, 1))
    assert holiday["is_trading_day"] is False and holiday["source"] == "baostock" and not holiday["cached"]
    assert service.status(date(2026, 11, 27))["is_trading_day"] is True
    assert service.status(date(2026, 11, 28))["is_trading_day"] is False
    assert service.status(date(2026, 11, 27))["cached"] is True
    assert service.gateway.request.call_count == 1


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "outside", "status", "invalid", "non_list"])
def test_incomplete_or_conflicting_calendar_is_unknown_and_not_cached(mutation):
    rows = year_rows()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[-1] = rows[0]
    elif mutation == "outside":
        rows[-1]["calendar_date"] = "2027-01-01"
    elif mutation == "status":
        rows[-1]["is_trading_day"] = "2"
    elif mutation == "invalid":
        rows[-1]["calendar_date"] = "2026-02-30"
    else:
        rows = {}
    service = calendar(rows)
    result = service.status()
    assert result["is_trading_day"] is None and not service.cache
    assert "未知" in result["warning"]


def test_failure_does_not_infer_closed_and_expired_cache_is_not_used():
    service = calendar()
    moment = [0]
    service.clock = lambda: moment[0]
    assert service.status()["is_trading_day"] is True
    moment[0] = 86401
    service.gateway.request.side_effect = MarketUnavailable("private-url")
    result = service.status()
    assert result["is_trading_day"] is None and "private" not in str(result)


def test_invalid_calendar_cools_down_without_blocking_daily_source_and_recovers():
    runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps({"ok": True, "data": {"calendar": []}})))
    moment = [0]
    gateway = BaoStockGateway(runner=runner, clock=lambda: moment[0], sleep=lambda seconds: None)
    service = TradingCalendar(gateway, now=lambda: NOW, clock=lambda: moment[0])
    for _ in range(3):
        assert service.status()["is_trading_day"] is None
    assert runner.call_count == 1 and not service.cache
    runner.return_value.stdout = json.dumps({"ok": True, "data": {"quotes": []}})
    assert gateway.request("daily", date(2026, 11, 27), date(2026, 11, 27), "600519") == {"quotes": []}
    moment[0] = 61
    runner.return_value.stdout = json.dumps({"ok": True, "data": {"calendar": year_rows()}})
    assert service.status()["is_trading_day"] is True
    assert runner.call_count == 3


def test_leap_year_calendar_has_366_days():
    service = calendar(year_rows(2028))
    service.now = lambda: NOW.replace(year=2028)
    assert service.status(date(2028, 2, 29))["is_trading_day"] is True


def test_busy_calendar_returns_unknown_within_bound_and_recovers():
    service = calendar()
    finished = threading.Event()
    results = []

    def query():
        results.append(service.status())
        finished.set()

    service.lock.acquire()
    worker = threading.Thread(target=query, daemon=True)
    try:
        worker.start()
        assert finished.wait(3), "并发日历请求未在等待上限内返回"
        assert results[0]["is_trading_day"] is None and not results[0]["cached"]
        assert "未知" in results[0]["warning"]
        service.gateway.request.assert_not_called()
        assert not service.cache
    finally:
        service.lock.release()
        worker.join(timeout=3)
    assert service.status()["is_trading_day"] is True


def test_calendar_api_returns_provenance_unknown_and_date_limit():
    app = FastAPI()
    app.include_router(router)
    service = calendar()
    app.dependency_overrides[get_trading_calendar] = lambda: service
    with TestClient(app) as client:
        response = client.get("/market/calendar?date=2026-11-27")
        assert response.status_code == 200 and response.json()["source"] == "baostock"
        assert client.get("/market/calendar?date=2020-01-01").status_code == 422
        service.cache.clear()
        service.gateway.request.side_effect = MarketUnavailable()
        response = client.get("/market/calendar?date=2026-11-27")
        assert response.status_code == 200 and response.json()["is_trading_day"] is None


@pytest.mark.parametrize("opened,executed", [(True, True), (False, False), (None, True)])
def test_summary_scheduler_skips_only_verified_closure(opened, executed):
    factory = Mock()
    source = Mock()
    source.status.return_value = {"is_trading_day": opened}
    scheduler = build_scheduler(factory, calendar_factory=lambda: source)
    scheduler.get_job("daily-summary").func()
    assert factory.called is executed
    if executed:
        factory.return_value.generate.assert_called_once_with()


@pytest.mark.parametrize("opened,executed", [(True, True), (False, False), (None, True)])
def test_collection_scheduler_skips_only_verified_closure(monkeypatch, opened, executed):
    from knowledge.api import market_collection_api
    service = Mock()
    factory = Mock(return_value=service)
    factory.cache_info.return_value.currsize = 0
    monkeypatch.setattr(market_collection_api, "get_collection_service", factory)
    monkeypatch.setenv("MARKET_COLLECTION_ENABLED", "1")
    source = Mock()
    source.status.return_value = {"is_trading_day": opened}
    async def run():
        app = FastAPI()
        async with summary_lifespan(False, calendar_factory=lambda: source)(app):
            app.state.collection_scheduler.get_job("market-collection").func()
    asyncio.run(run())
    assert service.start.called is executed


def test_actual_query_app_registers_calendar_without_startup_fetch(monkeypatch):
    from knowledge.api.query_api import create_app
    monkeypatch.setenv("MARKET_COLLECTION_ENABLED", "0")
    app = create_app(summary_enabled=False)
    service = calendar()
    app.dependency_overrides[get_trading_calendar] = lambda: service
    with TestClient(app) as client:
        service.gateway.request.assert_not_called()
        assert client.get("/market/calendar?date=2026-11-27").json()["is_trading_day"] is True


def test_unexpected_calendar_failure_does_not_suppress_jobs():
    failed = Mock(side_effect=RuntimeError("private-connection"))
    assert verified_closed(failed) is False
    factory = Mock()
    scheduler = build_scheduler(factory, calendar_factory=failed)
    scheduler.get_job("daily-summary").func()
    factory.return_value.generate.assert_called_once_with()

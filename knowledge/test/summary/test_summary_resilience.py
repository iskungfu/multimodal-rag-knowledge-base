"""摘要来源故障、缓存溯源和并发初始化回归；仅使用内存存储。"""
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock

import mongomock
import pytest
from fastapi import HTTPException

from knowledge.api import summary_api
from knowledge.market.models import MarketUnavailable, SHANGHAI
from knowledge.service.summary_service import SummaryBusy
from knowledge.summary.indices import (
    EastmoneyIndexAdapter, IndexQuote, IndexRepository, IndexService, TencentIndexAdapter,
)

DAY = date(2026, 11, 27)
NOW = datetime(2026, 11, 27, 17, tzinfo=SHANGHAI)
SYMBOL = "sh000001"
START = DAY - timedelta(days=30)
KLINE = "2026-11-27,3200,3200,3200,3200,10,5000,0,0.1,0,0"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def reject_connection(*args, **kwargs):
        raise AssertionError("摘要回归禁止访问网络或业务存储")

    monkeypatch.setenv("DAILY_SUMMARY_ENABLED", "0")
    monkeypatch.setattr(socket.socket, "connect", reject_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_connection)


@pytest.fixture
def repository():
    client = mongomock.MongoClient(tz_aware=True)
    yield IndexRepository(client.test.indices)
    client.close()


def eastmoney_payload(klines):
    return {"rc": 0, "data": {"code": "000001", "market": 1, "name": "上证指数", "klines": klines}}


def quote(day=DAY, source="eastmoney", close=3200):
    return IndexQuote(symbol=SYMBOL, name="上证指数", date=day, close=close,
                      change_pct=0.1, volume=1000, amount=5000, source=source,
                      updated_at=NOW, raw={})


def adapter(source, quotes):
    result = Mock()
    result.name = source
    result.fetch_daily.return_value = quotes
    return result


@pytest.mark.parametrize("payload", [
    None, [], {}, {"rc": 0, "data": None}, {"rc": 0, "data": []},
    eastmoney_payload(None), eastmoney_payload({}), eastmoney_payload(KLINE),
    eastmoney_payload([None]), eastmoney_payload([42]), eastmoney_payload([{}]),
    eastmoney_payload([[]]), eastmoney_payload([KLINE, None]),
])
def test_malformed_primary_uses_fallback_without_partial_writes(repository, payload):
    primary_http = Mock()
    primary_http.get.return_value = json.dumps(payload).encode("utf-8")
    primary = EastmoneyIndexAdapter(primary_http)
    with pytest.raises(MarketUnavailable, match="指数日线身份或数据无效"):
        primary.fetch_daily(SYMBOL, START, DAY, NOW)
    fallback_http = Mock()
    fallback_http.get.return_value = (Path(__file__).parent / "fixtures/sh000001-tencent.txt").read_bytes()
    service = IndexService(repository, [primary, TencentIndexAdapter(fallback_http)])

    rows, warnings = service.fetch(SYMBOL, START, DAY, NOW)

    fallback_http.get.assert_called_once()
    assert len(rows) == repository.collection.count_documents({}) == 1
    assert rows[0]["date"] == DAY.isoformat() and rows[0]["source"] == "tencent"
    assert any("备用源" in warning for warning in warnings)


@pytest.mark.parametrize("adapter_type", [EastmoneyIndexAdapter, TencentIndexAdapter])
@pytest.mark.parametrize("response", [None, {}, b"invalid response"])
def test_invalid_http_payload_is_market_unavailable(adapter_type, response):
    http = Mock()
    http.get.return_value = response
    with pytest.raises(MarketUnavailable):
        adapter_type(http).fetch_daily(SYMBOL, START, DAY, NOW)


def test_stale_primary_does_not_hide_fresh_fallback(repository):
    repository.save([quote(close=3100)])
    primary = adapter("eastmoney", [quote(DAY - timedelta(days=1))])
    fallback = adapter("tencent", [quote(source="tencent", close=3300)])

    rows, warnings = IndexService(repository, [primary, fallback]).fetch(SYMBOL, START, DAY, NOW)

    fallback.fetch_daily.assert_called_once_with(SYMBOL, START, DAY, NOW)
    assert rows[-1]["close"] == 3300 and rows[-1]["source"] == "tencent"
    assert any("备用源" in warning for warning in warnings)
    assert not any("缓存" in warning for warning in warnings)


@pytest.mark.parametrize("fallback_mode", ["empty", "stale", "unavailable"])
def test_no_source_update_returns_target_cache_with_warning(repository, fallback_mode):
    repository.save([quote(close=3100)])
    primary = adapter("eastmoney", [quote(DAY - timedelta(days=1))])
    fallback = adapter("tencent", [])
    if fallback_mode == "stale":
        fallback.fetch_daily.return_value = [quote(DAY - timedelta(days=1), source="tencent")]
    elif fallback_mode == "unavailable":
        fallback.fetch_daily.side_effect = MarketUnavailable("来源不可用")

    rows, warnings = IndexService(repository, [primary, fallback]).fetch(SYMBOL, START, DAY, NOW)

    fallback.fetch_daily.assert_called_once_with(SYMBOL, START, DAY, NOW)
    assert rows[-1]["date"] == DAY.isoformat() and rows[-1]["close"] == 3100
    assert any("目标日收盘缓存" in warning for warning in warnings)


def test_missing_target_day_is_not_replaced_by_previous_day(repository):
    primary = adapter("eastmoney", [quote(DAY - timedelta(days=1))])
    fallback = adapter("tencent", [])
    with pytest.raises(MarketUnavailable, match="目标日收盘数据尚未核实"):
        IndexService(repository, [primary, fallback]).fetch(SYMBOL, START, DAY, NOW)
    fallback.fetch_daily.assert_called_once()


def test_fresh_primary_replaces_cache_without_fallback(repository):
    repository.save([quote(close=3100)])
    primary = adapter("eastmoney", [quote(close=3300)])
    fallback = adapter("tencent", [])

    rows, warnings = IndexService(repository, [primary, fallback]).fetch(SYMBOL, START, DAY, NOW)

    assert rows[-1]["close"] == 3300 and warnings == []
    fallback.fetch_daily.assert_not_called()


@pytest.fixture
def mongo_factory(monkeypatch):
    summary_api._create_summary_service.cache_clear()
    factory = Mock(side_effect=lambda *args, **kwargs: mongomock.MongoClient(tz_aware=True))
    monkeypatch.setattr("pymongo.MongoClient", factory)
    monkeypatch.setenv("MONGO_URL", "mongodb://summary-test.invalid")
    monkeypatch.setenv("MONGO_DB_NAME", "summary_resilience_test")
    yield factory
    summary_api._create_summary_service.cache_clear()


def test_concurrent_first_requests_share_service_and_generation_slot(monkeypatch, mongo_factory):
    workers = 8
    constructor_entered = threading.Event()
    release_constructor = threading.Event()
    all_callers_entered = threading.Event()

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()
            self.counter_lock = threading.Lock()
            self.callers = 0

        def __enter__(self):
            with self.counter_lock:
                self.callers += 1
                if self.callers == workers:
                    all_callers_entered.set()
            self.lock.acquire()
            return self

        def __exit__(self, *args):
            self.lock.release()

    def connect(*args, **kwargs):
        constructor_entered.set()
        assert release_constructor.wait(5)
        return mongomock.MongoClient(tz_aware=True)

    monkeypatch.setattr(summary_api, "_summary_service_lock", ObservedLock())
    mongo_factory.side_effect = connect
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(summary_api.get_summary_service) for index in range(workers)]
        try:
            assert constructor_entered.wait(5)
            assert all_callers_entered.wait(5)
        finally:
            release_constructor.set()
        services = [future.result(timeout=5) for future in futures]

    mongo_factory.assert_called_once()
    assert all(service is services[0] for service in services)
    service = services[0]
    service.now = lambda: NOW
    claim, record = service.begin(DAY)
    assert claim and record["status"] == "processing"
    try:
        assert services[-1].begin(DAY)[0] is None
        with pytest.raises(SummaryBusy):
            services[-1].begin(DAY - timedelta(days=1))
    finally:
        service.slot.release()
        service.mongo_client.close()


def test_failed_initialization_closes_client_and_allows_retry(mongo_factory):
    failed_client = MagicMock()
    failed_client.__getitem__.return_value.daily_summaries.create_index.side_effect = RuntimeError("private-token")
    recovered_client = mongomock.MongoClient(tz_aware=True)
    mongo_factory.side_effect = [failed_client, recovered_client]

    with pytest.raises(HTTPException) as failure:
        summary_api.get_summary_service()

    assert failure.value.status_code == 503 and "private-token" not in failure.value.detail
    failed_client.close.assert_called_once()
    service = summary_api.get_summary_service()
    assert service.mongo_client is recovered_client
    assert summary_api.get_summary_service() is service
    assert mongo_factory.call_count == 2
    recovered_client.close()

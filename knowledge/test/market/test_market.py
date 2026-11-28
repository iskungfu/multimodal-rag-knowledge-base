import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import ConnectionFailure

from knowledge.api.market_api import get_market_service, router
from knowledge.market.adapters import EastmoneyAdapter, MarketHTTP, TencentAdapter
from knowledge.market.models import MarketUnavailable, SHANGHAI
from knowledge.service.market_service import MarketService

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 27, 18, tzinfo=SHANGHAI)
START, END = date(2026, 11, 25), date(2026, 11, 27)


def eastmoney(daily=None, snapshot=None):
    http = Mock()
    http.get.side_effect = [
        json.dumps(daily).encode() if daily is not None else (FIXTURES / "eastmoney_daily.json").read_bytes(),
        json.dumps(snapshot).encode() if snapshot is not None else (FIXTURES / "eastmoney_snapshot.json").read_bytes(),
    ]
    return EastmoneyAdapter(http)


def tencent(raw=None):
    http = Mock()
    http.get.return_value = raw or (FIXTURES / "tencent_snapshot.gbk").read_bytes()
    return TencentAdapter(http)


class MemoryRepository:
    def __init__(self):
        self.rows = {}
        self.writes = 0

    def save(self, quotes):
        self.writes += 1
        for quote in quotes:
            self.rows[(quote.code, quote.date)] = quote.model_dump(exclude={"raw"})

    def read(self, code, start, end):
        return [row for (c, d), row in sorted(self.rows.items()) if c == code and start <= d <= end]


def client_for(service):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_market_service] = lambda: service
    return TestClient(app)


def test_verified_daily_and_snapshot_units_and_provenance():
    quotes = eastmoney().fetch_daily("600519", START, END, NOW)
    assert len(quotes) == 3
    first, _, last = quotes
    assert first.pe is None and first.source_time is None
    assert last.name == "贵州茅台"
    assert (last.open, last.close, last.high, last.low, last.pre_close) == (1469.99, 1472.36, 1481.41, 1452.99, 1469)
    assert (last.change_pct, last.turnover, last.volume, last.amount) == (0.23, 0.32, 3980800, 5849112431)
    assert (last.pe, last.pb) == (21.41, 6.79)
    assert last.market_cap == pytest.approx(1840570146048.36)
    assert last.raw["snapshot"]["data"]["f169"] == 336  # 是涨跌额，不能当 PE
    assert last.source_time.date() == END
    assert last.updated_at == NOW


def test_tencent_gbk_and_dynamic_pe_match_primary_source():
    primary = eastmoney().fetch_daily("600519", START, END, NOW)[-1]
    backup = tencent().fetch_daily("600519", START, END, NOW)[0]
    for key in ("code", "name", "date", "close", "open", "high", "low", "pre_close", "change_pct", "turnover", "volume", "amount", "pe", "pb"):
        assert getattr(backup, key) == getattr(primary, key)
    assert backup.market_cap == 1840570000000  # 腾讯市值仅精确到 0.01 亿元


def test_snapshot_uses_precision_metadata():
    snapshot = json.loads((FIXTURES / "eastmoney_snapshot.json").read_bytes())
    snapshot["data"].update(f59=3, f43=1472360)
    adapter = EastmoneyAdapter(Mock())
    adapter.http.get.return_value = json.dumps(snapshot).encode()
    assert adapter.snapshot("600519", NOW)["close"] == 1472.36


def test_missing_values_are_null_and_never_zero():
    daily = json.loads((FIXTURES / "eastmoney_daily.json").read_bytes())
    daily["data"]["klines"][-1] = daily["data"]["klines"][-1].replace(",0.23,3.36,0.32", ",-,3.36,-")
    snapshot = json.loads((FIXTURES / "eastmoney_snapshot.json").read_bytes())
    snapshot["data"].update(f162="-", f167="-", f116="-")
    last = eastmoney(daily, snapshot).fetch_daily("600519", START, END, NOW)[-1]
    assert (last.pe, last.pb, last.market_cap, last.change_pct, last.turnover) == (None,) * 5


@pytest.mark.parametrize("mutation", [{"f86": 0}, {"f86": 1e100}, {"f86": int(NOW.timestamp()) + 86400}, {"f43": 100}, {"f57": "000001"}])
def test_mismatched_or_undated_snapshot_cannot_enrich_history(mutation):
    snapshot = json.loads((FIXTURES / "eastmoney_snapshot.json").read_bytes())
    snapshot["data"].update(mutation)
    assert all(q.pe is None for q in eastmoney(snapshot=snapshot).fetch_daily("600519", START, END, NOW))


def test_history_does_not_receive_current_pe():
    quotes = eastmoney().fetch_daily("600519", START, END - timedelta(days=1), NOW)
    assert len(quotes) == 2 and all(q.pe is None for q in quotes)


@pytest.mark.parametrize("raw", [b'{}', b'{"rc":0,"data":null}', b'not-json', b'{"rc":1,"data":{}}'])
def test_malformed_response_is_controlled_failure(raw):
    http = Mock()
    http.get.return_value = raw
    with pytest.raises(MarketUnavailable):
        EastmoneyAdapter(http).fetch_daily("600519", START, END, NOW)


@pytest.mark.parametrize("stamp", ["20261127143000", "20261128161500", "20261124161500"])
def test_tencent_rejects_intraday_future_and_out_of_range(stamp):
    raw = (FIXTURES / "tencent_snapshot.gbk").read_bytes().replace(b"20261127161454", stamp.encode())
    with pytest.raises(MarketUnavailable):
        tencent(raw).fetch_daily("600519", START, END, NOW)


@pytest.mark.parametrize("failure", [requests.Timeout(), requests.ConnectionError(), requests.HTTPError(response=Mock(status_code=429))])
def test_bounded_retries_cooldown_and_next_day_recovery(failure):
    tick = [100.0]
    def advance(seconds):
        tick[0] += seconds
    session = Mock()
    session.get.side_effect = [failure, failure, Mock(content=b"ok")]
    http = MarketHTTP(session, clock=lambda: tick[0], sleep=advance)
    with pytest.raises(MarketUnavailable):
        http.get("https://example.invalid")
    for _ in range(5):
        with pytest.raises(MarketUnavailable):
            http.get("https://example.invalid")
    assert session.get.call_count == 2
    tick[0] += 86400
    assert http.get("https://example.invalid") == b"ok"


def test_api_refresh_read_cache_and_latest_date():
    repository = MemoryRepository()
    service = MarketService(repository, [eastmoney()], now=lambda: NOW)
    with client_for(service) as client:
        response = client.post("/market/quotes/600519/refresh?start=2026-11-25&end=2026-11-27")
        assert response.status_code == 200
        body = response.json()
        assert body["quotes"][-1]["date"] == "2026-11-27"
        assert "raw" not in body["quotes"][-1]
        again = client.post("/market/quotes/600519/refresh?start=2026-11-25&end=2026-11-27")
        assert again.json()["cached"] is True and repository.writes == 1
        stored = client.get("/market/quotes/600519?start=2026-11-25&end=2026-11-27")
        assert stored.json()["quotes"] == body["quotes"]


def test_fallback_exposes_partial_range_and_source():
    failed = Mock(name="primary")
    failed.fetch_daily.side_effect = MarketUnavailable()
    result = MarketService(MemoryRepository(), [failed, tencent()], now=lambda: NOW).refresh("600519", START, END)
    assert result["source"] == "tencent" and len(result["quotes"]) == 1
    assert any("历史区间可能不完整" in warning for warning in result["warnings"])


@pytest.mark.parametrize("row", [None, 123, {}, []])
def test_malformed_daily_row_uses_fallback_without_writing_primary(row):
    raw = json.loads((FIXTURES / "eastmoney_daily.json").read_text(encoding="utf-8"))
    raw["data"]["klines"].append(row)
    repository = MemoryRepository()
    result = MarketService(repository, [eastmoney(daily=raw), tencent()], now=lambda: NOW).refresh("600519", START, END)
    assert result["source"] == "tencent"
    assert repository.writes == 1
    assert all(quote["source"] == "tencent" for quote in result["quotes"])


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_same_day_quotes_are_rejected_before_storage(reverse):
    quotes = eastmoney().fetch_daily("600519", START, END, NOW)
    quotes.append(quotes[-1].model_copy(update={"close": quotes[-1].close + 1}))
    adapter = Mock()
    adapter.name = "eastmoney"
    adapter.fetch_daily.return_value = list(reversed(quotes)) if reverse else quotes
    repository = MemoryRepository()
    service = MarketService(repository, [adapter], now=lambda: NOW)
    with pytest.raises(MarketUnavailable):
        service.refresh("600519", START, END)
    assert repository.writes == 0 and repository.rows == {}
    assert service.cache == {}


def test_identical_daily_duplicates_remain_idempotent():
    quotes = eastmoney().fetch_daily("600519", START, END, NOW)
    adapter = Mock()
    adapter.name = "eastmoney"
    adapter.fetch_daily.return_value = quotes + [quotes[-1].model_copy(deep=True)]
    repository = MemoryRepository()
    result = MarketService(repository, [adapter], now=lambda: NOW).refresh("600519", START, END)
    assert len(result["quotes"]) == len(quotes)
    assert repository.writes == 1


def test_old_source_date_is_not_relabelled_as_today():
    next_day = NOW + timedelta(days=1)
    result = MarketService(MemoryRepository(), [tencent()], now=lambda: next_day).refresh("600519")
    assert result["quotes"][0]["date"] == END
    assert any("早于请求结束日期" in warning for warning in result["warnings"])


def test_failures_return_503_without_writing_or_leaking_connection():
    repository = MemoryRepository()
    failed = Mock()
    failed.fetch_daily.side_effect = MarketUnavailable("private-url")
    service = MarketService(repository, [failed], now=lambda: NOW)
    with client_for(service) as client:
        response = client.post("/market/quotes/600519/refresh")
        assert response.status_code == 503 and response.headers["Retry-After"] == "60"
        assert "private-url" not in response.text and repository.writes == 0
        repository.read = Mock(side_effect=ConnectionFailure("mongodb://secret@private"))
        response = client.get("/market/quotes/600519")
        assert response.status_code == 503 and "secret" not in response.text


@pytest.mark.parametrize("path", [
    "abc/refresh", "510300/refresh", "1.000001/refresh",
    "600519/refresh?start=2026-11-27&end=2026-11-25",
    "600519/refresh?end=2026-11-28", "600519/refresh?start=2020-01-01", "600519/refresh?end=invalid",
])
def test_invalid_requests_fail_before_fetch(path):
    adapter = Mock()
    service = MarketService(MemoryRepository(), [adapter], now=lambda: NOW)
    with client_for(service) as client:
        assert client.post("/market/quotes/" + path).status_code == 422
    adapter.fetch_daily.assert_not_called()


def test_intraday_request_cannot_persist_unfinished_bar():
    adapter = Mock()
    service = MarketService(MemoryRepository(), [adapter], now=lambda: NOW.replace(hour=14))
    with client_for(service) as client:
        assert client.post("/market/quotes/600519/refresh?end=2026-11-27").status_code == 422
    adapter.fetch_daily.assert_not_called()


def test_next_day_refresh_is_not_hidden_by_cache():
    current = [NOW]
    tick = [0]
    adapter = Mock(name="tencent")
    adapter.name = "tencent"
    adapter.fetch_daily.return_value = tencent().fetch_daily("600519", START, END, NOW)
    service = MarketService(MemoryRepository(), [adapter], now=lambda: current[0], clock=lambda: tick[0])
    service.refresh("600519")
    current[0] += timedelta(days=1)
    tick[0] += 86400
    service.refresh("600519")
    assert adapter.fetch_daily.call_count == 2

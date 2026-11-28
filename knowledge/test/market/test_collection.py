from datetime import date, datetime, timedelta
import json
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import mongomock
import pytest

from knowledge.api.market_collection_api import get_collection_service, router
from knowledge.market.adapters import TencentAdapter
from knowledge.market.batch import BatchQuotes
from knowledge.market.collection import CollectionBusy, CollectionRepository, CollectionStopped, MarketCollectionService
from knowledge.market.models import DailyQuote, MarketUnavailable, SHANGHAI, market_symbol
from knowledge.market.repository import MarketQuoteRepository
from knowledge.market.universe import MarketUniverse
from knowledge.risk.symbols import SymbolResolver

NOW = datetime(2026, 11, 27, 18, tzinfo=SHANGHAI)
DAY = NOW.date()


def item(code="000001", name="平安银行"):
    return {"code": code, "name": name, "symbol": market_symbol(code)}


def quote(code="000001", name="平安银行", day=DAY):
    return DailyQuote(code=code, name=name, date=day, close=12, open=11, high=13, low=10,
                      pre_close=11, volume=10000, amount=120000, change_pct=9.09, pe=10,
                      source="tencent", source_time=NOW, updated_at=NOW, raw={"snapshot": "verified"})


def service_fixture(items=None, complete=True):
    db = mongomock.MongoClient(tz_aware=True).test
    repository = CollectionRepository(db.runs, db.locks)
    universe, batch = Mock(), Mock()
    universe.fetch.return_value = {"items": items or [item()], "source": "eastmoney" if complete else "sina", "complete": complete}
    batch.fetch.return_value = ([quote()], {})
    service = MarketCollectionService(repository, MarketQuoteRepository(db.quotes), universe=universe,
                                      batch=batch, now=lambda: NOW)
    return service, db, universe, batch


def run(service, retry=False):
    token = service.repository.claim(DAY, service.now(), retry)
    assert token
    service.run(DAY, token)
    return service.repository.get(DAY, service.now())


def test_batch_collection_persists_daily_quotes_and_idempotent_progress():
    service, db, _, batch = service_fixture()
    result = run(service)
    assert result["status"] == "completed" and result["succeeded"] == result["total"] == 1
    assert db.quotes.count_documents({"date": DAY.isoformat()}) == 1
    assert service.start(DAY)["started"] is False
    result = run(service, True)
    assert batch.fetch.call_count == 1
    assert result["attempts"] == 2 and db.locks.count_documents({}) == 0


def test_partial_run_retries_only_missing_and_never_labels_backup_complete():
    service, db, universe, batch = service_fixture([item(), item("600519", "贵州茅台")])
    batch.fetch.return_value = ([quote()], {"600519": "无行情"})
    assert run(service)["status"] == "partial"
    batch.fetch.return_value = ([quote("600519", "贵州茅台")], {})
    result = run(service)
    assert result["status"] == "completed" and result["failed"] == 0
    assert batch.fetch.call_args.args[0] == [item("600519", "贵州茅台")]
    universe.fetch.return_value["complete"] = False
    result = run(service, True)
    assert result["status"] == "partial" and result["warning"]


@pytest.mark.parametrize("bad", [quote(day=DAY - timedelta(days=1)), quote("600519", "贵州茅台"), quote(name="错误名称")])
def test_wrong_date_or_identity_is_rejected_before_storage(bad):
    service, db, _, batch = service_fixture()
    batch.fetch.return_value = ([bad], {})
    assert run(service)["status"] == "failed"
    assert db.quotes.count_documents({}) == 0


def test_expired_worker_cannot_publish_or_release_new_lease():
    service, db, _, batch = service_fixture()
    first = service.repository.claim(DAY, NOW)
    with pytest.raises(CollectionBusy):
        service.repository.claim(DAY, NOW)
    future = NOW + timedelta(seconds=181)
    second = service.repository.claim(DAY, future)
    service.now = lambda: future
    service.run(DAY, first)
    assert db.runs.find_one({})["token"] == second
    assert db.locks.find_one({})["token"] == second
    assert db.quotes.count_documents({}) == 0
    batch.fetch.assert_not_called()


def test_interrupted_or_storage_failed_run_retains_existing_data_and_hides_errors():
    service, db, _, batch = service_fixture()
    db.quotes.insert_one({"code": "600519", "date": DAY.isoformat()})
    batch.fetch.side_effect = RuntimeError("private-token")
    result = run(service)
    assert result["status"] == "failed" and "private-token" not in json.dumps(result, default=str)
    assert db.quotes.count_documents({}) == 1
    service.stop.set()
    batch.reset_mock()
    assert run(service)["status"] == "failed"
    batch.fetch.assert_not_called()


def test_api_validates_date_and_has_persistent_results():
    service, _, _, _ = service_fixture()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_collection_service] = lambda: service
    with TestClient(app) as client:
        assert client.post("/market/collection/run", json={"date": "2026-11-28"}).status_code == 422
        assert client.post("/market/collection/run", json={"date": DAY.isoformat()}).status_code == 202
        service.worker.join(timeout=5)
        assert client.get("/market/collection/latest").json()["record"]["status"] == "completed"
        assert "token" not in client.get("/market/collection/2026-11-27").text


def test_universe_checks_pages_unique_codes_and_unstable_counts():
    primary = Mock()
    rows = [{"f12": f"600{index:03}", "f13": 1, "f14": f"证券{index}"} for index in range(1000)]

    def get(url, params):
        offset = (params["pn"] - 1) * 100
        return json.dumps({"rc": 0, "data": {"total": 1000, "diff": rows[offset:offset + 100]}}).encode()

    primary.get.side_effect = get
    backup = Mock()
    backup.get.side_effect = MarketUnavailable("备用失败")
    source = MarketUniverse(primary, backup)
    result = source.fetch()
    assert len(result["items"]) == 1000 and result["complete"]
    rows[100] = rows[0]
    with pytest.raises(MarketUnavailable):
        source.fetch()


def test_sina_fallback_is_always_marked_incomplete():
    primary, backup = Mock(), Mock()
    primary.get.side_effect = MarketUnavailable("主源失败")
    rows = [{"code": f"600{index:03}", "symbol": f"sh600{index:03}", "name": f"证券{index}"} for index in range(1000)]

    def get(url, params):
        if "Count" in url:
            return b'"1000"'
        offset = (params["page"] - 1) * 100
        return json.dumps(rows[offset:offset + 100]).encode()

    backup.get.side_effect = get
    result = MarketUniverse(primary, backup).fetch()
    assert len(result["items"]) == 1000 and not result["complete"] and result["source"] == "sina"


def snapshot(code, name, stamp="20261127161500"):
    fields = ["0"] * 88
    for index, value in {1: name, 2: code, 3: "12", 4: "11", 5: "11.5", 6: "100", 30: stamp,
                         32: "9.09", 33: "13", 34: "10", 38: "2", 45: "10", 46: "1", 52: "8", 57: "12"}.items():
        fields[index] = value
    return f'v_{market_symbol(code)}="' + "~".join(fields) + '";'


def test_batch_validates_each_symbol_date_name_and_units():
    http, fallback = Mock(), Mock()
    fallback.fetch_daily.side_effect = MarketUnavailable("备用失败")
    http.get.return_value = (snapshot("000001", "平安银行") + "\n" + snapshot("920002", "万达轴承") + "\n" +
                            snapshot("600519", "贵州茅台", "20261126161500")).encode("gbk")
    rows, failures = BatchQuotes(http, fallback).fetch([item(), item("920002", "万达轴承"), item("600519", "贵州茅台")], DAY, NOW)
    assert {row.code for row in rows} == {"000001", "920002"}
    assert failures == {"600519": "无目标日收盘快照或身份不一致"}
    assert rows[0].volume == 10000 and rows[0].amount == 120000 and rows[0].market_cap == 1000000000


def test_beijing_identity_requires_exchange_type_not_just_code_prefix():
    http = Mock()
    row = {"Code": "920002", "Name": "万达轴承", "Classify": "NEEQ", "SecurityType": "27", "SecurityTypeName": "京A", "JYS": "81", "MktNum": "0", "QuoteID": "0.920002"}
    http.get.return_value = json.dumps({"QuotationCodeTable": {"Status": 0, "Data": [row]}}).encode()
    assert SymbolResolver(http).resolve("bj920002风险").security.code == "920002"
    row["SecurityTypeName"] = "新三板"
    http.get.return_value = json.dumps({"QuotationCodeTable": {"Status": 0, "Data": [row]}}).encode()
    assert SymbolResolver(http).resolve("920002风险").security is None


def test_existing_302_security_is_supported_without_accepting_etfs():
    assert market_symbol("302132") == "sz302132"
    with pytest.raises(ValueError):
        market_symbol("510300")


def test_mixed_source_directory_reports_exclusions_instead_of_claiming_full_market():
    records = [item(f"600{index:03}", f"证券{index}") for index in range(1000)]
    records.append({"code": "810011", "symbol": "sz810011", "name": "定转"})
    result = MarketUniverse._finish(records, 1001, "eastmoney", True)
    assert len(result["items"]) == 1000 and result["reported_total"] == 1001
    assert result["excluded_codes"] == ["810011"] and not result["complete"]


def test_name_format_normalization_preserves_special_treatment_markers():
    from knowledge.market.models import security_name
    assert security_name("万  科Ａ") == security_name("万科A")
    assert security_name("*ST股票") != security_name("股票")


def test_actual_collection_scheduler_uses_service_and_stops(monkeypatch):
    import asyncio
    from knowledge.api import market_collection_api
    from knowledge.summary.scheduler import summary_lifespan
    service, _, _, _ = service_fixture()
    factory = Mock(return_value=service)
    factory.cache_info.return_value.currsize = 1
    monkeypatch.setattr(market_collection_api, "get_collection_service", factory)
    monkeypatch.setenv("MARKET_COLLECTION_ENABLED", "1")

    async def check():
        app = FastAPI()
        async with summary_lifespan(False, calendar_factory=lambda: Mock(status=lambda: {"is_trading_day": True}))(app):
            scheduler = app.state.collection_scheduler
            job = scheduler.get_job("market-collection")
            assert str(job.trigger.timezone) == "Asia/Shanghai" and job.max_instances == 1
            next_run = job.trigger.get_next_fire_time(None, NOW.replace(hour=15))
            assert next_run.hour == 16 and next_run.minute == 0
            job.func()
            service.worker.join(timeout=5)
            assert service.repository.get(DAY, NOW)["status"] == "completed"
        assert not scheduler.running and service.stop.is_set()
    asyncio.run(check())


def test_late_collector_cannot_overwrite_newer_or_interactive_quotes():
    service, db, _, _ = service_fixture()
    newer = quote()
    newer.close = 13
    service.quotes.save([newer])
    service.quotes.save_missing([quote()])
    assert db.quotes.find_one({"code": "000001"})["close"] == 13


def test_recovery_uses_authoritative_lock_while_run_heartbeat_is_pending():
    service, db, _, _ = service_fixture()
    token = service.repository.claim(DAY, NOW)
    future = NOW + timedelta(seconds=181)
    db.locks.update_one({"token": token}, {"$set": {"lease_until": future + timedelta(seconds=180)}})
    service.repository.recover(future)
    assert db.runs.find_one({})["status"] == "running"


def test_interruption_counts_all_unprocessed_symbols():
    records = [item(f"600{index:03}", f"证券{index}") for index in range(100)]
    service, _, _, batch = service_fixture(records)
    first = [quote(record["code"], record["name"]) for record in records[:50]]
    batch.fetch.side_effect = [(first, {}), RuntimeError("timeout")]
    result = run(service)
    assert result["status"] == "failed" and result["succeeded"] == result["failed"] == 50
    assert len(result["failures"]) == 50 and result["pending"] == 0


def test_missing_optional_fields_are_reported_without_faking_values():
    service, _, _, batch = service_fixture()
    value = quote()
    value.pe = None
    batch.fetch.return_value = ([value], {})
    result = run(service)
    assert result["status"] == "completed" and result["incomplete_fields"] == 1 and result["data_warning"]


def test_more_than_five_missing_snapshots_still_use_historical_fallback():
    http, fallback = Mock(), Mock()
    http.get.return_value = b""
    records = [item(f"600{index:03}", f"证券{index}") for index in range(6)]
    fallback.fetch_daily.side_effect = lambda code, start, end, now: [quote(code, next(record["name"] for record in records if record["code"] == code))]
    rows, failures = BatchQuotes(http, fallback).fetch(records, DAY, NOW)
    assert len(rows) == 6 and not failures and fallback.fetch_daily.call_count == 6


def test_collection_validation_does_not_echo_private_input(monkeypatch):
    from knowledge.api.query_api import create_app
    service, _, _, _ = service_fixture()
    monkeypatch.setenv("DAILY_SUMMARY_ENABLED", "0")
    monkeypatch.setenv("MARKET_COLLECTION_ENABLED", "0")
    app = create_app()
    app.dependency_overrides[get_collection_service] = lambda: service
    with TestClient(app) as client:
        for method, path, payload in [("post", "/market/collection/run", {"date": "13812345678"}),
                                      ("get", "/market/collection/13812345678", None)]:
            response = getattr(client, method)(path, **({"json": payload} if payload else {}))
            assert response.status_code == 422 and "13812345678" not in response.text


def test_invalid_existing_row_is_not_counted_as_saved_or_silently_overwritten():
    service, db, _, _ = service_fixture()
    db.quotes.insert_one({"code": "000001", "date": DAY.isoformat(), "close": 999})
    result = run(service)
    assert result["status"] == "failed" and result["succeeded"] == 0 and result["failed"] == 1
    assert "已有记录" in result["failures"][0]["reason"]
    assert db.quotes.find_one({})["close"] == 999


@pytest.mark.parametrize("existing", [False, True])
def test_delayed_claim_cannot_replace_a_new_owner(monkeypatch, existing):
    service, db, _, _ = service_fixture()
    if existing:
        run(service)
    original_update = db.runs.update_one
    latest = []

    def interleave(query, update, **kwargs):
        if "$inc" in update and not latest:
            latest.append("starting")
            latest[0] = service.repository.claim(DAY, NOW + timedelta(seconds=181), retry=True)
        return original_update(query, update, **kwargs)

    monkeypatch.setattr(db.runs, "update_one", interleave)
    with pytest.raises(CollectionStopped):
        service.repository.claim(DAY, NOW, retry=True)
    assert db.runs.find_one({})["token"] == latest[0]
    assert db.locks.find_one({})["token"] == latest[0]
    service.repository.renew(DAY, latest[0], NOW + timedelta(seconds=182))


def test_collection_rejects_older_than_366_days():
    service, _, _, _ = service_fixture()
    with pytest.raises(ValueError, match="366"):
        service.validate_day(date(2020, 1, 2))

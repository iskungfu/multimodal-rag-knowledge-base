import copy
import json
from datetime import date, timedelta
from unittest.mock import Mock

import pytest
import mongomock

from knowledge.market.adapters import EastmoneyAdapter, TencentAdapter
from knowledge.market.batch import BatchQuotes
from knowledge.market.models import MarketUnavailable, market_symbol
from knowledge.market.tencent_history import TencentHistoryAdapter
from knowledge.market.repository import MarketQuoteRepository
from knowledge.service.market_service import MarketService
from knowledge.test.market.test_market import END, FIXTURES, NOW, START, MemoryRepository, client_for, tencent


def payload(code="600519"):
    identity = (FIXTURES / "tencent_snapshot.gbk").read_bytes().decode("gbk").split('"')[1].split("~")
    identity[2] = code
    return {"code": 0, "data": {market_symbol(code): {
        "day": [
            ["2026-11-25", "1460", "1465", "1470", "1450", "30000", {}, "0.24", "439500"],
            ["2026-11-26", "1465", "1469", "1475", "1460", "35000", {}, "0.28", "514150"],
            ["2026-11-27", "1469.99", "1472.36", "1481.41", "1452.99", "39808.00", {}, "0.32", "584911.24"],
        ], "qt": {market_symbol(code): identity},
    }}}


def adapter_for(body=None):
    http = Mock()
    http.get.return_value = json.dumps(payload() if body is None else body).encode("utf-8")
    return TencentHistoryAdapter(http)


def test_history_preserves_percent_units_and_only_enriches_matched_close():
    adapter = adapter_for()
    rows = adapter.fetch_daily("600519", START, END, NOW)
    first, _, last = rows
    assert len(rows) == 3 and all(row.source == "tencent_history" for row in rows)
    assert (first.volume, first.amount, first.turnover) == (3000000, 4395000000, 0.24)
    assert (first.pre_close, first.change_pct, first.pe, first.source_time) == (None,) * 4
    assert (last.close, last.volume, last.amount, last.turnover, last.pe) == (1472.36, 3980800, 5849112431, 0.32, 21.41)
    assert last.change_pct == 0.23 and last.valuation_source == "tencent"
    assert first.raw["fqt"] == 0 and first.raw["volume_multiplier"] == 100
    assert "qt" not in first.raw and last.raw["qt"][2] == "600519"
    assert adapter.http.get.call_args.args[1]["param"].endswith(",640,")


@pytest.mark.parametrize("code,multiplier", [("600519", 100), ("000001", 100), ("688981", 1), ("689009", 1), ("920002", 100), ("302132", 100)])
def test_snapshot_and_history_agree_on_board_specific_volume(code, multiplier):
    body = payload(code)
    history = adapter_for(body).fetch_daily(code, START, END, NOW)[-1]
    identity = body["data"][market_symbol(code)]["qt"][market_symbol(code)]
    text = f'v_{market_symbol(code)}="' + "~".join(identity) + '";'
    snapshot = TencentAdapter().parse_snapshot(text, code, START, END, NOW)
    assert history.volume == snapshot.volume == 39808 * multiplier
    assert snapshot.raw["volume_multiplier"] == multiplier


def test_historical_subset_keeps_missing_returns_and_excludes_current_snapshot():
    body = payload()
    body["data"]["sh600519"]["day"][0][1:5] = ["2000", "2000", "2000", "2000"]
    rows = adapter_for(body).fetch_daily("600519", START, END - timedelta(days=1), NOW)
    assert len(rows) == 2
    assert all(row.change_pct is None and row.pre_close is None and row.pe is None for row in rows)


@pytest.mark.parametrize("mutation", ["code", "symbol", "adjusted_only", "row", "invalid_date", "range", "duplicate", "non_finite", "identity", "name", "status"])
def test_invalid_source_never_saves_partial_history(mutation):
    body = payload()
    data = body["data"]["sh600519"]
    if mutation == "code":
        data["qt"]["sh600519"][2] = "000001"
    elif mutation == "symbol":
        body["data"]["sz600519"] = body["data"].pop("sh600519")
    elif mutation == "adjusted_only":
        data["qfqday"] = data.pop("day")
    elif mutation == "row":
        data["day"].append({"date": "2026-11-27"})
    elif mutation == "invalid_date":
        data["day"][0][0] = "2026-02-30"
    elif mutation == "range":
        data["day"][0][3] = "1"
    elif mutation == "duplicate":
        conflict = copy.deepcopy(data["day"][-1])
        conflict[2] = "1473"
        data["day"].append(conflict)
    elif mutation == "non_finite":
        data["day"][0][5] = "NaN"
    elif mutation == "identity":
        data["qt"]["sh600519"] = None
    elif mutation == "name":
        data["qt"]["sh600519"][1] = ""
    else:
        body["code"] = 1
    repository = MemoryRepository()
    service = MarketService(repository, [adapter_for(body)], now=lambda: NOW)
    with pytest.raises(MarketUnavailable):
        service.refresh("600519", START, END)
    assert repository.writes == 0 and not service.cache


@pytest.mark.parametrize("body", [None, [], {"code": 0, "data": None}])
def test_non_object_payload_is_controlled(body):
    adapter = adapter_for()
    adapter.http.get.return_value = json.dumps(body).encode()
    with pytest.raises(MarketUnavailable):
        adapter.fetch_daily("600519", START, END, NOW)


@pytest.mark.parametrize("field,value", [(30, "20261127143000"), (30, "20261128161500"), (3, "1473"), (6, "39809")])
def test_inconsistent_snapshot_cannot_enrich_but_history_survives(field, value):
    body = payload()
    body["data"]["sh600519"]["qt"]["sh600519"][field] = value
    last = adapter_for(body).fetch_daily("600519", START, END, NOW)[-1]
    assert last.pe is None and last.change_pct is None and last.source_time is None
    assert last.amount == 5849112400


def test_short_rows_keep_optional_fields_missing_and_duplicate_rows_deduplicate():
    body = payload()
    rows = body["data"]["sh600519"]["day"]
    rows[0] = rows[0][:6]
    rows.append(copy.deepcopy(rows[0]))
    result = adapter_for(body).fetch_daily("600519", START, END, NOW)
    assert len(result) == 3 and result[0].turnover is None and result[0].amount is None


def test_capped_history_does_not_claim_older_range_coverage():
    body = payload()
    body["data"]["sh600519"]["day"] = [body["data"]["sh600519"]["day"][-1]] * 640
    with pytest.raises(MarketUnavailable):
        adapter_for(body).fetch_daily("600519", START, END, NOW)


def test_future_request_is_rejected_before_network():
    adapter = adapter_for()
    with pytest.raises(ValueError):
        adapter.fetch_daily("600519", START, END + timedelta(days=1), NOW)
    adapter.http.get.assert_not_called()


def test_api_fallback_returns_historical_range_and_honest_warnings():
    failed = Mock()
    failed.fetch_daily.side_effect = MarketUnavailable("private-source-url")
    service = MarketService(MemoryRepository(), [failed, adapter_for()], now=lambda: NOW)
    with client_for(service) as client:
        response = client.post("/market/quotes/600519/refresh?start=2026-11-25&end=2026-11-27")
        assert response.status_code == 200
        body = response.json()
        assert len(body["quotes"]) == 3 and body["source"] == "tencent_history"
        assert any("备用历史日线" in warning for warning in body["warnings"])
        assert any("缺少来源涨跌幅" in warning for warning in body["warnings"])
        assert not any("仅返回最新" in warning for warning in body["warnings"])
        assert "private-source-url" not in response.text and all("raw" not in row for row in body["quotes"])


def test_default_source_order_keeps_snapshot_as_last_resort():
    service = MarketService(MemoryRepository())
    assert [adapter.name for adapter in service.adapters] == ["eastmoney", "tencent_history", "tencent"]


def test_batch_falls_through_failed_primary_history_without_faking_date(monkeypatch):
    monkeypatch.setattr(EastmoneyAdapter, "fetch_daily", Mock(side_effect=MarketUnavailable()))
    fetch = Mock(side_effect=adapter_for().fetch_daily)
    monkeypatch.setattr(TencentHistoryAdapter, "fetch_daily", fetch)
    http = Mock()
    http.get.side_effect = MarketUnavailable()
    checkpoint = Mock()
    items = [{"code": "600519", "symbol": "sh600519", "name": "贵州茅台"}]
    target = date(2026, 11, 26)
    rows, failures = BatchQuotes(http=http).fetch(items, target, NOW, checkpoint)
    assert not failures and len(rows) == 1 and rows[0].date == target
    assert rows[0].source == "tencent_history" and rows[0].pe is None
    assert fetch.call_count == 1 and checkpoint.call_count == 3


def test_batch_name_mismatch_remains_missing(monkeypatch):
    monkeypatch.setattr(EastmoneyAdapter, "fetch_daily", Mock(side_effect=MarketUnavailable()))
    monkeypatch.setattr(TencentHistoryAdapter, "fetch_daily", Mock(side_effect=adapter_for().fetch_daily))
    http = Mock()
    http.get.side_effect = MarketUnavailable()
    rows, failures = BatchQuotes(http=http).fetch(
        [{"code": "600519", "symbol": "sh600519", "name": "其他公司"}], END, NOW,
    )
    assert not rows and "600519" in failures


def test_snapshot_still_succeeds_when_both_history_sources_fail():
    failed = Mock()
    failed.fetch_daily.side_effect = MarketUnavailable()
    result = MarketService(MemoryRepository(), [failed, failed, tencent()], now=lambda: NOW).refresh("600519", START, END)
    assert result["source"] == "tencent" and len(result["quotes"]) == 1


@pytest.mark.parametrize("stored_multiplier", [1, 100])
def test_legacy_star_volume_is_verified_on_read_without_rewriting_storage(stored_multiplier):
    collection = mongomock.MongoClient(tz_aware=False).test.quotes
    identity = payload("688981")["data"]["sh688981"]["qt"]["sh688981"]
    text = 'v_sh688981="' + "~".join(identity) + '";'
    quote = TencentAdapter().parse_snapshot(text, "688981", START, END, NOW)
    quote.volume *= stored_multiplier
    quote.raw.pop("volume_multiplier")
    repository = MarketQuoteRepository(collection)
    repository.save([quote])
    rows = repository.read("688981", START, END)
    assert rows[0]["volume"] == 39808 and "raw" not in rows[0]
    assert collection.find_one({})["volume"] == 39808 * stored_multiplier


@pytest.mark.parametrize("failure", ["missing_raw", "invalid_raw", "wrong_code", "wrong_price", "wrong_date", "wrong_volume"])
def test_unverifiable_legacy_star_volume_is_not_exposed_as_reliable(failure):
    collection = mongomock.MongoClient(tz_aware=True).test.quotes
    identity = payload("688981")["data"]["sh688981"]["qt"]["sh688981"]
    text = 'v_sh688981="' + "~".join(identity) + '";'
    quote = TencentAdapter().parse_snapshot(text, "688981", START, END, NOW)
    quote.raw.pop("volume_multiplier")
    if failure == "missing_raw":
        quote.raw = {}
    elif failure == "wrong_code":
        quote.raw["snapshot"] = quote.raw["snapshot"].replace("688981", "600519")
    elif failure == "wrong_price":
        quote.close = 1473
    elif failure == "wrong_date":
        quote.date = START
    elif failure == "wrong_volume":
        quote.volume = 123
    repository = MarketQuoteRepository(collection)
    repository.save([quote])
    if failure == "invalid_raw":
        collection.update_one({}, {"$set": {"raw": "invalid"}})
    assert repository.read("688981", START, END)[0]["volume"] is None


def test_other_source_volume_is_unchanged_and_raw_is_never_returned():
    collection = mongomock.MongoClient().test.quotes
    quote = adapter_for(payload("688981")).fetch_daily("688981", START, END, NOW)[-1]
    repository = MarketQuoteRepository(collection)
    repository.save([quote])
    rows = repository.read("688981", START, END)
    assert rows[0]["volume"] == quote.volume and "raw" not in rows[0]

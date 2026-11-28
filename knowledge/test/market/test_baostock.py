import copy
from datetime import timedelta
import json
import subprocess
from unittest.mock import Mock

import pytest

from knowledge.market.baostock_source import BaoStockAdapter, BaoStockGateway
from knowledge.market.baostock_worker import collect
from knowledge.market.models import MarketUnavailable
from knowledge.service.market_service import MarketService
from knowledge.test.market.test_market import END, NOW, START, MemoryRepository, tencent


def payload():
    return {"identity": {"code": "sh.600519", "code_name": "贵州茅台", "type": "1", "status": "1"}, "quotes": [
        {"date": "2026-11-25", "code": "sh.600519", "open": "1460", "close": "1465", "high": "1470", "low": "1450",
         "preclose": "1460", "volume": "3000035", "amount": "4395000000", "adjustflag": "3", "tradestatus": "1",
         "turn": "0.2401", "pctChg": "0.3425", "peTTM": "22.1", "pbMRQ": "6.7", "isST": "0"},
        {"date": "2026-11-27", "code": "sh.600519", "open": "1469.99", "close": "1472.36", "high": "1481.41", "low": "1452.99",
         "preclose": "1469", "volume": "3980835", "amount": "5849112431.39", "adjustflag": "3", "tradestatus": "1",
         "turn": "0.3184", "pctChg": "0.2287", "peTTM": "22.402062", "pbMRQ": "6.786644", "isST": "0"},
    ]}


def adapter(body=None, snapshot=None):
    gateway = Mock()
    gateway.request.return_value = payload() if body is None else body
    if snapshot is None:
        snapshot = Mock()
        snapshot.fetch_daily.side_effect = MarketUnavailable()
    return BaoStockAdapter(gateway, snapshot)


def test_source_units_and_returns_are_not_derived_or_ttm_mislabelled():
    rows = adapter().fetch_daily("600519", START, END, NOW)
    first, latest = rows
    assert latest.volume == 3980835 and latest.amount == 5849112431.39
    assert latest.change_pct == 0.2287 and latest.turnover == 0.3184
    assert latest.pe is None and latest.pb == 6.786644
    assert latest.valuation_source == "baostock" and latest.source_time is None
    assert first.pe is None and latest.raw["baostock"]["peTTM"] == "22.402062"


def test_same_day_snapshot_enriches_dynamic_pe_without_erasing_source_precision():
    first, latest = adapter(snapshot=tencent()).fetch_daily("600519", START, END, NOW)
    assert first.pe is None and latest.pe == 21.41 and latest.valuation_source == "tencent"
    assert latest.volume == 3980835 and latest.change_pct == 0.2287
    assert latest.raw["valuation_snapshot"]["snapshot"] and latest.source == "baostock"


@pytest.mark.parametrize("missing", [("pb",), ("pe", "pb", "market_cap")])
def test_missing_snapshot_valuations_preserve_source_pb_and_provenance(missing):
    snapshot = Mock()
    snapshot.fetch_daily.return_value = [tencent().fetch_daily("600519", START, END, NOW)[0].model_copy(
        update={field: None for field in missing})]
    latest = adapter(snapshot=snapshot).fetch_daily("600519", START, END, NOW)[-1]
    assert latest.pb == 6.786644
    if "pe" in missing:
        assert latest.pe is None and latest.market_cap is None
        assert latest.valuation_source == "baostock" and latest.source_time is None
        assert "valuation_snapshot" not in latest.raw
    else:
        assert latest.pe == 21.41 and latest.market_cap is not None
        assert latest.valuation_source == "baostock+tencent"
        assert latest.raw["valuation_sources"] == {"pb": "baostock", "pe": "tencent", "market_cap": "tencent"}


@pytest.mark.parametrize("mutation", ["date", "code", "adjustflag", "tradestatus", "volume", "close", "identity", "name", "type", "duplicate", "rows", "suspended"])
def test_invalid_source_cannot_partially_write_or_cache(mutation):
    body = payload()
    row = body["quotes"][-1]
    if mutation == "date":
        row["date"] = "2026-11-28"
    elif mutation == "code":
        row["code"] = "sz.600519"
    elif mutation == "adjustflag":
        row["adjustflag"] = "2"
    elif mutation == "tradestatus":
        row["tradestatus"] = "unknown"
    elif mutation == "volume":
        row["volume"] = "NaN"
    elif mutation == "close":
        row["close"] = "9999"
    elif mutation == "identity":
        body["identity"]["code"] = "sz.000001"
    elif mutation == "name":
        body["identity"]["code_name"] = ""
    elif mutation == "type":
        body["identity"]["type"] = "2"
    elif mutation == "duplicate":
        conflict = copy.deepcopy(row)
        conflict["pctChg"] = "8.88"
        body["quotes"].append(conflict)
    elif mutation == "rows":
        body["quotes"] = None
    else:
        row["tradestatus"] = "0"
    repository = MemoryRepository()
    service = MarketService(repository, [adapter(body)], now=lambda: NOW)
    with pytest.raises(MarketUnavailable):
        service.refresh("600519", START, END)
    assert repository.writes == 0 and not service.cache


def test_suspended_zero_volume_remains_zero_and_optional_values_remain_null():
    body = payload()
    body["quotes"][-1].update(tradestatus="0", volume="0", amount="0", turn="", peTTM="", pbMRQ="", pctChg="0")
    latest = adapter(body).fetch_daily("600519", START, END, NOW)[-1]
    assert latest.volume == 0 and latest.pe is None and latest.pb is None and latest.turnover is None
    assert latest.valuation_source is None


def test_beijing_rejected_before_sdk_and_future_date_before_network():
    source = adapter()
    with pytest.raises(MarketUnavailable):
        source.fetch_daily("920002", START, END, NOW)
    with pytest.raises(ValueError):
        source.fetch_daily("600519", START, END + timedelta(days=1), NOW)
    source.gateway.request.assert_not_called()


@pytest.mark.parametrize("changed", [{"code": "000001"}, {"name": "其他证券"}, {"close": 1473}, {"volume": 100}, {"amount": 100}, {"date": START}])
def test_snapshot_mismatch_cannot_inject_valuation(changed):
    snapshot = Mock()
    snapshot.fetch_daily.return_value = [tencent().fetch_daily("600519", START, END, NOW)[0].model_copy(update=changed)]
    latest = adapter(snapshot=snapshot).fetch_daily("600519", START, END, NOW)[-1]
    assert latest.pe is None and latest.valuation_source == "baostock"


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("worker", 20), OSError("private-url"),
                                     Mock(returncode=1, stdout="private-token"), Mock(returncode=0, stdout="not-json"),
                                     Mock(returncode=0, stdout='{"ok":false}'), Mock(returncode=0, stdout="[]")])
def test_gateway_bounded_failure_cooldown_and_recovery(failure):
    runner = Mock()
    if isinstance(failure, Exception):
        runner.side_effect = failure
    else:
        runner.return_value = failure
    moment = [0.0]
    gateway = BaoStockGateway(runner=runner, clock=lambda: moment[0], sleep=lambda seconds: None)
    for _ in range(3):
        with pytest.raises(MarketUnavailable) as error:
            gateway.request("daily", START, END, "600519")
        assert "private" not in str(error.value)
    assert runner.call_count == 1
    assert runner.call_args.kwargs["timeout"] == 20 and "shell" not in runner.call_args.kwargs
    runner.side_effect = None
    runner.return_value = Mock(returncode=0, stdout=json.dumps({"ok": True, "data": payload()}))
    moment[0] = 61
    assert gateway.request("daily", START, END, "600519")["identity"]["code"] == "sh.600519"
    assert runner.call_count == 2


def test_busy_gateway_does_not_launch_or_wait_unbounded():
    gateway = BaoStockGateway(runner=Mock())
    gateway.lock = Mock()
    gateway.lock.acquire.return_value = False
    with pytest.raises(MarketUnavailable):
        gateway.request("daily", START, END, "600519")
    gateway.lock.acquire.assert_called_once_with(timeout=2)
    gateway.runner.assert_not_called()


def test_worker_pagination_error_is_not_silently_partial():
    result = Mock(error_code="0", fields=["date"])
    result.get_row_data.return_value = ["2026-11-27"]
    calls = [True, False]
    def advance():
        value = calls.pop(0)
        if not value:
            result.error_code = "10002007"
        return value
    result.next.side_effect = advance
    with pytest.raises(ValueError):
        collect(result)


def test_worker_rejects_duplicate_fields_and_oversized_rows():
    result = Mock(error_code="0", fields=["code", "code"])
    with pytest.raises(ValueError):
        collect(result)
    result.fields = ["code"]
    result.next.return_value = True
    result.get_row_data.return_value = ["sh.600519"]
    with pytest.raises(ValueError):
        collect(result, maximum=1)


def test_primary_failure_uses_source_returns_in_risk_input():
    failed = Mock()
    failed.fetch_daily.side_effect = MarketUnavailable()
    result = MarketService(MemoryRepository(), [failed, adapter()], now=lambda: NOW).refresh("600519", START, END)
    assert result["source"] == "baostock" and result["quotes"][-1]["change_pct"] == 0.2287
    assert not any("缺少来源涨跌幅" in warning for warning in result["warnings"])
    assert any("动态 PE" in warning for warning in result["warnings"])

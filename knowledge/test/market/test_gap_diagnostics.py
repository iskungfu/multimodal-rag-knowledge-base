import json
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from knowledge.api.market_collection_api import get_collection_service, router
from knowledge.market.adapters import TencentAdapter
from knowledge.market.batch import BatchQuotes
from knowledge.market.models import MarketUnavailable, SnapshotUnavailable
from knowledge.test.market.test_collection import DAY, NOW, item, quote, run, service_fixture, snapshot


def change_field(text, index, value):
    prefix, body = text.split('="', 1)
    fields = body.removesuffix('";').split("~")
    fields[index] = value
    return prefix + '="' + "~".join(fields) + '";'


@pytest.mark.parametrize("text, reason", [
    (snapshot("000001", "平安银行", "20261126161500"), "date_mismatch"),
    (snapshot("000001", "平安银行", "20261127090000"), "not_closed"),
    (snapshot("000001", "平安银行", "20261127191000"), "future_timestamp"),
    (snapshot("000001", "平安银行").replace("v_sz", "v_sh"), "identity_mismatch"),
    (change_field(snapshot("000001", "平安银行"), 2, "600519"), "identity_mismatch"),
    (change_field(snapshot("000001", "平安银行"), 5, "0"), "invalid_prices"),
    (change_field(snapshot("000001", "平安银行"), 33, "-"), "invalid_prices"),
    (change_field(snapshot("000001", "平安银行"), 34, "-1"), "invalid_prices"),
    (change_field(snapshot("000001", "平安银行"), 30, "private-token"), "invalid_snapshot"),
    (change_field(snapshot("000001", "平安银行"), 6, "private-token"), "invalid_snapshot"),
])
def test_snapshot_rejections_have_safe_observed_reasons(text, reason):
    with pytest.raises(SnapshotUnavailable) as failure:
        TencentAdapter().parse_snapshot(text, "000001", DAY, DAY, NOW)
    assert failure.value.reason == reason
    assert str(failure.value) == SnapshotUnavailable.messages[reason]
    assert "private-token" not in str(failure.value)


@pytest.mark.parametrize("response, reason", [
    (b"", "missing_snapshot"),
    (b'v_pv_none_match="1";', "missing_snapshot"),
    (b"<html>private-token</html>", "invalid_response"),
    (b"\xff", "invalid_response"),
    ((snapshot("000001", "平安银行") * 2).encode("gbk"), "invalid_response"),
    (MarketUnavailable("private-token"), "source_unavailable"),
])
def test_batch_distinguishes_missing_response_from_source_and_format_failures(response, reason):
    http = Mock()
    if isinstance(response, Exception):
        http.get.side_effect = response
    else:
        http.get.return_value = response
    rows, failures = BatchQuotes(http).fetch_snapshots([item()], DAY, NOW)
    assert not rows
    assert failures == {"000001": SnapshotUnavailable.messages[reason]}
    assert "private-token" not in json.dumps(failures)


@pytest.mark.parametrize("directory_name, source_name", [
    ("华润新能", "华润新能源"),
    ("*ST三六", "*ST三六五"),
    ("粤芯半导-U", "粤芯半导体-U"),
    ("百联股份", "百联股份(已合并)"),
    ("平安银行", "*ST平安银行"),
])
def test_name_differences_remain_failures_until_matching_history_is_available(directory_name, source_name):
    http, fallback = Mock(), Mock()
    http.get.return_value = snapshot("000001", source_name).encode("gbk")
    fallback.fetch_daily.return_value = [quote(name=source_name)]
    batch = BatchQuotes(http, fallback)
    rows, failures = batch.fetch([item(name=directory_name)], DAY, NOW)
    assert not rows
    assert "腾讯快照：名称与目录不一致" in failures["000001"]
    assert "历史备用出现名称与目录不一致" in failures["000001"]
    fallback.fetch_daily.return_value = [quote(name=directory_name)]
    recovered, failures = batch.fetch([item(name=directory_name)], DAY, NOW)
    assert len(recovered) == 1 and recovered[0].name == directory_name
    assert not failures


def test_collecting_keeps_pending_until_history_finishes_and_exposes_safe_failure():
    service, database, _, batch = service_fixture()
    batch.fetch.return_value = ([], {"000001": SnapshotUnavailable.messages["not_closed"]})

    def unavailable_history(*args):
        progress = service.repository.get(DAY, NOW)
        assert progress["pending"] == 1 and progress["failed"] == 0 and progress["failures"] == []
        return [], {"000001": "历史备用未取得可核实的目标日行情"}

    batch.fetch_history.side_effect = unavailable_history
    result = run(service)
    assert result["failed"] == 1 and result["succeeded"] == result["pending"] == 0
    assert database.quotes.count_documents({}) == 0
    expected = "腾讯快照：时间早于15:00，不能作为收盘行情；历史备用未取得可核实的目标日行情"
    assert result["failures"] == [{"code": "000001", "reason": expected}]
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_collection_service] = lambda: service
    with TestClient(app) as client:
        response = client.get("/market/collection/2026-11-27")
        assert response.json()["record"]["failures"] == result["failures"]
        assert not any(key in response.json()["record"] for key in ("raw", "token", "universe"))


def test_retry_replaces_old_diagnostic_after_history_recovers():
    service, database, _, batch = service_fixture()
    batch.fetch.return_value = ([], {"000001": SnapshotUnavailable.messages["name_mismatch"]})
    batch.fetch_history.return_value = ([], {"000001": "历史备用未取得可核实的目标日行情"})
    assert run(service)["failed"] == 1
    batch.fetch_history.return_value = ([quote()], {})
    result = run(service, retry=True)
    assert result["status"] == "completed" and result["succeeded"] == 1
    assert not result["failures"] and result["failed"] == result["pending"] == 0
    assert database.quotes.count_documents({}) == 1


def test_post_write_existing_conflict_keeps_storage_reason():
    service, _, _, batch = service_fixture()
    batch.fetch.return_value = ([], {"000001": SnapshotUnavailable.messages["missing_snapshot"]})
    batch.fetch_history.return_value = ([quote()], {})
    service.quotes.save_missing = Mock(side_effect=[[], [quote(name="错误名称").model_dump(mode="json")]])
    result = run(service)
    assert result["failed"] == 1 and result["succeeded"] == 0
    assert result["failures"] == [{"code": "000001", "reason": "已有记录格式或身份不符，需通过单股刷新核对"}]


def test_unknown_reason_never_echoes_untrusted_text():
    assert str(SnapshotUnavailable("private-token")) == SnapshotUnavailable.messages["invalid_snapshot"]


@pytest.mark.parametrize("malformed", [
    'v_sh600519="private-token',
    'v_sh600519=private-token;',
    snapshot("600519", "贵州茅台").removesuffix(";"),
])
def test_mixed_response_keeps_malformed_symbol_reason_through_history(malformed):
    http, fallback = Mock(), Mock()
    http.get.return_value = (snapshot("000001", "平安银行") + "\n" + malformed).encode("gbk")
    fallback.fetch_daily.side_effect = MarketUnavailable("private-token")
    rows, failures = BatchQuotes(http, fallback).fetch([item(), item("600519", "贵州茅台")], DAY, NOW)
    assert [row.code for row in rows] == ["000001"]
    assert failures == {"600519": "腾讯快照：批量响应格式无效或证券重复；历史备用未取得可核实的目标日行情"}
    assert "private-token" not in str(failures)


def test_malformed_duplicate_cannot_hide_behind_valid_record():
    http = Mock()
    http.get.return_value = (snapshot("000001", "平安银行") + '\nv_sz000001="private-token').encode("gbk")
    rows, failures = BatchQuotes(http).fetch_snapshots([item()], DAY, NOW)
    assert not rows and failures == {"000001": SnapshotUnavailable.messages["invalid_response"]}

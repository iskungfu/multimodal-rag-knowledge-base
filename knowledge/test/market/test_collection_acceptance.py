from unittest.mock import Mock
import json
import os
import socket
import threading

import mongomock
import pytest

from knowledge.market.collection import CollectionStopped
from scripts.verify_market_collection import ObservedBatch, coverage_details, stop_worker
from scripts import verify_market_collection


def test_report_separates_source_exchange_and_missing_optional_fields():
    universe = [{"code": "600519", "name": "贵州茅台", "symbol": "sh600519"},
                {"code": "000001", "name": "平安银行", "symbol": "sz000001"},
                {"code": "920002", "name": "万达轴承", "symbol": "bj920002"}]
    rows = [{"code": "600519", "source": "baostock", "pe": None, "pb": 0, "amount": 0, "change_pct": 0, "turnover": 0},
            {"code": "000001", "source": "tencent", "pe": -1, "pb": 1, "amount": 10, "change_pct": -1, "turnover": None}]
    result = coverage_details(rows, universe, [{"code": "920002", "reason": "目标日未核实"}])
    assert result["source_counts"] == {"baostock": 1, "tencent": 1}
    assert result["exchange_coverage"]["bj"] == {"expected": 1, "saved": 0, "missing": 1}
    assert result["directory_coverage_pct"] == 66.6667
    assert result["missing_optional_fields"] == {"pe": 1, "pb": 0, "amount": 0, "change_pct": 0, "turnover": 1}
    assert result["missing"] == [{"code": "920002", "name": "万达轴承", "reason": "目标日未核实"}]
    assert coverage_details([], [], [])["directory_coverage_pct"] is None


def test_interruption_probe_records_only_dispatched_batches_and_can_resume():
    source = Mock()
    source.fetch_snapshots.return_value = ([], {})
    probe = ObservedBatch(source, interrupt_after=1)
    items = [{"code": "000001"}]
    assert probe.fetch_snapshots(items, None, None, None) == ([], {})
    with pytest.raises(CollectionStopped, match="验收主动中断"):
        probe.fetch_snapshots([{"code": "600519"}], None, None, None)
    assert probe.requested_codes == ["000001"] and source.fetch_snapshots.call_count == 1
    probe.interrupt_after = None
    probe.fetch_snapshots([{"code": "600519"}], None, None, None)
    assert probe.requested_codes == ["000001", "600519"] and source.fetch_snapshots.call_count == 2


def test_cleanup_handles_unstarted_or_unstoppable_workers():
    service = Mock(worker=threading.Thread())
    assert stop_worker(service) is True
    service.stop.set.assert_called_once_with()
    service.worker = Mock()
    service.worker.is_alive.return_value = True
    service.worker.join.side_effect = RuntimeError("worker error")
    assert stop_worker(service) is False
    assert stop_worker(None) is True


def test_deferred_cleanup_requires_stopped_process_and_preserves_other_data(monkeypatch, tmp_path):
    run_id = "market_collection_0123456789ab"
    directory = tmp_path / ".sandcastle/logs"
    directory.mkdir(parents=True)
    path = directory / (run_id + ".json")
    report = {"run_id": run_id, "test_database": "isolated", "pid": 12345, "host": socket.gethostname(),
              "passed": False, "cleanup": {"worker_stopped": False}}
    path.write_text(json.dumps(report), encoding="utf-8")
    client = mongomock.MongoClient()
    for suffix in ("_runs", "_locks", "_quotes"):
        client.isolated[run_id + suffix].insert_one({"test": True})
    client.isolated.keep.insert_one({"sentinel": True})
    monkeypatch.setattr(verify_market_collection, "ROOT", tmp_path)
    monkeypatch.setattr("pymongo.MongoClient", Mock(return_value=client))
    monkeypatch.setenv("MONGO_URL", "unused-mocked-connection")
    monkeypatch.setattr(verify_market_collection, "process_is_running", lambda pid: True)
    with pytest.raises(ValueError, match="仍在运行"):
        verify_market_collection.cleanup_run(run_id, "isolated")
    assert len(client.isolated.list_collection_names()) == 4
    monkeypatch.setattr(verify_market_collection, "process_is_running", lambda pid: False)
    with pytest.raises(ValueError, match="不匹配"):
        verify_market_collection.cleanup_run(run_id, "another_database")
    assert verify_market_collection.cleanup_run(run_id, "isolated") == 0
    assert client.isolated.list_collection_names() == ["keep"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["passed"] is False and all(saved["cleanup"].values())


def test_process_check_recognizes_own_process_and_rejects_invalid_pid():
    assert verify_market_collection.process_is_running(os.getpid()) is True
    with pytest.raises(ValueError):
        verify_market_collection.process_is_running(-1)

"""在独立测试库验证真实全市场收盘采集；只清理本次随机集合。"""

import argparse
from collections import Counter
import csv
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.acceptance_report import begin_report, finish_report
ENTRY_REPORT = begin_report({}) if __name__ == "__main__" else None


class ObservedBatch:
    def __init__(self, source, interrupt_after=None):
        self.source = source
        self.interrupt_after = interrupt_after
        self.calls = 0
        self.requested_codes = []

    def fetch_snapshots(self, items, day, now, checkpoint):
        from knowledge.market.collection import CollectionStopped
        if self.interrupt_after is not None and self.calls >= self.interrupt_after:
            raise CollectionStopped("验收主动中断，验证已保存行情与补采恢复")
        self.calls += 1
        self.requested_codes.extend(item["code"] for item in items)
        return self.source.fetch_snapshots(items, day, now, checkpoint)

    def fetch_history(self, items, day, now, checkpoint):
        self.requested_codes.extend(item["code"] for item in items)
        return self.source.fetch_history(items, day, now, checkpoint)


def stop_worker(service):
    if service is None or service.worker is None:
        return True
    try:
        service.stop.set()
        if service.worker.is_alive():
            service.worker.join(timeout=45)
        return not service.worker.is_alive()
    except Exception:
        return False


def process_is_running(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError("无有效进程标识，拒绝清理")
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        rows = list(csv.reader(result.stdout.splitlines()))
        if result.returncode or not rows or any(len(row) < 2 or not row[1].isdigit() for row in rows):
            raise ValueError("无法核实进程状态，拒绝清理")
        return any(int(row[1]) == pid for row in rows)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cleanup_run(run_id, test_db):
    from pymongo import MongoClient
    if not re.fullmatch(r"market_collection_[0-9a-f]{12}", run_id):
        raise ValueError("无效验收编号")
    path = ROOT / ".sandcastle/logs" / (run_id + ".json")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("run_id") != run_id or report.get("test_database") != test_db or report.get("host") != socket.gethostname():
        raise ValueError("报告与本机独立测试库不匹配，拒绝清理")
    if process_is_running(report.get("pid")):
        raise ValueError("原验收进程仍在运行或PID已复用，拒绝清理")
    with MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000,
                     connectTimeoutMS=5000, socketTimeoutMS=5000) as client:
        database = client[test_db]
        report["cleanup"]["worker_stopped"] = True
        for suffix in ("_runs", "_locks", "_quotes"):
            name = run_id + suffix
            try:
                database[name].drop()
                report["cleanup"][name] = name not in database.list_collection_names()
            except Exception:
                report["cleanup"][name] = False
    report["cleanup_retried_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    cleaned = all(report["cleanup"].values())
    print("cleanup", "PASS" if cleaned else "FAIL", flush=True)
    return 0 if cleaned else 1


def coverage_details(rows, universe, failures):
    expected = {item["code"]: item for item in universe}
    saved = {row["code"] for row in rows if row["code"] in expected}
    exchanges = {}
    for prefix in ("sh", "sz", "bj"):
        codes = {code for code, item in expected.items() if item["symbol"].startswith(prefix)}
        exchanges[prefix] = {"expected": len(codes), "saved": len(codes & saved), "missing": len(codes - saved)}
    return {
        "source_counts": dict(sorted(Counter(row.get("source", "unknown") for row in rows).items())),
        "exchange_coverage": exchanges,
        "directory_coverage_pct": round(len(saved) / len(expected) * 100, 4) if expected else None,
        "missing_optional_fields": {field: sum(row.get(field) is None for row in rows)
                                    for field in ("pe", "pb", "turnover", "change_pct", "amount")},
        "failure_reasons": dict(sorted(Counter(item["reason"] for item in failures).items())),
        "missing": [{"code": item["code"], "name": expected.get(item["code"], {}).get("name"), "reason": item["reason"]}
                    for item in failures],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="首批保存后主动中断，再用同一真实目录流程补采")
    mode.add_argument("--cleanup-run", help="核实原进程已退出后，只清理给定报告的随机测试集合")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / "knowledge/.env")
    test_db = os.getenv("AFK_TEST_DB_NAME")
    if not test_db or test_db in {os.getenv("MONGO_DB_NAME"), "kb001", "admin", "config", "local"}:
        print("必须指定独立 AFK_TEST_DB_NAME")
        return 1
    if args.cleanup_run:
        try:
            return cleanup_run(args.cleanup_run, test_db)
        except Exception as error:
            print("清理未完成：" + type(error).__name__)
            return 1
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pymongo import MongoClient
    from knowledge.api.market_collection_api import get_collection_service, router
    from knowledge.market.adapters import TencentAdapter
    from knowledge.market.batch import BatchQuotes
    from knowledge.market.collection import CollectionRepository, MarketCollectionService
    from knowledge.market.models import date_window
    from knowledge.market.repository import MarketQuoteRepository

    logging.disable(logging.CRITICAL)
    run_id = "market_collection_" + uuid4().hex[:12]
    names = [run_id + suffix for suffix in ("_runs", "_locks", "_quotes")]
    report = {"run_id": run_id, "checks": {}, "cleanup": {}, "passed": False,
              "pid": os.getpid(), "host": socket.gethostname(), "test_database": test_db,
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "injected_faults": ["interrupt_after_first_batch"] if args.resume else []}
    begin_report(report, entry_report=ENTRY_REPORT)
    started = time.monotonic()
    mongo = None
    service = None

    def check(name, passed):
        report["checks"][name] = bool(passed)
        print(name, "PASS" if passed else "FAIL", flush=True)
        if not passed:
            raise AssertionError(name)

    try:
        mongo = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000,
                            connectTimeoutMS=5000, socketTimeoutMS=5000, tz_aware=True)
        db = mongo[test_db]
        check("isolated_collections", not any(name in db.list_collection_names() for name in names))
        now = datetime.now(timezone.utc)
        start, end = date_window(None, None, now)
        anchor = TencentAdapter().fetch_daily("600519", start, end, now)[0]
        day = anchor.date
        report["date"] = day.isoformat()
        batch = ObservedBatch(BatchQuotes(), interrupt_after=1 if args.resume else None)
        service = MarketCollectionService(CollectionRepository(db[names[0]], db[names[1]]), MarketQuoteRepository(db[names[2]]), batch=batch)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_collection_service] = lambda: service
        with TestClient(app) as client:
            def wait_for_run():
                while service.worker.is_alive():
                    service.worker.join(timeout=10)
                    current = client.get("/market/collection/latest").json()["record"]
                    print("progress", current["status"], current.get("succeeded", 0), "/", current.get("total", 0), flush=True)
                return client.get("/market/collection/" + day.isoformat()).json()["record"]

            response = client.post("/market/collection/run", json={"date": day.isoformat()})
            check("api_accepted", response.status_code == 202 and response.json()["started"])
            check("concurrent_rejected", client.post("/market/collection/run", json={"date": day.isoformat()}).status_code == 409)
            result = wait_for_run()
            if args.resume:
                report["interrupted"] = result
                before = {row["code"]: row for row in db[names[2]].find({}, {"_id": 0})}
                check("controlled_interruption", result["status"] == "failed" and "验收主动中断" in result["error"])
                check("interruption_preserves_saved", len(before) == result["succeeded"] > 0)
                check("interruption_counts_remaining", result["pending"] == 0 and result["succeeded"] + result["failed"] == result["total"])
                request_offset = len(batch.requested_codes)
                batch.interrupt_after = None
                response = client.post("/market/collection/run", json={"date": day.isoformat(), "retry": True})
                check("resume_accepted", response.status_code == 202 and response.json()["started"])
                result = wait_for_run()
                requested = batch.requested_codes[request_offset:]
                check("resume_skips_saved", not set(before).intersection(requested))
                after = {row["code"]: row for row in db[names[2]].find({"code": {"$in": list(before)}}, {"_id": 0})}
                check("resume_preserves_saved_values", before == after)
                check("resume_attempts_recorded", result["attempts"] == 2)
                report["resume"] = {"preserved_count": len(before), "requested_count": len(requested),
                                    "additional_saved": result["succeeded"] - len(before)}
            report["coverage"] = result
            stored_rows = list(db[names[2]].find({}, {"_id": 0, "raw": 0}))
            universe = db[names[0]].find_one({"date": day.isoformat()}).get("universe", [])
            report["quality"] = coverage_details(stored_rows, universe, result.get("failures", []))
            check("real_universe_loaded", result["total"] >= 1000)
            check("quotes_saved", result["succeeded"] > 1000)
            check("counts_match_storage", result["succeeded"] == db[names[2]].count_documents({"date": day.isoformat()}))
            check("no_wrong_dates", db[names[2]].count_documents({"date": {"$ne": day.isoformat()}}) == 0)
            check("no_duplicates", db[names[2]].count_documents({}) == len(db[names[2]].distinct("code")))
            check("coverage_status_honest", result["status"] == ("completed" if result["succeeded"] == result["total"] and result["universe_complete"] else "partial"))
            check("missing_count_honest", result["total"] == result["succeeded"] + result["failed"])
            check("lease_released", db[names[1]].count_documents({}) == 0)
            report["exchange_counts"] = {prefix: db[names[2]].count_documents({"code": {"$regex": prefix}}) for prefix in ("^6", "^[03]", "^920")}
            check("all_three_exchanges", all(report["exchange_counts"].values()))
            report["samples"] = list(db[names[2]].find({"code": {"$in": ["600519", "000001", "920002"]}}, {"_id": 0}))
            check("anchor_consistency", db[names[2]].find_one({"code": "600519"})["close"] == anchor.close)
        report["passed"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        print("验收未通过：" + type(exc).__name__, flush=True)
    finally:
        worker_stopped = stop_worker(service)
        report["cleanup"]["worker_stopped"] = worker_stopped
        if mongo is not None:
            for name in names:
                try:
                    if not worker_stopped:
                        report["cleanup"][name] = False
                        continue
                    mongo[test_db][name].drop()
                    report["cleanup"][name] = name not in mongo[test_db].list_collection_names()
                except Exception:
                    report["cleanup"][name] = False
            mongo.close()
        report["seconds"] = round(time.monotonic() - started, 2)
        report["passed"] = report["passed"] and bool(report["cleanup"]) and all(report["cleanup"].values())
        path = ROOT / ".sandcastle/logs" / (run_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        finish_report(report)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("report", str(path), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

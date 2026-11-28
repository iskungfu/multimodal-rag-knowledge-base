"""真实 MongoDB/子进程恢复验收；时钟和行情为固定样本，不代表实际跨日观察。"""

import json
import logging
import multiprocessing
import os
from pathlib import Path
import sys
import threading
from datetime import date, datetime, timedelta
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.acceptance_report import begin_report, finish_report
ENTRY_REPORT = begin_report({}) if __name__ == "__main__" else None

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo import MongoClient

from knowledge.api.market_collection_api import get_collection_service, router as collection_router
from knowledge.api.summary_api import get_summary_service, router as summary_router
from knowledge.market.collection import CollectionRepository, MarketCollectionService
from knowledge.market.models import DailyQuote, SHANGHAI, market_symbol
from knowledge.market.repository import MarketQuoteRepository
from knowledge.runtime import RuntimeConfig
from knowledge.service.summary_service import SummaryService
from knowledge.summary.indices import INDICES, IndexQuote, IndexRepository, IndexService
from knowledge.summary.repository import SummaryRepository, WatchlistRepository
from knowledge.summary.scheduler import build_scheduler
from knowledge.summary.strategies import PersonalStrategyRetriever

DAY = date(2026, 11, 26)
NEXT_DAY = date(2026, 11, 27)
NOW = datetime(2026, 11, 26, 18, tzinfo=SHANGHAI)


def connect():
    return MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000,
                       connectTimeoutMS=5000, socketTimeoutMS=5000, tz_aware=True)


class SampleIndices:
    name = "recovery_fixture"

    def fetch_daily(self, symbol, start, end, now):
        return [IndexQuote(symbol=symbol, name=INDICES[symbol][1], date=end, close=3200,
                           change_pct=0.1, volume=1000, amount=3200000, source=self.name,
                           updated_at=now, raw={"fixture": True})]


class SampleUniverse:
    def fetch(self, checkpoint):
        checkpoint()
        return {"items": [{"code": f"600{number:03d}", "name": f"验收样本{number}",
                            "symbol": market_symbol(f"600{number:03d}")} for number in range(51)],
                "source": "recovery_fixture", "complete": True}


class SampleBatch:
    def __init__(self, blocked=None):
        self.blocked = blocked
        self.calls = 0
        self.requested = []

    def fetch_snapshots(self, items, day, now, checkpoint):
        self.calls += 1
        if self.blocked is not None and self.calls == 2:
            self.blocked.send({"ready": True})
            # 父进程只结束自己创建的验收子进程，模拟保存首批后的真实崩溃。
            threading.Event().wait()
        checkpoint()
        self.requested.extend(item["code"] for item in items)
        return ([DailyQuote(code=item["code"], name=item["name"], date=day, close=12,
                            open=11, high=13, low=10, volume=10000, amount=120000,
                            source="recovery_fixture", updated_at=now, raw={"fixture": True})
                 for item in items], {})

    def fetch_history(self, items, day, now, checkpoint):
        raise AssertionError("固定行情完整，不应请求历史备用")


def summary_service(database, names, now):
    return SummaryService(SummaryRepository(database[names["summaries"]]),
                          WatchlistRepository(database[names["watches"]]),
                          IndexService(IndexRepository(database[names["indices"]]), [SampleIndices()]),
                          None, PersonalStrategyRetriever(database[names["documents"]]), now=now)


def collection_service(database, names, now, batch):
    return MarketCollectionService(CollectionRepository(database[names["runs"]], database[names["locks"]]),
                                   MarketQuoteRepository(database[names["stocks"]]),
                                   now=now, universe=SampleUniverse(), batch=batch)


def child_claims(database_name, names, signal):
    """在另一进程留下摘要处理中和已保存首批行情，等待父进程故障注入。"""
    logging.disable(logging.CRITICAL)
    try:
        with connect() as mongo:
            database = mongo[database_name]
            summary = summary_service(database, names, lambda: NOW)
            claim, _ = summary.begin(DAY, retry=True)
            if not claim:
                raise AssertionError("summary_claim_missing")
            service = collection_service(database, names, lambda: NOW, SampleBatch(signal))
            service.start(DAY)
            service.worker.join()
    except Exception as error:
        signal.send({"ready": False, "error_type": type(error).__name__})
    finally:
        signal.close()


def api(summary, collection):
    app = FastAPI()
    app.include_router(summary_router)
    app.include_router(collection_router)
    app.dependency_overrides[get_summary_service] = lambda: summary
    app.dependency_overrides[get_collection_service] = lambda: collection
    return TestClient(app)


def main():
    load_dotenv(ROOT / "knowledge/.env")
    logging.disable(logging.CRITICAL)
    run_id = "scheduling_recovery_" + uuid4().hex[:12]
    report = {"run_id": run_id, "checked_at": datetime.now(SHANGHAI).isoformat(),
              "scope": "real_mongo_process_restart_synthetic_market_and_clock",
              "actual_trading_day_observation": False, "checks": {}, "cleanup": {}, "passed": False}
    begin_report(report, entry_report=ENTRY_REPORT)
    mongo = child = collection = scheduler = None
    owned = []

    def check(name, condition):
        report["checks"][name] = bool(condition)
        print(name, "PASS" if condition else "FAIL", flush=True)
        if not condition:
            raise AssertionError(name)

    try:
        config = RuntimeConfig.acceptance("investment", run_id)
        names = config.mongo_names
        report.update(database=config.database, collections=list(names.values()))
        mongo = connect()
        database = mongo[config.database]
        check("isolated_unused_collections", not set(names.values()).intersection(database.list_collection_names()))
        owned = list(names.values())
        current = [NOW]
        summary = summary_service(database, names, lambda: current[0])
        collection = collection_service(database, names, lambda: current[0], SampleBatch())
        with api(summary, collection) as client:
            response = client.post("/daily-summaries/generate", json={"date": DAY.isoformat()})
            original = client.get("/daily-summaries/" + DAY.isoformat()).json()
            check("previous_summary_published", response.status_code == 202 and original["status"] == "completed")

        context = multiprocessing.get_context("spawn")
        reader, writer = context.Pipe(duplex=False)
        child = context.Process(target=child_claims, args=(config.database, names, writer))
        child.start()
        writer.close()
        try:
            check("child_saved_first_batch", reader.poll(30) and reader.recv().get("ready") is True)
        finally:
            reader.close()
        child.terminate()
        child.join(timeout=10)
        check("test_process_terminated", not child.is_alive() and child.exitcode != 0)

        summary = summary_service(database, names, lambda: current[0])
        batch = SampleBatch()
        collection = collection_service(database, names, lambda: current[0], batch)
        with api(summary, collection) as client:
            response = client.post("/daily-summaries/generate", json={"date": DAY.isoformat()})
            check("live_summary_lease_blocks_duplicate", response.status_code == 202 and not response.json()["started"])
            check("live_collection_lease_blocks_duplicate", client.post("/market/collection/run", json={"date": DAY.isoformat()}).status_code == 409)
            before = {f"600{number:03d}": collection.quotes.read(f"600{number:03d}", DAY, DAY) for number in range(50)}
            check("saved_fifty_quotes_survive_process_exit", all(len(rows) == 1 for rows in before.values()))
            current[0] = NOW + timedelta(seconds=301)
            expired = client.get("/daily-summaries/" + DAY.isoformat()).json()
            check("expired_summary_reports_reason", expired["status"] == "failed" and expired["error_code"] == "lease_expired")
            check("expired_summary_retains_previous_result", expired["result"] == original["result"] and expired["generated_at"] == original["generated_at"])
            interrupted = client.get("/market/collection/" + DAY.isoformat()).json()["record"]
            check("expired_collection_reports_one_missing", interrupted["status"] == "failed" and interrupted["succeeded"] == 50 and interrupted["failed"] == 1)
            response = client.post("/daily-summaries/generate", json={"date": DAY.isoformat()})
            recovered = client.get("/daily-summaries/" + DAY.isoformat()).json()
            check("summary_retry_completes", response.status_code == 202 and response.json()["started"] and recovered["status"] == "completed" and recovered["attempts"] == 3 and recovered["error_code"] is None)
            response = client.post("/market/collection/run", json={"date": DAY.isoformat()})
            check("collection_retry_accepted", response.status_code == 202 and response.json()["started"])
            collection.worker.join(timeout=10)
            check("collection_worker_finished", not collection.worker.is_alive())
            recovered = client.get("/market/collection/" + DAY.isoformat()).json()["record"]
            check("collection_recovers_only_missing", recovered["status"] == "completed" and recovered["succeeded"] == 51 and recovered["attempts"] == 2 and batch.requested == ["600050"])
            check("existing_quote_values_preserved", before == {code: collection.quotes.read(code, DAY, DAY) for code in before})
            check("completed_collection_deduplicated", not client.post("/market/collection/run", json={"date": DAY.isoformat()}).json()["started"])
            check("completed_summary_deduplicated", not client.post("/daily-summaries/generate", json={"date": DAY.isoformat()}).json()["started"])
            current[0] = datetime(2026, 11, 27, 18, tzinfo=SHANGHAI)
            check("next_date_collection_starts", client.post("/market/collection/run", json={"date": NEXT_DAY.isoformat()}).json()["started"])
            collection.worker.join(timeout=10)
            check("next_date_collection_completes", not collection.worker.is_alive() and client.get("/market/collection/" + NEXT_DAY.isoformat()).json()["record"]["succeeded"] == 51)

            # 同一真实调度 executor 连续触发两次，结果须经实际 API 可读且只发布一次。
            class OpenCalendar:
                def status(self):
                    return {"is_trading_day": True}

            scheduler = build_scheduler(lambda: summary, calendar_factory=OpenCalendar)
            job = scheduler.get_job("daily-summary")
            next_fire = job.trigger.get_next_fire_time(None, datetime(2026, 11, 27, 15, 31, tzinfo=SHANGHAI))
            check("cron_skips_weekend_in_shanghai", next_fire == datetime(2026, 11, 30, 15, 30, tzinfo=SHANGHAI))
            action = job.func
            scheduler.remove_all_jobs()
            from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
            finished = threading.Event()
            events = []

            def observed(event):
                events.append(event.code)
                finished.set()

            scheduler.add_listener(observed, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
            scheduler.start()
            for attempt in range(2):
                finished.clear()
                scheduler.add_job(action, "date", run_date=datetime.now(SHANGHAI), id="acceptance-" + str(attempt))
                check("executor_finished_" + str(attempt), finished.wait(15) and events[-1] == EVENT_JOB_EXECUTED)
            next_result = client.get("/daily-summaries/" + NEXT_DAY.isoformat()).json()
            check("next_date_summary_once", next_result["status"] == "completed" and next_result["attempts"] == 1 and next_result["date"] == NEXT_DAY.isoformat())
            history = client.get("/daily-summaries").json()["items"]
            check("both_summary_dates_preserved", [row["date"] for row in history] == ["2026-11-27", "2026-11-26"])
            check("both_quote_dates_preserved", len(collection.quotes.read("600000", DAY, NEXT_DAY)) == 2)
        report["passed"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        print("验收未通过：" + type(error).__name__, flush=True)
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=True)
        if child is not None and child.pid is not None:
            if child.is_alive():
                child.terminate()
            child.join(timeout=10)
        stopped = child is None or child.pid is None or not child.is_alive()
        if collection is not None and collection.worker is not None:
            collection.stop.set()
            collection.worker.join(timeout=10)
            stopped = stopped and not collection.worker.is_alive()
        report["cleanup"]["workers_stopped"] = stopped
        if mongo is not None:
            for name in owned:
                try:
                    if not stopped:
                        report["cleanup"][name] = False
                        continue
                    mongo[config.database][name].drop()
                    report["cleanup"][name] = not mongo[config.database].list_collection_names(filter={"name": name})
                except Exception:
                    report["cleanup"][name] = False
            mongo.close()
        report["passed"] = report["passed"] and all(report["cleanup"].values())
        output = ROOT / ".sandcastle/logs" / (run_id + ".json")
        output.parent.mkdir(parents=True, exist_ok=True)
        finish_report(report)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("report", output, flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

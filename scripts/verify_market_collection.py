"""在独立测试库验证真实全市场收盘采集；只清理本次随机集合。"""

import json
import logging
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / "knowledge/.env")
    test_db = os.getenv("AFK_TEST_DB_NAME")
    if not test_db or test_db in {os.getenv("MONGO_DB_NAME"), "kb001", "admin", "config", "local"}:
        print("必须指定独立 AFK_TEST_DB_NAME")
        return 1
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pymongo import MongoClient
    from knowledge.api.market_collection_api import get_collection_service, router
    from knowledge.market.adapters import TencentAdapter
    from knowledge.market.collection import CollectionRepository, MarketCollectionService
    from knowledge.market.models import SHANGHAI
    from knowledge.market.repository import MarketQuoteRepository
    from knowledge.market.universe import MarketUniverse

    logging.disable(logging.CRITICAL)
    run_id = "market_collection_" + uuid4().hex[:12]
    names = [run_id + suffix for suffix in ("_runs", "_locks", "_quotes")]
    report = {"run_id": run_id, "checks": {}, "cleanup": {}, "passed": False}
    started = time.monotonic()
    mongo = None

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
        end = now.astimezone(SHANGHAI).date()
        anchor = TencentAdapter().fetch_daily("600519", end - timedelta(days=10), end, now)[0]
        day = anchor.date
        report["date"] = day.isoformat()
        service = MarketCollectionService(CollectionRepository(db[names[0]], db[names[1]]), MarketQuoteRepository(db[names[2]]))
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_collection_service] = lambda: service
        with TestClient(app) as client:
            response = client.post("/market/collection/run", json={"date": day.isoformat()})
            check("api_accepted", response.status_code == 202 and response.json()["started"])
            check("concurrent_rejected", client.post("/market/collection/run", json={"date": day.isoformat()}).status_code == 409)
            while service.worker.is_alive():
                service.worker.join(timeout=10)
                current = client.get("/market/collection/latest").json()["record"]
                print("progress", current["status"], current.get("succeeded", 0), "/", current.get("total", 0), flush=True)
            result = client.get("/market/collection/" + day.isoformat()).json()["record"]
            report["coverage"] = result
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
        if mongo is not None:
            for name in names:
                try:
                    mongo[test_db][name].drop()
                    report["cleanup"][name] = name not in mongo[test_db].list_collection_names()
                except Exception:
                    report["cleanup"][name] = False
            mongo.close()
        report["seconds"] = round(time.monotonic() - started, 2)
        report["passed"] = report["passed"] and bool(report["cleanup"]) and all(report["cleanup"].values())
        path = ROOT / ".sandcastle/logs" / (run_id + ".json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("report", str(path), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

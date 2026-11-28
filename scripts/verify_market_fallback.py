"""实测免费历史日线；--with-storage 仅写入独立测试库的随机集合。"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class UnavailableSource:
    name = "injected_unavailable"

    def fetch_daily(self, *args):
        from knowledge.market.models import MarketUnavailable
        raise MarketUnavailable("验收主动模拟来源故障")

    def get(self, *args):
        from knowledge.market.models import MarketUnavailable
        raise MarketUnavailable("验收主动模拟来源故障")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-storage", action="store_true")
    args = parser.parse_args()
    from knowledge.market.adapters import EastmoneyAdapter, TencentAdapter
    from knowledge.market.batch import BatchQuotes
    from knowledge.market.models import MarketUnavailable, date_window, market_symbol
    from knowledge.market.tencent_history import TencentHistoryAdapter

    logging.disable(logging.CRITICAL)
    now = datetime.now(timezone.utc)
    start, end = date_window(None, None, now)
    run_id = "market_fallback_" + uuid4().hex[:12]
    report = {"run_id": run_id, "checked_at": now.isoformat(), "checks": {}, "samples": [], "cleanup": {}, "passed": False,
              "scope": "five-stock-sample", "faults_injected": ["batch_snapshot", "primary_history_for_api_and_batch"]}
    started = time.monotonic()
    mongo = None
    collection = None

    def check(name, passed):
        report["checks"][name] = bool(passed)
        print(name, "PASS" if passed else "FAIL", flush=True)
        if not passed:
            raise AssertionError(name)

    try:
        histories = TencentHistoryAdapter()
        snapshots = TencentAdapter()
        primary = EastmoneyAdapter()
        samples = {}
        for code in ("600519", "000001", "688981", "920002", "302132"):
            rows = histories.fetch_daily(code, start, end, now)
            snapshot = snapshots.fetch_daily(code, start, end, now)[0]
            latest = rows[-1]
            check("history_" + code, len(rows) >= 15 and latest.date == snapshot.date)
            check("units_" + code, all(getattr(latest, field) == getattr(snapshot, field)
                                      for field in ("open", "close", "high", "low", "volume", "turnover")))
            check("amount_" + code, latest.amount is not None and abs(latest.amount - snapshot.amount) <= 100)
            check("mean_price_" + code, latest.low <= latest.amount / latest.volume <= latest.high)
            check("historical_nulls_" + code, all(row.pe is None and row.change_pct is None for row in rows[:-1]))
            sample = {"code": code, "rows": len(rows), "first_date": rows[0].date, "latest": latest.model_dump(mode="json", exclude={"raw"})}
            report["samples"].append(sample)
            try:
                comparison = primary.fetch_daily(code, latest.date, latest.date, now)[0]
                sample["eastmoney_comparison"] = {field: getattr(comparison, field) for field in ("close", "volume", "amount", "turnover")}
                sample["volume_difference_shares"] = comparison.volume - latest.volume
                tolerance = 100 if code.startswith(("688", "689")) else 0
                check("cross_source_" + code, comparison.close == latest.close and abs(comparison.volume - latest.volume) <= tolerance)
            except MarketUnavailable:
                sample["eastmoney_comparison"] = "unavailable"
            samples[code] = rows

        batch = BatchQuotes(http=UnavailableSource())
        batch.fallbacks = [UnavailableSource(), histories]
        target = samples["600519"][-2].date
        items = [{"code": code, "name": rows[-1].name, "symbol": market_symbol(code)} for code, rows in samples.items()]
        recovered, missing = batch.fetch(items, target, now)
        report["batch"] = {"target": target, "requested": items, "recovered": [row.code for row in recovered], "missing": missing}
        check("batch_historical_fallback", len(recovered) == 5 and not missing and all(row.date == target for row in recovered))
        report["batch_target"] = target

        if args.with_storage:
            from dotenv import load_dotenv
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from pymongo import MongoClient
            from knowledge.api.market_api import get_market_service, router
            from knowledge.market.repository import MarketQuoteRepository
            from knowledge.service.market_service import MarketService

            load_dotenv(ROOT / "knowledge/.env")
            test_db = os.getenv("AFK_TEST_DB_NAME")
            check("isolated_database", bool(test_db) and test_db not in {os.getenv("MONGO_DB_NAME"), "kb001", "admin", "config", "local"})
            mongo = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000,
                                connectTimeoutMS=5000, socketTimeoutMS=5000, tz_aware=True)
            database = mongo[test_db]
            check("new_collection", run_id not in database.list_collection_names())
            collection = database[run_id]
            service = MarketService(MarketQuoteRepository(collection), [UnavailableSource(), histories, snapshots], now=lambda: now)
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_market_service] = lambda: service
            with TestClient(app) as client:
                for code, expected in samples.items():
                    query = f"/market/quotes/{code}/refresh?start={start}&end={end}"
                    response = client.post(query)
                    body = response.json()
                    check("api_" + code, response.status_code == 200 and body["source"] == "tencent_history")
                    check("stored_" + code, collection.count_documents({"code": code}) == len(expected) == len(body["quotes"]))
                    check("provenance_" + code, all(row["source"] == "tencent_history" and "raw" not in row for row in body["quotes"]))
                    check("cached_" + code, client.post(query).json()["cached"] is True)
                    check("read_" + code, client.get(f"/market/quotes/{code}?start={start}&end={end}").json()["quotes"] == body["quotes"])
                    check("warnings_" + code, any("缺少来源涨跌幅" in warning for warning in body["warnings"]))
                legacy = snapshots.fetch_daily("688981", start, end, now)[0]
                expected_volume = legacy.volume
                legacy.volume *= 100
                legacy.raw.pop("volume_multiplier")
                legacy_repository = MarketQuoteRepository(collection)
                legacy_repository.save([legacy])
                key = {"code": "688981", "date": legacy.date.isoformat()}
                collection.update_one(key, {"$unset": {"raw.volume_multiplier": ""}})
                check("legacy_volume_read", legacy_repository.read("688981", legacy.date, legacy.date)[0]["volume"] == expected_volume)
                check("legacy_storage_unchanged", collection.find_one(key)["volume"] == expected_volume * 100)
                collection.update_one(key, {"$unset": {"raw.snapshot": ""}})
                check("legacy_unverified_volume_missing", legacy_repository.read("688981", legacy.date, legacy.date)[0]["volume"] is None)
        report["passed"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        print("验收未通过：" + type(error).__name__, flush=True)
    finally:
        if collection is not None:
            try:
                collection.drop()
                report["cleanup"][run_id] = run_id not in collection.database.list_collection_names()
            except Exception:
                report["cleanup"][run_id] = False
        if mongo is not None:
            mongo.close()
        report["passed"] = report["passed"] and all(report["cleanup"].values())
        report["seconds"] = round(time.monotonic() - started, 2)
        directory = ROOT / ".sandcastle/logs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (run_id + ".json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("report", str(path), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

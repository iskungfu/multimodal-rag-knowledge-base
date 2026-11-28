"""真实BaoStock/腾讯、日历及故障切换验收，只写独立测试库的随机集合。"""

from datetime import date, datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.acceptance_report import begin_report, finish_report
ENTRY_REPORT = begin_report({}) if __name__ == "__main__" else None


class FailedPrimary:
    name = "injected_primary_failure"

    def fetch_daily(self, *args):
        from knowledge.market.models import MarketUnavailable
        raise MarketUnavailable("验收模拟主源故障")

    def get(self, *args):
        from knowledge.market.models import MarketUnavailable
        raise MarketUnavailable("验收模拟批量快照故障")


def main():
    from dotenv import load_dotenv
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pymongo import MongoClient
    from knowledge.api.market_api import router as market_router, get_market_service
    from knowledge.api.market_calendar_api import router as calendar_router
    from knowledge.market.adapters import TencentAdapter
    from knowledge.market.baostock_source import BaoStockAdapter
    from knowledge.market.batch import BatchQuotes
    from knowledge.market.calendar import TradingCalendar, get_trading_calendar
    from knowledge.market.models import MarketUnavailable, date_window, market_symbol
    from knowledge.market.repository import MarketQuoteRepository
    from knowledge.risk.rules import evaluate
    from knowledge.service.market_service import MarketService
    from knowledge.summary.scheduler import build_scheduler

    load_dotenv(ROOT / "knowledge/.env")
    test_db = os.getenv("AFK_TEST_DB_NAME")
    if not test_db or test_db in {os.getenv("MONGO_DB_NAME"), "kb001", "admin", "config", "local"}:
        print("必须指定独立 AFK_TEST_DB_NAME")
        return 1
    logging.disable(logging.CRITICAL)
    now = datetime.now(timezone.utc)
    start, end = date_window(None, None, now)
    run_id = "baostock_accept_" + uuid4().hex[:12]
    report = {"run_id": run_id, "checks": {}, "samples": [], "calendar": [], "cleanup": {}, "passed": False,
              "checked_at": now.isoformat(), "scope": "four-shenzhen-shanghai-stocks-and-calendar",
              "injected_faults": ["primary_daily", "batch_snapshot"]}
    begin_report(report, entry_report=ENTRY_REPORT)
    mongo = None
    collection = None
    started = time.monotonic()

    def check(name, passed):
        report["checks"][name] = bool(passed)
        print(name, "PASS" if passed else "FAIL", flush=True)
        if not passed:
            raise AssertionError(name)

    try:
        source = BaoStockAdapter()
        snapshots = TencentAdapter()
        calendar = TradingCalendar(now=lambda: now)
        samples = {}
        for code in ("600519", "000001", "688981", "302132"):
            rows = source.fetch_daily(code, start, end, now)
            snapshot = snapshots.fetch_daily(code, start, end, now)[0]
            latest = rows[-1]
            check("historical_returns_" + code, len(rows) >= 21 and all(row.change_pct is not None for row in rows))
            check("latest_matches_" + code, latest.date == snapshot.date and all(getattr(latest, field) == getattr(snapshot, field)
                                                                               for field in ("open", "close", "high", "low")))
            check("units_" + code, abs(latest.volume - snapshot.volume) <= 100 and abs(latest.amount - snapshot.amount) <= 1)
            check("percents_" + code, abs(latest.change_pct - snapshot.change_pct) <= 0.01 and abs(latest.turnover - snapshot.turnover) <= 0.01)
            check("ttm_not_dynamic_" + code, all(row.pe is None for row in rows[:-1]) and latest.pe == snapshot.pe)
            risk_rows = [row.model_dump(mode="json", exclude={"raw"}) for row in rows]
            assessment = evaluate(risk_rows, code, latest.date)
            check("risk_metrics_available_" + code, assessment["metrics"]["volatility"] is not None and not assessment["limited"])
            report["samples"].append({"code": code, "rows": len(rows), "first_date": rows[0].date,
                                      "latest": latest.model_dump(mode="json", exclude={"raw"}), "assessment": assessment})
            samples[code] = rows
        try:
            source.fetch_daily("920002", start, end, now)
            unsupported = False
        except MarketUnavailable:
            unsupported = True
        check("beijing_not_falsely_supported", unsupported)
        anchor = samples["600519"][-1].date
        calendar_cases = [(anchor, True), (date(2026, 10, 1), False), (date(2026, 11, 28), False)]
        for target, expected in calendar_cases:
            status = calendar.status(target)
            report["calendar"].append(status)
            check("calendar_" + target.isoformat(), status["is_trading_day"] is expected and status["source"] == "baostock")

        class HolidayCalendar:
            def status(self):
                return calendar.status(date(2026, 10, 1))

        calls = []
        scheduler = build_scheduler(lambda: calls.append("unexpected_generation"), calendar_factory=HolidayCalendar)
        scheduler.get_job("daily-summary").func()
        check("verified_holiday_skips_generation", calls == [])

        target = samples["600519"][-2].date
        batch = BatchQuotes(http=FailedPrimary(), fallback=source)
        items = [{"code": code, "name": rows[-1].name, "symbol": market_symbol(code)} for code, rows in samples.items()]
        recovered, missing = batch.fetch(items, target, now)
        report["batch"] = {"date": target, "recovered": [row.code for row in recovered], "missing": missing}
        check("historical_batch_fallback", len(recovered) == 4 and not missing and all(row.date == target for row in recovered))

        mongo = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000,
                            connectTimeoutMS=5000, socketTimeoutMS=5000, tz_aware=True)
        database = mongo[test_db]
        check("isolated_collection", run_id not in database.list_collection_names())
        collection = database[run_id]
        service = MarketService(MarketQuoteRepository(collection), [FailedPrimary(), source], now=lambda: now)
        app = FastAPI()
        app.include_router(market_router)
        app.include_router(calendar_router)
        app.dependency_overrides[get_market_service] = lambda: service
        app.dependency_overrides[get_trading_calendar] = lambda: calendar
        with TestClient(app) as client:
            for code, expected in samples.items():
                path = f"/market/quotes/{code}/refresh?start={start}&end={end}"
                response = client.post(path)
                body = response.json()
                check("api_" + code, response.status_code == 200 and body["source"] == "baostock")
                check("stored_" + code, len(body["quotes"]) == len(expected) == collection.count_documents({"code": code}))
                check("cached_" + code, client.post(path).json()["cached"] is True)
                check("read_" + code, client.get(f"/market/quotes/{code}?start={start}&end={end}").json()["quotes"] == body["quotes"])
                check("raw_hidden_" + code, all("raw" not in row for row in body["quotes"]))
            response = client.get("/market/calendar?date=" + anchor.isoformat())
            check("calendar_api", response.status_code == 200 and response.json()["is_trading_day"] is True)
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
        report["passed"] = report["passed"] and bool(report["cleanup"]) and all(report["cleanup"].values())
        report["seconds"] = round(time.monotonic() - started, 2)
        directory = ROOT / ".sandcastle/logs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (run_id + ".json")
        finish_report(report)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("report", str(path), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

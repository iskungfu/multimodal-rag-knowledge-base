"""只读记录指定日期的自动任务状态；不调用会回收租约的业务 GET 接口。"""

import argparse
from datetime import date, datetime
import json
import logging
import os
from pathlib import Path
import sys
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from dotenv import load_dotenv
    from pymongo import MongoClient
    from knowledge.market.models import SHANGHAI
    from knowledge.runtime import RuntimeConfig

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(SHANGHAI).date())
    args = parser.parse_args()
    load_dotenv(ROOT / "knowledge/.env")
    logging.disable(logging.CRITICAL)
    config = RuntimeConfig.from_env()
    day = args.date.isoformat()
    report = {"checked_at": datetime.now(SHANGHAI).isoformat(), "date": day,
              "database": config.database, "read_only": True,
              "configuration": {"summary_enabled": config.summary_enabled,
                                "summary_time": f"{config.summary_hour:02}:{config.summary_minute:02}",
                                "collection_enabled": config.collection_enabled,
                                "collection_time": "16:00", "timezone": "Asia/Shanghai"}}
    try:
        with urlopen("http://127.0.0.1:8001/openapi.json", timeout=5) as response:
            routes = json.load(response).get("paths", {})
            report["query_service_reachable"] = "/daily-summaries/generate" in routes and "/market/collection/run" in routes
    except Exception as error:
        report["query_service_reachable"] = False
        report["query_service_error_type"] = type(error).__name__

    try:
        with MongoClient(config.mongo_url, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
                         socketTimeoutMS=5000, tz_aware=True, appname="scheduled-operations-readonly") as mongo:
            database = mongo[config.database]
            summary = database[config.mongo_names["summaries"]].find_one(
                {"owner_id": "default_user", "date": day},
                {"_id": 0, "date": 1, "status": 1, "attempts": 1, "error_code": 1,
                 "updated_at": 1, "generated_at": 1, "lease_until": 1})
            if summary:
                summary["has_saved_result"] = database[config.mongo_names["summaries"]].count_documents(
                    {"owner_id": "default_user", "date": day, "result": {"$exists": True}}, maxTimeMS=5000) > 0
            report["summary"] = summary
            report["collection"] = database[config.mongo_names["runs"]].find_one(
                {"date": day}, {"_id": 0, "date": 1, "status": 1, "attempts": 1, "total": 1,
                                "succeeded": 1, "failed": 1, "pending": 1, "universe_complete": 1,
                                "updated_at": 1, "finished_at": 1, "lease_until": 1})
            counts = list(database[config.mongo_names["stocks"]].aggregate([
                {"$match": {"date": day}}, {"$group": {"_id": "$source", "count": {"$sum": 1}}}
            ], maxTimeMS=5000))
            report["quote_count"] = sum(row["count"] for row in counts)
            report["quote_source_counts"] = {str(row["_id"]): row["count"] for row in counts}
            report["storage_read_succeeded"] = True
    except Exception as error:
        report["storage_read_succeeded"] = False
        report["storage_error_type"] = type(error).__name__
    output = ROOT / ".sandcastle/logs" / ("scheduled_operations_" + day + "_" + uuid4().hex[:8] + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"report": str(output), **report}, ensure_ascii=False, default=str))
    return 0 if report["storage_read_succeeded"] and report["query_service_reachable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

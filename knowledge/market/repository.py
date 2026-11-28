"""行情写入使用代码 + 交易日期幂等更新，不覆盖已知估值为空值。"""

from datetime import timezone

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from knowledge.market.adapters import TencentAdapter
from knowledge.market.models import MarketUnavailable, security_name


class MarketQuoteRepository:
    def __init__(self, collection):
        self.collection = collection
        self.collection.create_index([("code", ASCENDING), ("date", ASCENDING)], unique=True)

    def save(self, quotes):
        for quote in quotes:
            document = quote.model_dump()
            document["date"] = quote.date.isoformat()
            # 重抓历史时可能只有 kline，不能连同旧估值的快照证据一起覆盖。
            raw = document.pop("raw")
            known = {key: value for key, value in document.items() if value is not None}
            known.update({f"raw.{key}": value for key, value in raw.items()})
            missing = {key: None for key, value in document.items() if value is None}
            query = {"code": quote.code, "date": document["date"]}
            try:
                self.collection.update_one(query, {"$set": known, "$setOnInsert": missing}, upsert=True)
            except DuplicateKeyError:
                # 多进程同日首次抓取竞争时，唯一索引保证仍只有一条记录。
                self.collection.update_one(query, {"$set": known})

    def read(self, code, start, end):
        rows = list(self.collection.find(
            {"code": code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}},
            {"_id": 0},
        ).sort("date", ASCENDING))
        for row in rows:
            raw = row.pop("raw", {})
            if not isinstance(raw, dict):
                raw = {}
            if code.startswith(("688", "689")) and row.get("source") == "tencent" and raw.get("volume_multiplier") != 1:
                try:
                    observed = row["updated_at"]
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=timezone.utc)
                    snapshot = TencentAdapter().parse_snapshot(raw["snapshot"], code, start, end, observed)
                    if (row["date"] != snapshot.date.isoformat() or security_name(row["name"]) != security_name(snapshot.name)
                            or any(row[field] != getattr(snapshot, field) for field in ("open", "close", "high", "low"))
                            or row["volume"] not in (snapshot.volume, snapshot.volume * 100)):
                        raise MarketUnavailable("历史记录与原始快照不一致")
                    row["volume"] = snapshot.volume
                except (MarketUnavailable, KeyError, TypeError, AttributeError):
                    row["volume"] = None
        return rows

    def save_missing(self, quotes):
        stored = []
        for quote in quotes:
            document = quote.model_dump()
            document["date"] = quote.date.isoformat()
            query = {"code": quote.code, "date": document["date"]}
            try:
                self.collection.update_one(query, {"$setOnInsert": document}, upsert=True)
            except DuplicateKeyError:
                pass
            stored.append(self.collection.find_one(query))
        return stored

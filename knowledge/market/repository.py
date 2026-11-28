"""行情写入使用代码 + 交易日期幂等更新，不覆盖已知估值为空值。"""

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError


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
        return list(self.collection.find(
            {"code": code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}},
            {"_id": 0, "raw": 0},
        ).sort("date", ASCENDING))

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

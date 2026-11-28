"""一个用户/收盘日一条记录；租约 + token 防止跨进程重复发布与过期写回。"""
from datetime import timedelta
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from knowledge.review.privacy import redact_value
from knowledge.utils.answer_policy import finalize_answer

LEASE_SECONDS = 300


class SummaryInputError(ValueError):
    """可安全返回的输入/证券核验提示；不透传任意底层 ValueError。"""


def protect_result(result):
    if not result:
        return result
    citations = [str(item["chunk_id"]) for item in result.get("strategies", []) + result.get("knowledge", [])]
    answer = finalize_answer(result.get("answer", ""), citations)
    result = redact_value({key: value for key, value in result.items() if key != "answer"})
    return {**result, "answer": answer}


class SummaryRepository:
    def __init__(self, collection):
        self.collection = collection
        collection.create_index([("owner_id", 1), ("date", 1)], unique=True)

    def expire(self, owner, now):
        self.collection.update_many({"owner_id": owner, "status": "processing", "lease_until": {"$lte": now}}, {
            "$set": {"status": "failed", "error": "生成超时或进程中断，请手动重试", "updated_at": now},
            "$unset": {"token": "", "lease_until": ""},
        })

    def claim(self, owner, day, now, retry=False):
        self.expire(owner, now)
        key = {"owner_id": owner, "date": day.isoformat()}
        try:
            self.collection.update_one(key, {"$setOnInsert": {
                **key, "status": "pending", "attempts": 0, "created_at": now,
            }}, upsert=True)
        except DuplicateKeyError:
            pass
        allowed = ["pending", "failed"] + (["completed"] if retry else [])
        return self.collection.find_one_and_update({**key, "status": {"$in": allowed}}, {
            "$set": {"status": "processing", "token": uuid4().hex,
                     "lease_until": now + timedelta(seconds=LEASE_SECONDS), "updated_at": now, "error": None},
            "$inc": {"attempts": 1},
        }, return_document=ReturnDocument.AFTER)

    def finish(self, claim, now, result=None, error=None):
        fields = {"status": "failed" if error else "completed", "updated_at": now, "error": error}
        if result is not None:
            fields.update(result=protect_result(result), generated_at=now)
        return self.collection.update_one({"_id": claim["_id"], "owner_id": claim["owner_id"],
            "token": claim["token"], "status": "processing", "lease_until": {"$gt": now}}, {
                "$set": fields, "$unset": {"token": "", "lease_until": ""},
            }).modified_count == 1

    def get(self, owner, day, now):
        self.expire(owner, now)
        return self.present(self.collection.find_one({"owner_id": owner, "date": day.isoformat()}))

    def recent(self, owner, now, limit=30):
        self.expire(owner, now)
        return [self.present(row) for row in self.collection.find({"owner_id": owner}).sort("date", -1).limit(limit)]

    def latest_success(self, owner, now):
        self.expire(owner, now)
        return self.present(self.collection.find_one({"owner_id": owner, "result": {"$exists": True}}, sort=[("date", -1)]))

    @staticmethod
    def present(row):
        if row is None:
            return None
        result = protect_result(row.get("result"))
        row = redact_value({key: value for key, value in row.items() if key not in {"_id", "token", "result"}})
        if result is not None:
            row["result"] = result
        return row


class WatchlistRepository:
    """单文档原子更新同时保证去重和最多 20 个；不预置持仓。"""
    def __init__(self, collection):
        self.collection = collection

    def read(self, owner):
        return redact_value((self.collection.find_one({"_id": owner}) or {}).get("items", []))

    def add(self, owner, security):
        try:
            self.collection.update_one({"_id": owner}, {"$setOnInsert": {"items": []}}, upsert=True)
        except DuplicateKeyError:
            pass
        row = {"code": security.code, "name": security.name, "source": security.source}
        self.collection.update_one({"_id": owner, "items.code": {"$ne": security.code},
                                    "items.19": {"$exists": False}}, {"$push": {"items": row}})
        items = self.read(owner)
        if not any(item["code"] == security.code for item in items):
            raise SummaryInputError("最多关注 20 只股票，请先删除后添加")
        return items

    def remove(self, owner, code):
        self.collection.update_one({"_id": owner}, {"$pull": {"items": {"code": code}}})
        return self.read(owner)

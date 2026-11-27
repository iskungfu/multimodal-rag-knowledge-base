"""先落盘文本，再更新抽取结果。所有读写均限定 owner_id。"""

from datetime import datetime, timezone

from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError


class ReviewConflict(Exception):
    pass


class ReviewRepository:
    def __init__(self, collection):
        self.collection = collection
        collection.create_index([("owner_id", DESCENDING), ("created_at", DESCENDING)])

    def create(self, review_id, owner_id, text):
        now = datetime.now(timezone.utc)
        document = {
            "_id": review_id, "owner_id": owner_id, "doc_type": "review", "original_text": text,
            "extraction": None, "extraction_status": "pending", "extraction_error": None,
            "created_at": now, "updated_at": now,
        }
        try:
            self.collection.insert_one(document)
            return document, True
        except DuplicateKeyError:
            existing = self.get(review_id, owner_id)
            if existing is None or existing["original_text"] != text:
                raise ReviewConflict("请求编号已用于其他复盘") from None
            return existing, False

    def finish(self, review_id, owner_id, extraction, error):
        changes = {
            "extraction": extraction, "extraction_status": "failed" if error else "succeeded",
            "extraction_error": error, "updated_at": datetime.now(timezone.utc),
        }
        self.collection.update_one({"_id": review_id, "owner_id": owner_id}, {"$set": changes})
        return changes

    def get(self, review_id, owner_id):
        return self.collection.find_one({"_id": review_id, "owner_id": owner_id})

    def recent(self, owner_id, limit):
        return list(self.collection.find({"owner_id": owner_id}).sort(
            [("created_at", DESCENDING), ("_id", DESCENDING)]
        ).limit(limit))

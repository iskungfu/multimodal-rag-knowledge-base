"""持久化导入任务；所有访问必须匹配 owner_id 与 personal 类型。"""

from datetime import datetime, timezone

from knowledge.review.privacy import redact_value


class StrategyRepository:
    def __init__(self, collection):
        self.collection = collection
        collection.create_index([("owner_id", 1), ("doc_type", 1), ("created_at", -1)])

    def create(self, document_id, owner_id, title, filename):
        now = datetime.now(timezone.utc)
        self.collection.insert_one(redact_value({
            "_id": document_id, "owner_id": owner_id, "doc_type": "personal",
            "title": title, "filename": filename, "status": "processing", "error": None,
            "chunk_count": 0, "created_at": now, "updated_at": now,
        }))

    def finish(self, document_id, owner_id, status, error=None, chunk_count=0):
        self.collection.update_one(self._scope(document_id, owner_id), {"$set": {
            "status": status, "error": error, "chunk_count": chunk_count,
            "updated_at": datetime.now(timezone.utc),
        }})

    def get(self, document_id, owner_id):
        return self.collection.find_one(self._scope(document_id, owner_id))

    def recent(self, owner_id, limit=20):
        return list(self.collection.find({"owner_id": owner_id, "doc_type": "personal"}).sort(
            [("created_at", -1), ("_id", -1)]).limit(limit))

    @staticmethod
    def _scope(document_id, owner_id):
        return {"_id": document_id, "owner_id": owner_id, "doc_type": "personal"}

"""先落盘文本，再更新抽取结果。所有读写均限定 owner_id。"""

from datetime import datetime, timezone
from uuid import uuid4

from pymongo import DESCENDING, ReadPreference, ReturnDocument
from pymongo.errors import DuplicateKeyError

from knowledge.review.publication import COOLDOWN, LEASE, index_state


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
            "index_status": "pending", "index_attempts": 0,
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
            "index_status": "pending", "index_token": uuid4().hex, "index_manifest": {},
            "index_lease_until": None, "index_retry_after": None, "index_error": None,
            "index_attempts": 0, "index_window": None,
        }
        self.collection.update_one({"_id": review_id, "owner_id": owner_id}, {"$set": changes})
        return changes

    def get(self, review_id, owner_id):
        return self.collection.find_one({"_id": review_id, "owner_id": owner_id})

    def recent(self, owner_id, limit):
        return list(self.collection.find({"owner_id": owner_id}).sort(
            [("created_at", DESCENDING), ("_id", DESCENDING)]
        ).limit(limit))

    def published(self, review_id, owner_id, version):
        if not version:
            return None
        return self.collection.find_one({
            "_id": review_id, "owner_id": owner_id, "doc_type": "review",
            "index_status": "completed", "index_version": version,
        }, {"index_manifest": 1, "original_text": 1})

    def claim_index(self, review_id, owner_id, now):
        """单记录 CAS；每10分钟至多3次、失败间隔30秒、租约180秒。旧记录按ID补索引。"""
        scope = {"_id": review_id, "owner_id": owner_id, "doc_type": "review"}
        old = self.collection.find_one(scope)
        if not old:
            return None
        state = index_state(old, now)
        if not state.can_retry:
            return None
        scope["index_token"] = old.get("index_token")
        scope["index_status"] = old.get("index_status")
        claim = self.collection.find_one_and_update(scope, {"$set": {
            "index_status": "processing", "index_token": uuid4().hex,
            "index_lease_until": now + LEASE,
            "index_window": state.window, "index_attempts": state.attempts + 1,
            "index_error": None, "updated_at": now,
        }}, return_document=ReturnDocument.AFTER)
        if claim:
            claim["previous_index_token"] = old.get("index_token")
            claim["previous_index_version"] = old.get("index_version")
        return claim

    def index_is_unpublished(self, claim):
        document = self.collection.with_options(read_preference=ReadPreference.PRIMARY).find_one({
            "_id": claim["_id"], "owner_id": claim["owner_id"], "doc_type": "review",
        }, {"index_status": 1, "index_token": 1, "index_version": 1})
        if document is None:
            return True
        if document.get("index_status") == "completed" and document.get("index_version") == claim["index_token"]:
            return False
        return document.get("index_token") != claim["index_token"] or document.get("index_status") not in {
            "processing", "completed"}

    def finish_index(self, claim, now, manifest=None):
        success = manifest is not None
        changes = {"index_status": "completed" if success else "failed",
                   "index_error": None if success else "index_unavailable",
                   "index_lease_until": None, "updated_at": now,
                   "index_retry_after": None if success else now + COOLDOWN}
        if success:
            changes.update(index_version=claim["index_token"], index_manifest=manifest)
        return self.collection.update_one({
            "_id": claim["_id"], "owner_id": claim["owner_id"], "doc_type": "review",
            "index_token": claim["index_token"], "index_status": "processing",
            "index_lease_until": {"$gt": now},
        }, {"$set": changes}).modified_count == 1

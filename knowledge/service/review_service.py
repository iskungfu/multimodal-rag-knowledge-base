"""抽取失败不阻塞复盘保存；数据库首次写入失败则明确报错。"""

from pymongo.errors import PyMongoError
from datetime import datetime, timedelta, timezone

from knowledge.review.extraction import ExtractionUnavailable, ReviewExtractor
from knowledge.review.privacy import redact_text, redact_value

DEFAULT_OWNER = "default_user"
from knowledge.utils.answer_policy import DISCLAIMER


def present(document):
    result = dict(document)
    result["original_text"] = redact_text(result["original_text"])
    result["extraction"] = redact_value(result["extraction"])
    result["id"] = result.pop("_id")
    result["disclaimer"] = DISCLAIMER
    now = datetime.now(timezone.utc)
    status = result.get("index_status", "pending")
    deadline = result.get("index_lease_until")
    if status == "processing" and (not deadline or deadline.replace(tzinfo=timezone.utc) <= now):
        status = "interrupted"
    result["index_status"] = status
    retry = result.get("index_retry_after")
    window = result.get("index_window")
    if window and result.get("index_attempts", 0) >= 3:
        retry = max(retry.replace(tzinfo=timezone.utc) if retry else now,
                    window.replace(tzinfo=timezone.utc) + timedelta(minutes=10))
    result["index_retry_after"] = retry
    result["index_can_retry"] = status not in {"completed", "processing"} and (not retry or retry.replace(tzinfo=timezone.utc) <= now)
    for key in ("index_token", "index_manifest", "index_window", "index_lease_until"):
        result.pop(key, None)
    return result


class ReviewService:
    def __init__(self, repository, extractor=None, indexer=None):
        self.repository = repository
        self.extractor = extractor if extractor is not None else ReviewExtractor()
        self.indexer = indexer

    def save(self, text, request_id):
        safe_text = redact_text(text)
        document, created = self.repository.create(str(request_id), DEFAULT_OWNER, safe_text)
        if created:
            extraction, error = None, None
            try:
                extraction = redact_value(self.extractor.extract(safe_text))
            except ExtractionUnavailable:
                error = "model_unavailable"
            except Exception:
                error = "invalid_extraction"
            try:
                document.update(self.repository.finish(document["_id"], DEFAULT_OWNER, extraction, error))
            except PyMongoError:
                result = present(document)
                result["warning"] = "复盘文本已保存，抽取结果未能写入，请稍后查看原文。"
                return result
        if created and self.indexer is not None:
            return self.reindex(document["_id"])
        return present(document)

    def reindex(self, review_id):
        document = self.repository.get(str(review_id), DEFAULT_OWNER)
        if document is None:
            return None
        if self.indexer is not None:
            try:
                self.indexer.index(str(review_id), DEFAULT_OWNER)
                document = self.repository.get(str(review_id), DEFAULT_OWNER) or document
            except Exception:
                result = present(document)
                result["warning"] = "复盘已保存，暂时无法确认索引状态，请稍后刷新或补索引。"
                return result
        return present(document)

    def get(self, review_id):
        document = self.repository.get(str(review_id), DEFAULT_OWNER)
        return present(document) if document else None

    def recent(self, limit):
        return [present(document) for document in self.repository.recent(DEFAULT_OWNER, limit)]

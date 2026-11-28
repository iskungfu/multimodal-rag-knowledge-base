"""抽取失败不阻塞复盘保存；数据库首次写入失败则明确报错。"""

from pymongo.errors import PyMongoError

from knowledge.review.extraction import ExtractionUnavailable, ReviewExtractor
from knowledge.review.indexing import ReviewIndexer
from knowledge.review.privacy import redact_text, redact_value

DEFAULT_OWNER = "default_user"
from knowledge.utils.answer_policy import DISCLAIMER


class ReviewService:
    def __init__(self, repository, extractor=None, indexer=None):
        self.repository = repository
        self.extractor = extractor if extractor is not None else ReviewExtractor()
        self.indexer = indexer if indexer is not None else ReviewIndexer(repository)
        self.index_enabled = indexer is not None

    def present(self, document):
        result = self.indexer.describe(document)
        result["original_text"] = redact_text(result["original_text"])
        result["extraction"] = redact_value(result["extraction"])
        result["id"] = result.pop("_id")
        result["disclaimer"] = DISCLAIMER
        return result

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
                result = self.present(document)
                result["warning"] = "复盘文本已保存，抽取结果未能写入，请稍后查看原文。"
                return result
        if created and self.index_enabled:
            return self.reindex(document["_id"])
        return self.present(document)

    def reindex(self, review_id):
        document = self.repository.get(str(review_id), DEFAULT_OWNER)
        if document is None:
            return None
        if self.index_enabled:
            try:
                self.indexer.index(str(review_id), DEFAULT_OWNER)
                document = self.repository.get(str(review_id), DEFAULT_OWNER) or document
            except Exception:
                result = self.present(document)
                result["warning"] = "复盘已保存，暂时无法确认索引状态，请稍后刷新或补索引。"
                return result
        return self.present(document)

    def get(self, review_id):
        document = self.repository.get(str(review_id), DEFAULT_OWNER)
        return self.present(document) if document else None

    def recent(self, limit):
        return [self.present(document) for document in self.repository.recent(DEFAULT_OWNER, limit)]

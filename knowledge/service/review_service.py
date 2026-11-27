"""抽取失败不阻塞复盘保存；数据库首次写入失败则明确报错。"""

from pymongo.errors import PyMongoError

from knowledge.review.extraction import ExtractionUnavailable, ReviewExtractor
from knowledge.review.privacy import redact_text, redact_value

DEFAULT_OWNER = "default_user"
DISCLAIMER = "以上信息不构成投资建议，投资有风险，入市需谨慎。"


def present(document):
    result = dict(document)
    result["original_text"] = redact_text(result["original_text"])
    result["extraction"] = redact_value(result["extraction"])
    result["id"] = result.pop("_id")
    result["disclaimer"] = DISCLAIMER
    return result


class ReviewService:
    def __init__(self, repository, extractor=None):
        self.repository = repository
        self.extractor = extractor if extractor is not None else ReviewExtractor()

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
        return present(document)

    def get(self, review_id):
        document = self.repository.get(str(review_id), DEFAULT_OWNER)
        return present(document) if document else None

    def recent(self, limit):
        return [present(document) for document in self.repository.recent(DEFAULT_OWNER, limit)]

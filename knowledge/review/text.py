"""复盘输入、持久化和完整引用共享的文本契约。"""
from knowledge.review.privacy import redact_text

MAX_REVIEW_TEXT_LENGTH = 10000


class ReviewTextError(ValueError):
    """可安全返回的文本提示，不携带用户原文。"""


def prepare_review_text(text):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_REVIEW_TEXT_LENGTH:
        raise ReviewTextError("复盘内容须为 1 至 10000 字的非空文本")
    safe = redact_text(text)
    if not safe.strip() or len(safe) > MAX_REVIEW_TEXT_LENGTH:
        raise ReviewTextError("脱敏后的复盘内容须为 1 至 10000 字，请调整后重试")
    return safe

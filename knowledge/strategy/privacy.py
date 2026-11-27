"""复用 ADR-0005 规则；策略图片不持久化、不生成公开对象 URL。"""

import re
from pathlib import PurePosixPath

from knowledge.review.privacy import redact_text


def safe_filename(filename):
    name = PurePosixPath((filename or "策略.md").replace("\\", "/")).name
    stem, dot, suffix = name.rpartition(".")
    # 文件名常用下划线分隔字段，转为空格后复用姓名规则的词边界。
    stem = re.sub(r"[_-]+", " ", stem if dot else name)
    return redact_text(stem)[:200] + ("." + suffix.lower() if dot else "")


def sanitize_markdown(text):
    # 图片像素不能靠文本规则脱敏；去除引用，避免原图通过召回暴露。
    text = re.sub(r"!\[([^\]]*)\]\([^\n]*?\)", r"[图片已省略：\1]", text)
    text = re.sub(r"!\[([^\]]*)\](?:\[[^\]]*\])?", r"[图片已省略：\1]", text)
    text = re.sub(r"<img\b[^>]*>", "[图片已省略]", text, flags=re.I)
    return redact_text(text)

"""所有聊天回答共用的服务端终稿策略；先完整脱敏，再附加一次免责声明。"""

import re
import uuid

from knowledge.review.privacy import redact_text

DISCLAIMER = "以上信息不构成投资建议，投资有风险，入市需谨慎。"


def finalize_answer(text, citation_ids=()):
    if not isinstance(text, str):
        text = "未生成有效文字回答，请稍后重试。"
    # 兼容模型重复输出、旧版无句号以及全角/半角句号。
    text = re.sub(re.escape(DISCLAIMER[:-1]) + r"[。.]?", "", text or "").strip()
    protected = {}
    nonce = uuid.uuid4().hex.translate(str.maketrans("0123456789", "klmnopqrst"))
    for index, citation_id in enumerate(citation_ids):
        citation_id = str(citation_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", citation_id):
            continue
        marker = f"[chunk_id={citation_id}]"
        if marker in text:
            token = f"RAGREFERENCE{nonce}TOKEN{index}END"
            protected[token] = marker
            text = text.replace(marker, token)
    text = redact_text(text).strip()
    for token, marker in protected.items():
        text = text.replace(token, marker)
    return (text + "\n\n" if text else "") + DISCLAIMER

"""所有聊天回答共用的服务端终稿策略；先完整脱敏，再附加一次免责声明。"""

import re

from knowledge.review.privacy import redact_text

DISCLAIMER = "以上信息不构成投资建议，投资有风险，入市需谨慎。"


def finalize_answer(text):
    if not isinstance(text, str):
        text = "未生成有效文字回答，请稍后重试。"
    # 兼容模型重复输出、旧版无句号以及全角/半角句号。
    text = re.sub(re.escape(DISCLAIMER[:-1]) + r"[。.]?", "", text or "").strip()
    text = redact_text(text).strip()
    return (text + "\n\n" if text else "") + DISCLAIMER

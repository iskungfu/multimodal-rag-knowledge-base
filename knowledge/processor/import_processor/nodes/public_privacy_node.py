from pathlib import Path

from knowledge.processor.import_processor.base import BaseNode
from knowledge.review.privacy import redact_text
from knowledge.strategy.privacy import sanitize_markdown


class PublicPrivacyNode(BaseNode):
    name = "public_privacy_node"

    def process(self, state):
        path = Path(state["md_path"])
        content = sanitize_markdown(path.read_text(encoding="utf-8-sig"))
        if not content.strip():
            raise ValueError("解析后的公共文档正文为空")
        path.write_text(content, encoding="utf-8")
        state["file_title"] = redact_text(state.get("document_title") or state["file_title"])
        state["md_content"] = content
        state["owner_id"] = "public"
        return state

"""策略切片保留完整标题路径，避免行动脱离上级适用条件。"""

import re

from langchain_text_splitters import RecursiveCharacterTextSplitter

from knowledge.processor.import_processor.nodes.document_split_node import DocumentSplitNode
from knowledge.utils.markdown_util import MarkdownTableLinearizer


class StrategySplitNode(DocumentSplitNode):
    def _split_and_merge(self, sections, max_content_length, min_content_length):
        hierarchy = {}
        introductions = {}
        result = []
        for section in sections:
            body = section["body"]
            if "<table>" in body:
                body = MarkdownTableLinearizer.process(body)
            heading = re.match(r"^\s*(#{1,6})\s+", section["title"])
            if heading:
                level = len(heading[1])
                hierarchy = {depth: title for depth, title in hierarchy.items() if depth < level}
                introductions = {depth: text for depth, text in introductions.items() if depth < level}
                hierarchy[level] = section["title"]
                context = "\n".join(
                    title + ("\n" + introductions[depth] if depth in introductions else "")
                    for depth, title in hierarchy.items()
                )
                if body.strip() and len(body) < min_content_length:
                    introductions[level] = body.strip()
            else:
                context = section["title"]
            prefix = context + "\n\n"
            budget = max_content_length - len(prefix)
            if budget < 0 or (budget == 0 and body.strip()):
                raise ValueError("策略标题及适用条件超过切片预算，请缩短标题后重新导入")
            if not body.strip():
                result.append({**section, "body": "", "context": context})
                continue
            splitter = RecursiveCharacterTextSplitter(
                separators=["\n\n", "\n", "。", "？", "！", "；", ".", "?", "!", ";", " ", ""],
                chunk_size=budget, chunk_overlap=0, keep_separator=True,
            )
            for fragment in splitter.split_text(body):
                result.append({**section, "body": fragment, "context": context})
        return result

    def _build_chunks(self, sections):
        chunks = super()._build_chunks(sections)
        for chunk, section in zip(chunks, sections):
            chunk["text"] = section["context"] + "\n\n" + section["body"]
        return chunks

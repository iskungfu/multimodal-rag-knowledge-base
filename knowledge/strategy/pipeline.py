"""个人策略 LangGraph：复用解析、切分、BGE-M3，跳过商品名识别。"""

from pathlib import Path

from langgraph.graph import StateGraph, END

from knowledge.processor.import_processor.base import BaseNode
from knowledge.processor.import_processor.config import ImportConfig
from knowledge.processor.import_processor.nodes.entry_node import EntryNode
from knowledge.processor.import_processor.nodes.pdf_to_md_node import PdfToMdNode
from knowledge.processor.import_processor.nodes.document_split_node import DocumentSplitNode
from knowledge.processor.import_processor.nodes.bge_embedding_chunks_node import BgeEmbeddingChunksNode
from knowledge.processor.import_processor.state import ImportGraphState
from knowledge.strategy.privacy import sanitize_markdown
from knowledge.review.privacy import redact_text


class StrategyState(ImportGraphState, total=False):
    owner_id: str
    document_id: str
    strategy_title: str
    chunk_count: int


class StrategyPrivacyNode(BaseNode):
    name = "strategy_privacy_node"

    def process(self, state):
        path = Path(state["md_path"])
        content = sanitize_markdown(path.read_text(encoding="utf-8-sig"))
        if not content.strip():
            raise ValueError("解析后的策略正文为空")
        path.write_text(content, encoding="utf-8")
        state["file_title"] = redact_text(state["strategy_title"])
        state["md_content"] = content
        return state


class StrategyInsertNode(BaseNode):
    name = "milvus_import_node"

    def __init__(self, vectors, config):
        super().__init__(config)
        self.vectors = vectors

    def process(self, state):
        state["chunk_count"] = self.vectors.insert(
            state["chunks"], state["owner_id"], state["document_id"],
        )
        return state


def build_strategy_graph(vectors, config=None):
    config = config or ImportConfig.from_env()
    graph = StateGraph(StrategyState)
    for name, node in {
        "entry_node": EntryNode(config),
        "pdf_to_md_node": PdfToMdNode(config),
        "strategy_privacy_node": StrategyPrivacyNode(config),
        "document_split_node": DocumentSplitNode(config),
        "bge_embedding_chunks_node": BgeEmbeddingChunksNode(config),
        "milvus_import_node": StrategyInsertNode(vectors, config),
    }.items():
        graph.add_node(name, node)
    graph.set_entry_point("entry_node")
    graph.add_conditional_edges("entry_node", lambda state: (
        "pdf_to_md_node" if state.get("is_pdf_read_enabled") else "strategy_privacy_node"
    ))
    graph.add_edge("pdf_to_md_node", "strategy_privacy_node")
    graph.add_edge("strategy_privacy_node", "document_split_node")
    graph.add_edge("document_split_node", "bge_embedding_chunks_node")
    graph.add_edge("bge_embedding_chunks_node", "milvus_import_node")
    graph.add_edge("milvus_import_node", END)
    return graph.compile()

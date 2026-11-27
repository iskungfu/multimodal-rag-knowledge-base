"""投资查询专用 LangGraph：三类资料融合，行情为独立上下文。"""
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from knowledge.investment.generation import EvidenceSelector, distinct_records, render_answer, rerank, validate_selections
from knowledge.investment.retrieval import InvestmentRetriever, fuse
from knowledge.review.privacy import redact_text


class InvestmentState(TypedDict, total=False):
    query: str
    market: list[dict]
    routes: dict
    fused: list[dict]
    reranked_docs: list[dict]
    rerank_status: str
    selection_status: str
    answer: str


class InvestmentAssistant:
    def __init__(self, retriever=None, selector=None, reranker=None):
        self.retriever = retriever if retriever is not None else InvestmentRetriever()
        self.selector = selector if selector is not None else EvidenceSelector()
        self.reranker = reranker if reranker is not None else rerank
        workflow = StateGraph(InvestmentState)
        workflow.add_node("retrieve", self._retrieve)
        workflow.add_node("fuse", self._fuse)
        workflow.add_node("rerank", self._rerank)
        workflow.add_node("answer", self._answer)
        workflow.set_entry_point("retrieve")
        workflow.add_edge("retrieve", "fuse")
        workflow.add_edge("fuse", "rerank")
        workflow.add_edge("rerank", "answer")
        workflow.add_edge("answer", END)
        self.graph = workflow.compile()

    def _retrieve(self, state):
        try:
            routes = self.retriever.retrieve(state["query"])
        except Exception:
            routes = {kind: {"status": "failed", "items": []} for kind in ("personal", "knowledge", "review")}
        return {"routes": routes}

    def _fuse(self, state):
        return {"fused": fuse(state["routes"])}

    def _rerank(self, state):
        try:
            documents, status = self.reranker(state["query"], state["fused"])
        except Exception:
            documents, status = state["fused"][:6], "failed"
        return {"reranked_docs": documents, "rerank_status": status}

    def _answer(self, state):
        documents = distinct_records(state["reranked_docs"])
        status = "empty"
        if documents:
            try:
                value = self.selector.select(state["query"], documents, state["market"])
                documents = validate_selections(value, documents, state["market"])
                status = "completed"
            except Exception:
                status = "failed"
        answer = render_answer(documents, state["market"], state["routes"], status, state["rerank_status"])
        return {"answer": answer, "selection_status": status, "reranked_docs": documents}

    def answer(self, query, market_context=None) -> dict[str, Any]:
        market = [{"id": str(item["id"]), "text": redact_text(item["text"])} for item in (market_context or [])]
        return self.graph.invoke({"query": redact_text(query), "market": market})

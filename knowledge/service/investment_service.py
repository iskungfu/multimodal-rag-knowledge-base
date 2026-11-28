"""投资查询专用 LangGraph：三类资料融合，行情为独立上下文。"""
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from knowledge.investment.generation import EvidenceSelector, distinct_records, evidence_text, render_answer, rerank, validate_selections
from knowledge.investment.retrieval import InvestmentRetriever, fuse
from knowledge.review.privacy import redact_text
from knowledge.personalization.settings import settings_context
from knowledge.utils.answer_policy import finalize_answer


class InvestmentState(TypedDict, total=False):
    query: str
    market: list[dict]
    routes: dict
    fused: list[dict]
    reranked_docs: list[dict]
    rerank_status: str
    selection_status: str
    selection_error: str
    answer: str
    personalization: dict
    retrieval_query: str


class InvestmentAssistant:
    def __init__(self, retriever=None, selector=None, reranker=None, settings=None):
        self.retriever = retriever if retriever is not None else InvestmentRetriever()
        self.selector = selector if selector is not None else EvidenceSelector()
        self.reranker = reranker if reranker is not None else rerank
        self.settings = settings
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
            identifiers = state.get("personalization", {}).get("document_ids")
            options = {"document_ids": identifiers} if identifiers is not None else {}
            routes = self.retriever.retrieve(state["retrieval_query"], **options)
        except Exception:
            routes = {kind: {"status": "failed", "items": []} for kind in ("personal", "knowledge", "review")}
        return {"routes": routes}

    def _fuse(self, state):
        return {"fused": fuse(state["routes"])}

    def _rerank(self, state):
        try:
            documents, status = self.reranker(state["retrieval_query"], state["fused"])
        except Exception:
            documents, status = state["fused"][:6], "failed"
        return {"reranked_docs": documents, "rerank_status": status}

    def _answer(self, state):
        documents = distinct_records(state["reranked_docs"])
        status = "empty"
        error = ""
        if documents:
            try:
                value = self.selector.select(state["retrieval_query"], documents, state["market"])
                documents = validate_selections(value, documents, state["market"])
                status = "completed"
            except Exception as failure:
                status = "failed"
                error = type(failure).__name__
        answer = render_answer(documents, state["market"], state["routes"], status, state["rerank_status"])
        if state.get("personalization"):
            answer = finalize_answer(evidence_text(state["personalization"]["text"]) + "\n\n" + answer,
                                     [document["chunk_id"] for document in documents])
        return {"answer": answer, "selection_status": status, "selection_error": error, "reranked_docs": documents}

    def answer(self, query, market_context=None) -> dict[str, Any]:
        market = [{"id": str(item["id"]), "text": redact_text(item["text"])} for item in (market_context or [])]
        context = settings_context(self.settings()) if self.settings is not None else {}
        safe_query = redact_text(query)
        retrieval_query = safe_query + ("\n用户档案资料：\n" + context["text"] if context else "")
        return self.graph.invoke({"query": safe_query, "market": market,
                                  "retrieval_query": retrieval_query, "personalization": context})

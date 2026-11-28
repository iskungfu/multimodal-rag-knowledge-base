"""查询业务服务

封装查询流程的执行、任务状态读写与历史会话管理，供 API 层调用。
"""

import logging
import uuid
from typing import Any, Dict, List

from knowledge.processor.query_processor.main_graph import query_app
from knowledge.utils.mongo_history_util import clear_history, get_recent_messages, save_chat_message
from knowledge.review.privacy import redact_text, redact_value
from knowledge.utils.answer_policy import finalize_answer
from knowledge.risk.symbols import SymbolResolver
from knowledge.service.risk_service import RiskService
from knowledge.utils.sse_util import push_sse_event, SSEEvent
from knowledge.utils.task_util import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    get_task_info,
    get_task_result,
    set_task_result,
    update_task_status,
)

logger = logging.getLogger(__name__)


class QueryService:
    """查询业务服务"""

    def __init__(self, resolver=None, risk=None, graph=None, investment=None, *, history=None,
                 index_context_factory=None, graph_factory=None):
        self.resolver = resolver or SymbolResolver()
        self.risk = risk or RiskService()
        self.graph = graph
        self.history = history
        self.index_context_factory = index_context_factory
        self.graph_factory = graph_factory
        if investment is None:
            from knowledge.service.investment_service import InvestmentAssistant
            from knowledge.personalization.settings import current_settings
            investment = InvestmentAssistant(settings=current_settings)
        self.investment = investment

    # ── ID 生成 ──

    @staticmethod
    def generate_session_id() -> str:
        """生成会话ID（多轮对话共享，用于历史记录）"""
        return f"sess-{uuid.uuid4().hex}"

    @staticmethod
    def generate_task_id() -> str:
        """生成任务ID（单次提问唯一，用于任务状态与SSE队列）"""
        return uuid.uuid4().hex

    # ── 查询主流程 ──

    def run_query_graph(self, task_id: str, session_id: str, user_query: str, is_stream: bool) -> None:
        """执行查询主图。

        注意：这是同步阻塞方法。
        - 流式模式：由 BackgroundTasks 在线程池中调用
        - 非流式模式：由路由通过 run_in_executor 调用，避免阻塞事件循环
        """
        update_task_status(task_id, TASK_STATUS_PROCESSING)

        safe_query = redact_text(user_query)
        state = {"original_query": safe_query, "session_id": session_id,
                 "task_id": task_id, "is_stream": is_stream}
        error = ""
        context = None
        try:
            from knowledge.investment.routing import route_query
            route = route_query(safe_query, self.resolver, get_recent_messages(session_id, limit=4, collection=self.history))
            resolution = route["resolution"]
            if resolution.message:
                answer = resolution.message
            elif route["investment"]:
                market = []
                security = None
                if resolution.security:
                    security = {"code": resolution.security.code, "name": resolution.security.name}
                    try:
                        risk_answer = self.risk.answer(resolution.security)
                    except Exception:
                        risk_answer = "行情服务暂不可用，无法判断当前个股风险。"
                    market = [{"id": "stock:" + security["code"], "text": risk_answer}]
                if route["market"]:
                    from knowledge.investment.market import index_context
                    market.extend((self.index_context_factory or index_context)())
                state.update(self.investment.answer(route["query"], market_context=market))
                answer = state["answer"]
                context = {"investment": True, "query": route["query"][:1500], "security": security, "market": route["market"]}
            else:
                state["knowledge_query"] = resolution.knowledge
                graph = self.graph if self.graph is not None else (self.graph_factory() if self.graph_factory else query_app)
                state = graph.invoke(state)
                answer = state.get("answer") or "未找到可用的回答，请补充问题后重试。"
        except Exception:
            # 不记录异常原文或 traceback，连接异常可能带有 URL/凭据。
            logger.error("查询流程执行失败")
            error = "查询流程执行失败，请稍后重试。"
            answer = error

        citation_ids = [str(document["chunk_id"]) for document in state.get("reranked_docs", []) if document.get("chunk_id") is not None]
        answer = finalize_answer(answer, citation_ids)
        user_saved = save_chat_message(session_id, "user", safe_query,
                          rewritten_query=state.get("rewritten_query", ""),
                          item_names=state.get("item_names", []), query_context=context, collection=self.history)
        saved = save_chat_message(session_id, "assistant", answer,
                                  item_names=state.get("item_names", []), citation_ids=citation_ids, collection=self.history)
        if not saved or not user_saved:
            error = error or "回答已生成，但会话历史保存失败，请稍后重试。"
        set_task_result(task_id, "answer", answer)
        set_task_result(task_id, "citation_ids", citation_ids)
        set_task_result(task_id, "error", error)
        update_task_status(task_id, TASK_STATUS_FAILED if error else TASK_STATUS_COMPLETED)
        if is_stream:
            # 一次发布完整、安全的 delta，final 为同一终稿，之后不再发 progress。
            push_sse_event(task_id, SSEEvent.DELTA, {"answer": answer})
            push_sse_event(task_id, SSEEvent.FINAL, {"answer": answer, "error": error})

    # ── 结果读取 ──

    def get_answer(self, task_id: str) -> str:
        """获取任务生成的答案（未完成时返回空字符串）"""
        answer = get_task_result(task_id, "answer", "")
        return finalize_answer(answer, get_task_result(task_id, "citation_ids", [])) if answer else ""

    def get_status(self, task_id: str) -> Dict[str, Any]:
        """获取任务状态 + 结果（供 /status 与 /query 非流式响应使用）"""
        task_info = get_task_info(task_id)
        task_info["answer"] = self.get_answer(task_id)
        task_info["error"] = get_task_result(task_id, "error", "")
        return task_info

    # ── 历史会话 ──

    def get_history(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """读取某个会话最近的历史消息（按时间正序）"""
        records = get_recent_messages(session_id, limit=limit, collection=self.history)

        return [
            {
                "session_id": record.get("session_id", ""),
                "role": record.get("role", ""),
                "text": finalize_answer(record.get("text", ""), record.get("citation_ids", [])) if record.get("role") == "assistant" else redact_text(record.get("text", "")),
                "rewritten_query": redact_text(record.get("rewritten_query", "")),
                "item_names": redact_value(record.get("item_names", [])),
                "ts": record.get("ts"),
            }
            for record in records
        ]

    def clear_history(self, session_id: str) -> int:
        """清空某个会话的全部历史消息，返回删除数量"""
        return clear_history(session_id, collection=self.history)

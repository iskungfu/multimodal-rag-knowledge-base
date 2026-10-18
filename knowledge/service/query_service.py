"""查询业务服务

封装查询流程的执行、任务状态读写与历史会话管理，供 API 层调用。
"""

import logging
import uuid
from typing import Any, Dict, List

from knowledge.processor.query_processor.main_graph import query_app
from knowledge.utils.mongo_history_util import clear_history, get_recent_messages
from knowledge.utils.task_util import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    get_task_info,
    get_task_result,
    update_task_status,
)

logger = logging.getLogger(__name__)


class QueryService:
    """查询业务服务"""

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

        try:
            initial_state = {
                "original_query": user_query,
                "session_id": session_id,
                "task_id": task_id,
                "is_stream": is_stream,
            }

            # 执行 LangGraph 查询流程
            query_app.invoke(initial_state)

            update_task_status(task_id, TASK_STATUS_COMPLETED)

        except Exception as e:
            logger.error(f"查询流程执行失败(task_id={task_id}): {e}", exc_info=True)
            update_task_status(task_id, TASK_STATUS_FAILED)

    # ── 结果读取 ──

    def get_answer(self, task_id: str) -> str:
        """获取任务生成的答案（未完成时返回空字符串）"""
        return get_task_result(task_id, "answer", "")

    def get_status(self, task_id: str) -> Dict[str, Any]:
        """获取任务状态 + 结果（供 /status 与 /query 非流式响应使用）"""
        task_info = get_task_info(task_id)
        task_info["answer"] = get_task_result(task_id, "answer", "")
        task_info["error"] = get_task_result(task_id, "error", "")
        return task_info

    # ── 历史会话 ──

    def get_history(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """读取某个会话最近的历史消息（按时间正序）"""
        records = get_recent_messages(session_id, limit=limit)

        return [
            {
                "session_id": record.get("session_id", ""),
                "role": record.get("role", ""),
                "text": record.get("text", ""),
                "rewritten_query": record.get("rewritten_query", ""),
                "item_names": record.get("item_names", []),
                "ts": record.get("ts"),
            }
            for record in records
        ]

    def clear_history(self, session_id: str) -> int:
        """清空某个会话的全部历史消息，返回删除数量"""
        return clear_history(session_id)

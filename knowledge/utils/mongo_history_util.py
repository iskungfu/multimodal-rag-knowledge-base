"""MongoDB 历史会话工具

提供查询流程的聊天历史读写能力：
- save_chat_message：保存一条聊天消息（user / assistant）
- get_recent_messages：读取某个 session 最近的聊天记录（按时间正序）
- clear_history：清空某个 session 的全部聊天记录

文档结构：
{
    "_id": ObjectId,
    "session_id": "sess-xxx",
    "role": "user" | "assistant",
    "text": "消息内容",
    "rewritten_query": "重写后的查询",
    "item_names": ["商品A"],
    "message_id": "消息ID（可空）",
    "ts": 1773926682.007168
}
"""

import os
import time
import logging
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv

from knowledge.utils.clients.storage_clients import StorageClients

load_dotenv()

logger = logging.getLogger(__name__)

# 聊天历史集合名
CHAT_HISTORY_COLLECTION = "chat_history"


def _get_collection():
    """获取聊天历史集合（复用 StorageClients 的单例 MongoDB 客户端）"""
    client = StorageClients.get_mongo_client()
    db_name = os.getenv("MONGO_DB_NAME", "kb001")
    return client[db_name][CHAT_HISTORY_COLLECTION]


def save_chat_message(
        session_id: str,
        role: str,
        text: str,
        rewritten_query: str = "",
        item_names: Optional[List[str]] = None,
        message_id: Optional[str] = None
) -> str:
    """保存一条聊天消息，返回插入的文档 id（失败时返回空字符串）"""
    if not session_id or not text:
        return ""

    document = {
        "session_id": session_id,
        "role": role,
        "text": text,
        "rewritten_query": rewritten_query or "",
        "item_names": item_names or [],
        "message_id": message_id,
        "ts": time.time(),
    }

    try:
        result = _get_collection().insert_one(document)
        return str(result.inserted_id)
    except Exception as e:
        # 历史保存失败不应该影响主流程
        logger.error(f"保存聊天历史失败(session_id={session_id}): {e}")
        return ""


def get_recent_messages(session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """读取某个 session 最近 limit 条聊天记录，按时间正序返回（老 -> 新）"""
    if not session_id:
        return []

    try:
        collection = _get_collection()

        # 1. 按时间倒序取最近 limit 条
        cursor = collection.find({"session_id": session_id}).sort("ts", -1).limit(limit)
        messages = list(cursor)

        # 2. 反转为正序，便于按对话顺序拼提示词
        messages.reverse()

        # 3. 去掉 _id（ObjectId 不可 JSON 序列化）
        for msg in messages:
            msg.pop("_id", None)

        return messages
    except Exception as e:
        logger.error(f"读取聊天历史失败(session_id={session_id}): {e}")
        return []


def clear_history(session_id: str) -> int:
    """清空某个 session 的全部聊天记录，返回删除数量"""
    if not session_id:
        return 0

    try:
        result = _get_collection().delete_many({"session_id": session_id})
        return result.deleted_count
    except Exception as e:
        logger.error(f"清空聊天历史失败(session_id={session_id}): {e}")
        return 0

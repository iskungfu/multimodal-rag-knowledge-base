"""SSE 推送工具

纯 SSE 队列与事件推送（无外部依赖）：
- 队列生命周期：create_sse_queue / get_sse_queue / remove_sse_queue
- push_sse_event：同步推送事件（可在任意线程调用，队列不存在时静默忽略）
- sse_generator：FastAPI StreamingResponse 使用的异步生成器
"""

import asyncio
import json
import queue
from typing import Dict, Any, Optional, AsyncGenerator

from fastapi import Request


class SSEEvent:
    READY = "ready"         # 连接建立
    PROGRESS = "progress"   # 任务节点进度
    DELTA = "delta"         # LLM 流式输出增量
    FINAL = "final"         # 最终完整答案


# 全局 SSE 任务队列存储
# Key: task_id, Value: queue.Queue
_task_stream: Dict[str, queue.Queue] = {}


def get_sse_queue(task_id: str) -> Optional[queue.Queue]:
    """获取指定任务的队列"""
    return _task_stream.get(task_id)


def create_sse_queue(task_id: str) -> queue.Queue:
    """创建并注册一个新的 SSE 队列"""
    q = queue.Queue()
    _task_stream[task_id] = q
    return q


def remove_sse_queue(task_id: str):
    """移除指定任务的队列（不存在时静默忽略，防止内存泄漏）"""
    _task_stream.pop(task_id, None)


def _sse_pack(event: str, data: Dict[str, Any]) -> str:
    """打包 SSE 消息格式"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def push_sse_event(task_id: str, event: str, data: Dict[str, Any]):
    """通过 task_id 推送事件到 SSE 队列（队列不存在时静默忽略）"""
    # 1. 获取 SSE 队列
    stream_queue = get_sse_queue(task_id)

    # 2. 队列存在则推送事件
    if stream_queue:
        stream_queue.put({"event": event, "data": data})


async def sse_generator(task_id: str, request: Request) -> AsyncGenerator[str, None]:
    """SSE 生成器，用于 FastAPI 的 StreamingResponse"""
    # 1. 获取队列
    stream_queue = get_sse_queue(task_id)

    # 2. 没有对应的队列（任务不存在或已结束），直接结束
    if stream_queue is None:
        return

    # 3. 获取当前事件循环（用于把阻塞的队列读取放入线程池）
    loop = asyncio.get_running_loop()
    try:
        while True:
            # 4. 检测客户端断开，尽快退出
            if await request.is_disconnected():
                break
            try:
                # 5. run_in_executor 避免阻塞事件循环；1 秒超时用于周期性检查断开状态
                msg = await loop.run_in_executor(None, stream_queue.get, True, 1.0)
            except queue.Empty:
                continue

            # 6. 解析消息并打包为 SSE 格式推送
            event = msg.get("event")
            data = msg.get("data")
            yield _sse_pack(event, data)

    except (ConnectionResetError, BrokenPipeError):
        # 客户端刷新页面或关闭标签页，TCP 管道破裂，静默退出
        return
    except asyncio.CancelledError:
        # 服务端中断：重新抛出，让外层知道协程被成功取消
        raise
    finally:
        # 7. 清理资源（防止内存泄漏）
        remove_sse_queue(task_id)

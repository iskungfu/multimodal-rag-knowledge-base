"""查询流程 API

提供：
- POST   /query                提交查询（流式 / 非流式）
- GET    /stream/{task_id}      SSE 推送进度与答案
- GET    /status/{task_id}      任务状态查询（轮询备用）
- GET    /history/{session_id}  历史会话读取
- DELETE /history/{session_id}  历史会话清空
- GET    /chat.html             聊天页面
"""

import asyncio
import logging
import os

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from starlette.staticfiles import StaticFiles

from knowledge.core.deps import get_query_service
from knowledge.core.paths import get_front_page_dir
from knowledge.processor.query_processor.base import setup_logging
from knowledge.schema.query_schema import (
    HistoryResponse,
    QueryRequest,
    QueryResponse,
    QueryStatusResponse,
    StreamSubmitResponse,
)
from knowledge.service.query_service import QueryService
from knowledge.utils.sse_util import create_sse_queue, sse_generator

logger = logging.getLogger(__name__)


def register_router(app: FastAPI) -> None:
    """注册查询相关路由"""

    @app.get("/", include_in_schema=False)
    def index():
        """根路径跳转到聊天页面"""
        return RedirectResponse(url="/front/chat.html")

    @app.get("/chat.html", include_in_schema=False)
    def chat_page():
        """直接访问聊天页面（也可以走 /front/chat.html）"""
        page_path = os.path.join(get_front_page_dir(), "chat.html")
        if not os.path.exists(page_path):
            raise HTTPException(status_code=404, detail="chat.html not found")
        return FileResponse(page_path)

    @app.post("/query", response_model=None)
    async def query(
            request: QueryRequest,
            background_tasks: BackgroundTasks,
            service: QueryService = Depends(get_query_service),
    ):
        """提交查询。

        流式：立即返回 task_id，前端拿 task_id 去连 /stream/{task_id}
        非流式：等主流程执行完，直接返回答案
        """
        # 1. 会话ID（多轮对话共享），没传则新生成
        session_id = request.session_id or service.generate_session_id()

        # 2. 任务ID（单次提问唯一）
        task_id = service.generate_task_id()

        # 3. 流式模式
        if request.is_stream:
            # 3.1 必须在返回响应前创建队列，否则前端连 /stream 时队列还不存在
            create_sse_queue(task_id)

            # 3.2 后台执行主图
            background_tasks.add_task(
                service.run_query_graph, task_id, session_id, request.query, True
            )

            # 3.3 立即返回 task_id
            return StreamSubmitResponse(
                message="Query submitted",
                session_id=session_id,
                task_id=task_id,
            )

        # 4. 非流式模式：放线程池执行，避免阻塞事件循环
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, service.run_query_graph, task_id, session_id, request.query, False
        )

        # 5. 返回完整结果
        status = service.get_status(task_id)
        return QueryResponse(
            message="处理完成",
            session_id=session_id,
            answer=service.get_answer(task_id),
            done_list=status.get("done_list", []),
            error=status.get("error", ""),
            image_urls=[],
        )

    @app.get("/stream/{task_id}")
    async def stream(task_id: str, request: Request):
        """SSE 长连接：推送 progress / delta / final 事件"""
        return StreamingResponse(
            sse_generator(task_id, request),
            media_type="text/event-stream",
        )

    @app.get("/status/{task_id}", response_model=QueryStatusResponse)
    def get_status(
            task_id: str,
            service: QueryService = Depends(get_query_service),
    ):
        """查询任务状态（轮询备用，前端当前主要走 SSE）"""
        return QueryStatusResponse(**service.get_status(task_id))

    @app.get("/history/{session_id}", response_model=HistoryResponse)
    def get_history(
            session_id: str,
            limit: int = 50,
            service: QueryService = Depends(get_query_service),
    ):
        """读取某个会话的历史消息"""
        items = service.get_history(session_id, limit)
        return HistoryResponse(session_id=session_id, items=items)

    @app.delete("/history/{session_id}")
    def clear_history(
            session_id: str,
            service: QueryService = Depends(get_query_service),
    ):
        """清空某个会话的历史消息"""
        deleted_count = service.clear_history(session_id)
        return {"message": "History cleared", "deleted_count": deleted_count}


def create_app() -> FastAPI:
    """创建查询服务 FastAPI 应用"""
    app = FastAPI(
        title="掌柜智库查询服务",
        description="知识库查询流程 API",
        version="1.0",
    )

    # 跨域配置
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 挂载前端静态资源：/front/chat.html
    front_dir = get_front_page_dir()
    if os.path.isdir(front_dir):
        app.mount("/front", StaticFiles(directory=front_dir))

    register_router(app)
    return app


# 全局应用实例（供 uvicorn 引用）
app = create_app()


if __name__ == '__main__':
    setup_logging()
    uvicorn.run(app, host="0.0.0.0", port=8001)

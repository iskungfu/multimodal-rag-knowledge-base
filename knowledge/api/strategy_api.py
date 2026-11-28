"""导入服务的个人策略入口；客户端不能选择 owner_id 或 doc_type。"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from knowledge.service.strategy_service import StrategyInputError, MAX_UPLOAD_BYTES

router = APIRouter(prefix="/strategies", tags=["strategies"])


async def strategy_validation_error(request, exc):
    if request.url.path == "/upload":
        return JSONResponse(status_code=422, content={"detail": "上传参数无效，请选择 PDF、Markdown 或 UTF-8 文字文件"})
    if request.url.path.startswith("/strategies"):
        # Pydantic 的默认响应含原始 input；个人文档校验失败也不能回显原文。
        return JSONResponse(status_code=422, content={"detail": "策略参数无效，请检查正文、标题和文件格式"})
    return await request_validation_exception_handler(request, exc)


class StrategyTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=200000)
    title: str = Field(default="个人策略", min_length=1, max_length=200)


def get_strategy_service(request: Request = None):
    from knowledge.runtime import runtime_for
    try:
        return runtime_for(request).strategy
    except Exception:
        raise HTTPException(503, "策略存储暂不可用") from None


def enqueue(service, background_tasks, data, filename, title=None):
    try:
        state = service.stage(data, filename, title)
    except StrategyInputError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:
        raise HTTPException(503, "策略任务保存失败，请保留输入后重试") from None
    background_tasks.add_task(service.run, state)
    return {"task_id": state["task_id"], "message": "策略已接收，正在导入"}


@router.post("/text", status_code=202)
def import_text(request: StrategyTextRequest, background_tasks: BackgroundTasks,
                service=Depends(get_strategy_service)):
    return enqueue(service, background_tasks, request.text.encode("utf-8"), "策略.md", request.title)


@router.post("/upload", status_code=202)
def import_file(file: UploadFile, background_tasks: BackgroundTasks, service=Depends(get_strategy_service)):
    try:
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        return enqueue(service, background_tasks, data, file.filename)
    finally:
        file.file.close()


@router.get("")
def list_strategies(service=Depends(get_strategy_service)):
    try:
        return {"items": service.recent()}
    except Exception:
        raise HTTPException(503, "策略存储暂不可用") from None


@router.get("/{document_id}")
def get_strategy(document_id: str, service=Depends(get_strategy_service)):
    try:
        document = service.get(document_id)
    except Exception:
        raise HTTPException(503, "策略存储暂不可用") from None
    if document is None:
        raise HTTPException(404, "策略不存在")
    return document

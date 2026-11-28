"""固定 default_user 的摘要与关注列表 API。"""
from datetime import date as calendar_date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from knowledge.market.models import SHANGHAI, market_symbol
from knowledge.service.summary_service import SummaryBusy
from knowledge.summary.repository import SummaryInputError

router = APIRouter(tags=["daily-summaries"])


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: calendar_date | None = None
    retry: bool = False


class WatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=80)


async def summary_validation_error(request, exc):
    if request.url.path.startswith("/market/collection"):
        return JSONResponse(status_code=422, content={"detail": "采集参数无效，请检查收盘日期和重试标记"})
    if request.url.path.startswith("/investor-settings"):
        return JSONResponse(status_code=422, content={"detail": "档案参数无效，请检查偏好、模式、文档编号和版本"})
    if request.url.path == "/reviews" or request.url.path.startswith("/reviews/"):
        return JSONResponse(status_code=422, content={"detail": "复盘参数无效，请检查文本长度、记录编号或查询数量"})
    if request.url.path.startswith(("/daily-summaries", "/watchlist")):
        return JSONResponse(status_code=422, content={"detail": "参数无效，请检查日期或股票完整名称/代码"})
    return await request_validation_exception_handler(request, exc)


def get_summary_service(request: Request = None):
    from knowledge.runtime import runtime_for
    try:
        return runtime_for(request).summary
    except Exception:
        raise HTTPException(503, "摘要存储暂不可用，请稍后重试") from None


def call(action):
    try:
        return action()
    except SummaryInputError as exc:
        raise HTTPException(422, str(exc)) from None
    except SummaryBusy as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, "摘要或关注列表暂不可用，请稍后重试") from None


@router.get("/watchlist")
def watchlist(service=Depends(get_summary_service)):
    return call(lambda: {"items": service.watchlist.read(service.owner)})


@router.post("/watchlist")
def add_watch(request: WatchRequest, service=Depends(get_summary_service)):
    return call(lambda: {"items": service.add_watch(request.query)})


@router.delete("/watchlist/{code}")
def delete_watch(code: str, service=Depends(get_summary_service)):
    def action():
        try:
            market_symbol(code)
        except ValueError:
            raise SummaryInputError("请提供有效的沪深京 A 股六位代码") from None
        return {"items": service.watchlist.remove(service.owner, code)}
    return call(action)


@router.post("/daily-summaries/generate", status_code=202)
def generate(request: GenerateRequest, background_tasks: BackgroundTasks, service=Depends(get_summary_service)):
    def action():
        day = request.date or service.now().astimezone(SHANGHAI).date()
        claim, record = service.begin(day, request.retry)
        if claim:
            background_tasks.add_task(service.run, claim)
        return {"record": record, "started": claim is not None}
    return call(action)


@router.get("/daily-summaries/latest")
def latest(service=Depends(get_summary_service)):
    return call(service.latest)


@router.get("/daily-summaries")
def history(limit: int = Query(30, ge=1, le=100), service=Depends(get_summary_service)):
    return call(lambda: {"items": service.repository.recent(service.owner, service.now(), limit)})


@router.get("/daily-summaries/{day}")
def get_summary(day: calendar_date, service=Depends(get_summary_service)):
    record = call(lambda: service.repository.get(service.owner, day, service.now()))
    if record is None:
        raise HTTPException(404, "该日尚无摘要记录")
    return record

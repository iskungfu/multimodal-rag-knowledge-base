"""查询服务中的复盘 API，固定单用户，不接受客户端指定 owner_id。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pymongo.errors import PyMongoError

from knowledge.review.repository import ReviewConflict
from knowledge.review.text import ReviewTextError
from knowledge.schema.review_schema import ReviewEditRequest, ReviewRequest
from knowledge.service.review_service import ReviewService

router = APIRouter(prefix="/reviews", tags=["reviews"])


def get_review_service(request: Request = None):
    from knowledge.runtime import runtime_for
    try:
        return runtime_for(request).reviews
    except Exception:
        raise HTTPException(503, "复盘存储暂不可用") from None


@router.post("", status_code=201)
def save_review(request: ReviewRequest, service: ReviewService = Depends(get_review_service)):
    try:
        return service.save(request.text, request.request_id)
    except ReviewTextError as failure:
        raise HTTPException(422, str(failure)) from None
    except ReviewConflict:
        raise HTTPException(409, "请求编号已用于其他复盘") from None
    except PyMongoError:
        raise HTTPException(503, "无法确认复盘已保存，请保留输入并使用相同请求编号重试") from None


@router.get("")
def recent_reviews(limit: int = Query(20, ge=1, le=100), service: ReviewService = Depends(get_review_service)):
    try:
        return {"items": service.recent(limit)}
    except PyMongoError:
        raise HTTPException(503, "复盘存储暂不可用") from None


@router.get("/{review_id}")
def read_review(review_id: UUID, service: ReviewService = Depends(get_review_service)):
    try:
        document = service.get(review_id)
    except PyMongoError:
        raise HTTPException(503, "复盘存储暂不可用") from None
    if document is None:
        raise HTTPException(404, "复盘不存在")
    return document


@router.post("/{review_id}/index")
def index_review(review_id: UUID, service: ReviewService = Depends(get_review_service)):
    try:
        document = service.reindex(review_id)
    except PyMongoError:
        raise HTTPException(503, "复盘存储暂不可用") from None
    if document is None:
        raise HTTPException(404, "复盘不存在")
    return document


@router.put("/{review_id}")
def edit_review(review_id: UUID, request: ReviewEditRequest, service: ReviewService = Depends(get_review_service)):
    try:
        document = service.edit(review_id, request.text, request.revision, request.request_id)
    except ReviewTextError as failure:
        raise HTTPException(422, str(failure)) from None
    except ReviewConflict:
        raise HTTPException(409, "复盘已被更新或请求编号已使用，请保留修改内容并刷新核对版本") from None
    except PyMongoError:
        raise HTTPException(503, "无法确认修改已保存，请保留输入并使用相同请求编号重试") from None
    if document is None:
        raise HTTPException(404, "复盘不存在")
    return document

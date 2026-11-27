"""查询服务中的复盘 API，固定单用户，不接受客户端指定 owner_id。"""

import os
from functools import cache
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from knowledge.review.repository import ReviewConflict, ReviewRepository
from knowledge.schema.review_schema import ReviewRequest
from knowledge.service.review_service import ReviewService

router = APIRouter(prefix="/reviews", tags=["reviews"])


@cache
def get_review_service():
    # 独立轻量客户端，复盘无需导入 Milvus/embedding 或连接其他存储。
    client = None
    try:
        client = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000,
                             connectTimeoutMS=5000, socketTimeoutMS=5000, tz_aware=True)
        return ReviewService(ReviewRepository(client[os.getenv("MONGO_DB_NAME", "kb001")]["trade_reviews"]))
    except (KeyError, ValueError, PyMongoError):
        if client is not None:
            client.close()
        raise HTTPException(503, "复盘存储暂不可用") from None


@router.post("", status_code=201)
def save_review(request: ReviewRequest, service: ReviewService = Depends(get_review_service)):
    try:
        return service.save(request.text, request.request_id)
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

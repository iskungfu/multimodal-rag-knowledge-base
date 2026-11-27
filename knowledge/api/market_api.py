"""查询服务的收盘行情路由；同步函数由 FastAPI 放入线程池。"""

import os
from datetime import date
from functools import cache

from fastapi import APIRouter, Depends, HTTPException
from pymongo.errors import PyMongoError

from knowledge.market.models import MarketUnavailable
from knowledge.market.repository import MarketQuoteRepository
from knowledge.service.market_service import MarketService

router = APIRouter(prefix="/market/quotes", tags=["market"])


@cache
def get_market_service():
    # 延迟使用已有 MongoDB 客户端，不依赖模型加载或连接其他存储。
    from knowledge.utils.clients.storage_clients import StorageClients

    try:
        client = StorageClients.get_mongo_client()
        repository = MarketQuoteRepository(client[os.getenv("MONGO_DB_NAME", "kb001")]["market_quotes"])
        return MarketService(repository)
    except (PyMongoError, EnvironmentError, ConnectionError):
        raise HTTPException(503, "行情存储暂不可用") from None


def execute(action, code, start, end):
    try:
        return action(code, start, end)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except MarketUnavailable as error:
        raise HTTPException(503, str(error), headers={"Retry-After": "60"}) from None
    except PyMongoError:
        raise HTTPException(503, "行情存储暂不可用") from None


@router.post("/{code}/refresh")
def refresh_quotes(code: str, start: date | None = None, end: date | None = None,
                   service: MarketService = Depends(get_market_service)):
    return execute(service.refresh, code, start, end)


@router.get("/{code}")
def get_quotes(code: str, start: date | None = None, end: date | None = None,
               service: MarketService = Depends(get_market_service)):
    return execute(service.read, code, start, end)

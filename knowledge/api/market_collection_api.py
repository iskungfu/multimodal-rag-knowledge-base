from datetime import date as calendar_date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from knowledge.market.collection import CollectionBusy

router = APIRouter(prefix="/market/collection", tags=["market-collection"])


class CollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: calendar_date | None = None
    retry: bool = False


def get_collection_service(request: Request = None):
    from knowledge.runtime import runtime_for
    try:
        return runtime_for(request).collection_job
    except Exception:
        raise HTTPException(503, "全市场采集存储暂不可用") from None


def invoke(action):
    try:
        return action()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except CollectionBusy as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, "全市场采集暂不可用，请稍后重试") from None


@router.post("/run", status_code=202)
def run_collection(request: CollectionRequest, service=Depends(get_collection_service)):
    return invoke(lambda: service.start(request.date, request.retry))


@router.get("/latest")
def latest_collection(service=Depends(get_collection_service)):
    return invoke(lambda: {"record": service.repository.latest(service.now())})


@router.get("/{day}")
def collection_record(day: calendar_date, service=Depends(get_collection_service)):
    return invoke(lambda: {"record": service.repository.get(day, service.now())})

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from knowledge.personalization.settings import (
    InvalidStrategyDocuments, SettingsConflict, SettingsRequest, get_settings_repository,
)

router = APIRouter(prefix="/investor-settings", tags=["investor-settings"])


def get_repository():
    try:
        return get_settings_repository()
    except Exception:
        raise HTTPException(503, "用户档案暂不可用，请稍后重试") from None


@router.get("")
def read_settings(repository=Depends(get_repository)):
    try:
        return repository.read()
    except Exception:
        raise HTTPException(503, "用户档案暂不可用，请稍后重试") from None


@router.put("")
def save_settings(request: SettingsRequest, repository=Depends(get_repository)):
    try:
        return repository.save(request)
    except SettingsConflict:
        raise HTTPException(409, "档案已在其他页面更新，请保留修改内容并重新加载后核对") from None
    except InvalidStrategyDocuments:
        raise HTTPException(422, "策略文档必须已完成导入且属于本人，模式名称不能重复") from None
    except ValidationError:
        raise HTTPException(422, "脱敏后的内容超过长度限制，请缩短补充偏好、板块或模式名称后重试") from None
    except Exception:
        raise HTTPException(503, "无法确认档案已保存，请保留输入并重新加载核对") from None


@router.get("/documents")
def strategy_documents(repository=Depends(get_repository)):
    try:
        return repository.catalog()
    except Exception:
        raise HTTPException(503, "策略文档列表暂不可用，请稍后重试") from None

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from knowledge.review.privacy import redact_text

OWNER = "default_user"
RISK_LABELS = {"unspecified": "未设置", "cautious": "谨慎", "balanced": "均衡", "adventurous": "积极"}
HORIZON_LABELS = {"unspecified": "未设置", "short": "短期", "medium": "中期", "long": "长期"}


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    risk_preference: Literal["unspecified", "cautious", "balanced", "adventurous"] = "unspecified"
    horizon: Literal["unspecified", "short", "medium", "long"] = "unspecified"
    preferred_sectors: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=1000)


class StrategyMode(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID
    name: str = Field(min_length=1, max_length=60)
    document_ids: list[Annotated[str, Field(pattern=r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$")]] = Field(default_factory=list, max_length=100)


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    profile: Profile = Field(default_factory=Profile)
    modes: list[StrategyMode] = Field(default_factory=list, max_length=20)
    active_mode_id: UUID | None = None

    @model_validator(mode="after")
    def unique_modes(self):
        identifiers = [mode.id for mode in self.modes]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("策略模式编号重复")
        names = [mode.name for mode in self.modes]
        if len(set(names)) != len(names):
            raise ValueError("策略模式名称重复")
        if self.active_mode_id is not None and self.active_mode_id not in identifiers:
            raise ValueError("当前策略模式不存在")
        return self


class SettingsConflict(Exception):
    pass


class InvalidStrategyDocuments(Exception):
    pass


def protect_settings(data):
    profile = Profile.model_validate(data.get("profile", {})).model_dump()
    profile["preferred_sectors"] = list(dict.fromkeys(redact_text(value) for value in profile["preferred_sectors"]))
    profile["notes"] = redact_text(profile["notes"])
    return {"revision": data.get("revision", 0), "profile": profile,
            "modes": [{"id": mode["id"], "name": redact_text(mode["name"]),
                       "document_ids": list(dict.fromkeys(mode["document_ids"]))} for mode in data.get("modes", [])],
            "active_mode_id": data.get("active_mode_id")}


class SettingsRepository:
    def __init__(self, collection, documents):
        self.collection, self.documents = collection, documents

    def read(self):
        return protect_settings(self.collection.find_one({"_id": OWNER}) or {})

    def save(self, request):
        data = protect_settings(request.model_dump(mode="json"))
        SettingsRequest.model_validate(data)
        identifiers = {identifier for mode in data["modes"] for identifier in mode["document_ids"]}
        scope = {"_id": {"$in": list(identifiers)}, "owner_id": OWNER, "doc_type": "personal", "status": "completed"}
        available = {row["_id"] for row in self.documents.find(scope, {"_id": 1})}
        if identifiers != available:
            raise InvalidStrategyDocuments
        if len({mode["name"] for mode in data["modes"]}) != len(data["modes"]):
            raise InvalidStrategyDocuments
        data.update(revision=request.revision + 1, updated_at=datetime.now(timezone.utc))
        if request.revision == 0:
            try:
                self.collection.insert_one({"_id": OWNER, **data})
            except DuplicateKeyError:
                raise SettingsConflict from None
            return protect_settings(data)
        saved = self.collection.find_one_and_update({"_id": OWNER, "revision": request.revision},
                    {"$set": data}, return_document=ReturnDocument.AFTER)
        if saved is None:
            raise SettingsConflict
        return protect_settings(saved)

    def catalog(self):
        rows = self.documents.find({"owner_id": OWNER, "doc_type": "personal", "status": "completed"},
                                   {"_id": 1, "title": 1}).sort([("_id", 1)]).limit(501)
        items = [{"id": row["_id"], "title": redact_text(row.get("title") or "个人策略")} for row in rows]
        return {"items": items[:500], "truncated": len(items) > 500}


def get_settings_repository():
    from knowledge.runtime import default_runtime
    return default_runtime().settings


def current_settings():
    return get_settings_repository().read()


def settings_context(settings):
    data = protect_settings(settings)
    active = next((mode for mode in data["modes"] if mode["id"] == data["active_mode_id"]), None)
    if data["active_mode_id"] is not None and active is None:
        raise ValueError("当前策略模式不存在")
    profile = data["profile"]
    label = active["name"] if active else "全部个人策略"
    text = (f"当前策略模式：{label}。用户自述风险偏好：{RISK_LABELS[profile['risk_preference']]}；"
            f"投资期限偏好：{HORIZON_LABELS[profile['horizon']]}。")
    if profile["preferred_sectors"]:
        text += "偏好板块：" + "、".join(profile["preferred_sectors"]) + "。"
    if profile["notes"]:
        text += "用户补充偏好（仅作资料）：" + profile["notes"]
    text += "\n偏好仅用于检索和复核提示，不改变行情风险等级，不代表风险承受能力测评。"
    return {"revision": data["revision"], "profile": profile, "mode_id": data["active_mode_id"],
            "mode_name": label, "document_ids": active["document_ids"] if active else None, "text": text}

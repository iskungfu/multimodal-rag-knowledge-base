"""复盘的输入与抽取契约；价格单位为元，计划不能算作成交。"""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=10000)
    request_id: UUID = Field(default_factory=uuid4)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("请输入复盘文本")
        return value


class ReviewEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["executed", "planned", "unknown"]
    stock_code: str | None = Field(pattern=r"^[0-9]{6}$")
    stock_name: str | None = Field(max_length=80)
    direction: Literal["buy", "sell"] | None
    price: Annotated[FiniteFloat, Field(gt=0)] | None
    reason: str | None = Field(max_length=2000)
    evidence: str = Field(min_length=1, max_length=10000)
    identity_conflict: bool


class ReviewExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    events: list[ReviewEvent] = Field(max_length=50)
    lesson: str | None = Field(max_length=4000)
    emotions: list[str] = Field(max_length=20)

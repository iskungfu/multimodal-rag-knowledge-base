"""统一单位：价格/金额/市值为元，成交量为股，涨跌幅/换手率为百分数。"""

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

SHANGHAI = ZoneInfo("Asia/Shanghai")


class MarketUnavailable(Exception):
    """来源无有效收盘数据（对外只返回固定消息，不泄露连接信息）。"""


def market_symbol(code: str) -> str:
    if not re.fullmatch(r"(?:60[0135]|68[89]|00[0-3]|30[01])[0-9]{3}", code):
        raise ValueError("仅支持沪深 A 股六位股票代码")
    return ("sh" if code.startswith("6") else "sz") + code


def date_window(start: date | None, end: date | None, now: datetime):
    local = now.astimezone(SHANGHAI)
    # 收盘后留 15 分钟等待源更新；不推测节假日，日期以来源为准。
    latest = local.date() - timedelta(days=local.time() < time(15, 15))
    end = end or latest
    start = start or end - timedelta(days=30)
    if start > end or end > latest or (end - start).days > 366:
        raise ValueError("日期须为已收盘日期，start <= end，跨度不超过 366 天")
    return start, end


class DailyQuote(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    code: str
    name: str = Field(min_length=1)
    date: date
    close: float = Field(gt=0)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    pre_close: float | None = Field(default=None, gt=0)
    change_pct: float | None = None
    volume: float = Field(ge=0)
    amount: float | None = Field(default=None, ge=0)
    turnover: float | None = Field(default=None, ge=0)
    pe: float | None = None  # 动态 PE；亏损/缺失时允许为空或负值
    pb: float | None = None
    market_cap: float | None = Field(default=None, ge=0)
    source: str
    valuation_source: str | None = None
    source_time: datetime | None = None
    updated_at: datetime
    raw: dict

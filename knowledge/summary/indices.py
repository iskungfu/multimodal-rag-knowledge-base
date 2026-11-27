"""指数专用适配，不改变六位 A 股代码的语义。"""
import json
import math
import statistics
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field

from knowledge.market.adapters import MarketHTTP, PARSE_ERRORS, number
from knowledge.market.models import MarketUnavailable, SHANGHAI, date_window
from knowledge.risk.rules import at_least, finite

INDICES = {"sh000001": ("1.000001", "上证指数"), "sz399001": ("0.399001", "深证成指")}
N = 20
RETURN_THRESHOLDS = (5, 10)
VOLUME_THRESHOLDS = (1.5, 2)


class IndexQuote(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    symbol: str
    name: str
    date: date
    close: float = Field(gt=0)
    change_pct: float
    volume: float = Field(ge=0)
    amount: float = Field(ge=0)
    source: str
    updated_at: datetime
    source_time: datetime | None = None
    raw: dict


class EastmoneyIndexAdapter:
    name = "eastmoney"

    def __init__(self, http=None):
        self.http = http or MarketHTTP()

    def fetch_daily(self, symbol, start, end, now):
        secid, name = INDICES[symbol]
        start, end = date_window(start, end, now)
        try:
            payload = json.loads(self.http.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", {
                "secid": secid, "klt": "101", "fqt": "0", "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"), "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            }).decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise ValueError
            data = payload["data"]
            if (payload.get("rc") != 0 or data["code"] != symbol[2:]
                    or str(data["market"]) != secid[0] or data["name"] != name
                    or not isinstance(data.get("klines"), list)):
                raise ValueError
            rows = []
            for line in data["klines"]:
                if not isinstance(line, str):
                    raise ValueError
                fields = line.split(",")
                day = date.fromisoformat(fields[0])
                if start <= day <= end:
                    rows.append(IndexQuote(
                        symbol=symbol, name=name, date=day, close=number(fields[2]),
                        change_pct=number(fields[8]), volume=number(fields[5], 100),
                        amount=number(fields[6]), source=self.name, updated_at=now,
                        raw={"kline": line, "secid": secid, "name": name},
                    ))
            return rows
        except (AttributeError, *PARSE_ERRORS):
            raise MarketUnavailable("指数日线身份或数据无效") from None


class TencentIndexAdapter:
    name = "tencent"

    def __init__(self, http=None):
        self.http = http or MarketHTTP()

    def fetch_daily(self, symbol, start, end, now):
        _, name = INDICES[symbol]
        start, end = date_window(start, end, now)
        try:
            raw = self.http.get("https://qt.gtimg.cn/q=" + symbol).decode("gbk")
            prefix, body = raw.split('="', 1)
            fields = body.split('"', 1)[0].split("~")
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
            if (prefix.strip() != "v_" + symbol or fields[1] != name or fields[2] != symbol[2:]
                    or stamp.time() < time(15) or stamp > now):
                raise ValueError
            if not start <= stamp.date() <= end:
                return []
            return [IndexQuote(
                symbol=symbol, name=name, date=stamp.date(), close=number(fields[3]),
                change_pct=number(fields[32]), volume=number(fields[6], 100),
                amount=number(fields[57], 10000), source=self.name, source_time=stamp,
                updated_at=now, raw={"snapshot": raw},
            )]
        except (AttributeError, *PARSE_ERRORS):
            raise MarketUnavailable("指数快照身份或数据无效") from None


class IndexRepository:
    def __init__(self, collection):
        self.collection = collection
        collection.create_index([("symbol", 1), ("date", 1)], unique=True)

    def save(self, quotes):
        from pymongo.errors import DuplicateKeyError
        for quote in quotes:
            row = quote.model_dump()
            row["date"] = quote.date.isoformat()
            key = {"symbol": quote.symbol, "date": row["date"]}
            try:
                self.collection.update_one(key, {"$set": row}, upsert=True)
            except DuplicateKeyError:
                self.collection.update_one(key, {"$set": row})

    def read(self, symbol, start, end):
        return list(self.collection.find({"symbol": symbol, "date": {
            "$gte": start.isoformat(), "$lte": end.isoformat(),
        }}, {"_id": 0, "raw": 0}).sort("date", 1))


class IndexService:
    def __init__(self, repository, adapters=None):
        self.repository = repository
        self.adapters = adapters if adapters is not None else [EastmoneyIndexAdapter(), TencentIndexAdapter()]

    def fetch(self, symbol, start, end, now):
        for adapter in self.adapters:
            try:
                quotes = adapter.fetch_daily(symbol, start, end, now)
                if not quotes:
                    continue
                by_day = {}
                for quote in quotes:
                    if quote.date in by_day and quote != by_day[quote.date]:
                        raise MarketUnavailable("指数同日行情冲突")
                    by_day[quote.date] = quote
                self.repository.save(quotes)
                if end in by_day:
                    rows = self.repository.read(symbol, start, end)
                    return rows, (["指数备用源仅提供单日快照；历史依据以已存日线为准"]
                                  if adapter.name != "eastmoney" else [])
            except MarketUnavailable:
                continue
        rows = self.repository.read(symbol, start, end)
        if any(row["date"] == end.isoformat() for row in rows):
            return rows, ["指数源未返回目标日更新，使用已核实的目标日收盘缓存"]
        raise MarketUnavailable("双指数目标日收盘数据尚未核实，可能休市、行情延迟或来源不可用；可稍后重试")


def evaluate_index(rows, symbol, day):
    """N 个源日收益复合涨跌幅及当日量/此前 N 日均量；不能用快照凑历史。"""
    unique = {}
    for row in rows:
        parsed = IndexQuote.model_validate({**row, "raw": row.get("raw", {})})
        if parsed.symbol != symbol or parsed.name != INDICES[symbol][1] or parsed.date > day:
            raise MarketUnavailable("指数身份或日期异常")
        if parsed.date in unique and any(unique[parsed.date][field] != row[field]
                                        for field in ("close", "change_pct", "volume")):
            raise MarketUnavailable("指数同日行情冲突")
        unique[parsed.date] = row
    ordered = [unique[key] for key in sorted(unique)]
    if not ordered or ordered[-1]["date"] != day.isoformat():
        raise MarketUnavailable("指数缺少目标日收盘数据")
    latest = ordered[-1]
    change = ratio = None
    if len(ordered) >= N + 1:
        returns = [row["change_pct"] for row in ordered[-N:]]
        if all(value > -100 for value in returns):
            change = finite(100 * (math.prod(1 + value / 100 for value in returns) - 1))
        average = statistics.mean(row["volume"] for row in ordered[-N-1:-1])
        if average > 0:
            ratio = finite(latest["volume"] / average)
    scores = []
    for value, thresholds in ((abs(change) if change is not None else None, RETURN_THRESHOLDS),
                              (ratio, VOLUME_THRESHOLDS)):
        if value is not None:
            scores.append(2 if at_least(value, thresholds[1]) else 1 if at_least(value, thresholds[0]) else 0)
    if ratio is not None and ratio <= 0.5:
        scores.append(2 if ratio == 0 else 1)
    limited = change is None or ratio is None or latest["volume"] <= 0
    score = max(scores, default=0)
    level = ("低", "中", "高")[score] if scores and (score or not limited) else None
    return {"symbol": symbol, "name": latest["name"], "date": latest["date"], "level": level,
            "limited": limited, "return_pct": change, "volume_ratio": ratio, "sample_count": len(ordered),
            "latest": latest, "warnings": ["历史不足或数据有限，无法完整评级；无数据不代表低风险"] if limited else []}


def rule_note():
    return (f"指数初始规则 v1，未经回测：N={N}；近 N 日源涨跌幅复合收益的绝对值达到 "
            f"{RETURN_THRESHOLDS[0]}%/{RETURN_THRESHOLDS[1]}% 为中/高；当日量/此前 N 日均量达到 "
            f"{VOLUME_THRESHOLDS[0]}/{VOLUME_THRESHOLDS[1]} 倍为中/高，≤0.5 倍为中、零量比为高；取最高档。"
            "点位单位为点，成交量为股，金额为元，涨跌幅为百分数。不判断市场阶段，不预测涨跌。")

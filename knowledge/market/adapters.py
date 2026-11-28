"""免费行情源。历史日线与快照分别解析，快照只补充同一收盘日。"""

import json
import threading
import time
from datetime import date, datetime, time as wall_time
from decimal import Decimal, InvalidOperation
from typing import Protocol

import requests
from pydantic import ValidationError

from knowledge.market.models import DailyQuote, MarketUnavailable, SHANGHAI, date_window, market_symbol


def number(value, multiplier=1):
    if value is None or value in ("", "-", "--"):
        return None
    result = Decimal(str(value)) * Decimal(str(multiplier))
    if not result.is_finite():
        raise ValueError("非有限行情值")
    return float(result)


def tencent_volume_multiplier(code):
    return 1 if code.startswith(("688", "689")) else 100


class QuoteAdapter(Protocol):
    name: str

    def fetch_daily(self, code: str, start: date, end: date, now: datetime) -> list[DailyQuote]: ...


class MarketHTTP:
    """每个源串行限速、最多两次尝试；失败冷却，避免短时间反复请求。"""

    def __init__(self, session=None, *, interval=1.0, cooldown=60.0,
                 clock=time.monotonic, sleep=time.sleep):
        self.session = session or requests.Session()
        self.interval, self.cooldown = interval, cooldown
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.next_request = self.blocked_until = 0.0

    def get(self, url, params=None):
        with self.lock:
            if self.clock() < self.blocked_until:
                raise MarketUnavailable("行情源冷却中")
            for attempt in range(2):
                self.sleep(max(0, self.next_request - self.clock()))
                try:
                    response = self.session.get(
                        url, params=params, timeout=(3, 8),
                        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
                    )
                    response.raise_for_status()
                    return response.content
                except requests.RequestException:
                    # 不记录异常字符串，代理和连接 URL 可能含凭证。
                    if attempt == 1:
                        self.blocked_until = self.clock() + self.cooldown
                        raise MarketUnavailable("行情源请求失败") from None
                    self.sleep(1.0)
                finally:
                    self.next_request = self.clock() + self.interval


PARSE_ERRORS = (ValueError, TypeError, KeyError, IndexError, OverflowError, OSError, InvalidOperation, ValidationError)


class EastmoneyAdapter:
    name = "eastmoney"

    def __init__(self, http=None):
        self.http = http or MarketHTTP()

    def snapshot(self, code: str, now: datetime) -> dict:
        secid = ("1." if market_symbol(code).startswith("sh") else "0.") + code
        try:
            raw = json.loads(self.http.get("https://push2.eastmoney.com/api/qt/stock/get", {
                "secid": secid, "fltt": "1",
                "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f86,f116,f162,f167,f168,f169,f170",
            }).decode("utf-8"))
            data = raw["data"]
            if raw.get("rc") != 0 or data["f57"] != code:
                raise ValueError("代码或状态不匹配")
            stamp = datetime.fromtimestamp(data["f86"], SHANGHAI)
            if stamp.time() < wall_time(15) or stamp > now:
                raise ValueError("不是有效收盘快照")
            precision = int(data["f59"])
            if not 0 <= precision <= 4:
                raise ValueError("价格精度无效")
            return {
                "date": stamp.date(), "close": number(data["f43"], Decimal(10) ** -precision),
                "change_pct": number(data.get("f170"), "0.01"),
                "turnover": number(data.get("f168"), "0.01"),
                "pe": number(data.get("f162"), "0.01"), "pb": number(data.get("f167"), "0.01"),
                "market_cap": number(data.get("f116")),
                "valuation_source": self.name, "source_time": stamp, "raw_snapshot": raw,
            }
        except PARSE_ERRORS:
            raise MarketUnavailable("东方财富快照格式无效") from None

    def fetch_daily(self, code, start, end, now):
        start, end = date_window(start, end, now)
        secid = ("1." if market_symbol(code).startswith("sh") else "0.") + code
        try:
            raw = json.loads(self.http.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", {
                "secid": secid, "klt": "101", "fqt": "0",
                "beg": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
                "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            }).decode("utf-8"))
            data = raw["data"]
            if raw.get("rc") != 0 or data["code"] != code:
                raise ValueError("代码或状态不匹配")
            if not isinstance(data["klines"], list):
                raise ValueError("日线列表无效")
            quotes = []
            for row in data["klines"]:
                if not isinstance(row, str):
                    raise ValueError("日线记录无效")
                fields = row.split(",")
                day = date.fromisoformat(fields[0])
                if not start <= day <= end:
                    continue
                # 日线 f52..f61 已是十进制单位，f56 为手；f60 为涨跌额。
                pre_close = (float(Decimal(fields[2]) - Decimal(fields[9]))
                             if fields[9] not in ("", "-", "--") else None)
                quotes.append(DailyQuote(
                    code=code, name=data["name"], date=day,
                    open=number(fields[1]), close=number(fields[2]), high=number(fields[3]),
                    low=number(fields[4]), volume=number(fields[5], 100),
                    amount=number(fields[6]), change_pct=number(fields[8]), turnover=number(fields[10]),
                    pre_close=pre_close, source=self.name, updated_at=now,
                    raw={"kline": row, "code": code, "name": data["name"], "decimal": data["decimal"], "fqt": 0},
                ))
        except PARSE_ERRORS:
            raise MarketUnavailable("东方财富日线格式无效") from None
        if not quotes:
            raise MarketUnavailable("日期范围内无日线")
        # 历史日线不提供历史估值；只允许同日期、同收盘价的快照补充。
        try:
            snapshot = self.snapshot(code, now)
            for quote in quotes:
                if quote.date == snapshot["date"] and quote.close == snapshot["close"]:
                    quote.raw["snapshot"] = snapshot["raw_snapshot"]
                    for key in ("pe", "pb", "market_cap", "valuation_source", "source_time"):
                        setattr(quote, key, snapshot[key])
        except MarketUnavailable:
            pass  # 有效日线仍可保存；估值缺失在服务响应中明确提示。
        return quotes


class TencentAdapter:
    """备用源仅提供最新已收盘日；不将快照伪造为历史日线。"""

    name = "tencent"

    def __init__(self, http=None):
        self.http = http or MarketHTTP()

    def fetch_daily(self, code, start, end, now):
        start, end = date_window(start, end, now)
        symbol = market_symbol(code)
        try:
            text = self.http.get(f"https://qt.gtimg.cn/q={symbol}").decode("gbk")
            return [self.parse_snapshot(text, code, start, end, now)]
        except PARSE_ERRORS:
            raise MarketUnavailable("腾讯无有效收盘快照") from None

    def parse_snapshot(self, text, code, start, end, now):
        symbol = market_symbol(code)
        try:
            if not text.startswith(f'v_{symbol}="'):
                raise ValueError("代码不匹配")
            fields = text.split('"')[1].split("~")
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
            if fields[2] != code or not start <= stamp.date() <= end or stamp > now or stamp.time() < wall_time(15):
                raise ValueError("范围内无收盘快照")
            return DailyQuote(
                code=code, name=fields[1], date=stamp.date(), close=number(fields[3]),
                pre_close=number(fields[4]), open=number(fields[5]), high=number(fields[33]), low=number(fields[34]),
                volume=number(fields[6], tencent_volume_multiplier(code)), amount=number(fields[57], 10000),
                change_pct=number(fields[32]), turnover=number(fields[38]),
                pe=number(fields[52]), pb=number(fields[46]), market_cap=number(fields[45], 100000000),
                source=self.name, valuation_source=self.name, source_time=stamp, updated_at=now,
                raw={"snapshot": text, "volume_multiplier": tencent_volume_multiplier(code)},
            )
        except PARSE_ERRORS:
            raise MarketUnavailable("腾讯无有效收盘快照") from None

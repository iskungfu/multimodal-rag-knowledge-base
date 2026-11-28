"""BaoStock独立日线来源，受限子进程避免SDK全局连接阻塞或串线。"""

from datetime import date
from functools import cache
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from knowledge.market.adapters import PARSE_ERRORS, TencentAdapter, number
from knowledge.market.models import DailyQuote, MarketUnavailable, date_window, market_symbol, security_name, validate_quote_batch


class BaoStockGateway:
    def __init__(self, *, runner=subprocess.run, clock=time.monotonic, sleep=time.sleep):
        self.runner, self.clock, self.sleep = runner, clock, sleep
        self.lock = threading.Lock()
        self.next_request = self.blocked_until = 0.0

    def request(self, action, start, end, code=None):
        if not self.lock.acquire(timeout=2):
            raise MarketUnavailable("BaoStock来源正在处理其他请求")
        try:
            if self.clock() < self.blocked_until:
                raise MarketUnavailable("BaoStock来源冷却中")
            self.sleep(max(0, self.next_request - self.clock()))
            try:
                result = self.runner(
                    [sys.executable, "-X", "utf8", "-m", "knowledge.market.baostock_worker"],
                    input=json.dumps({"action": action, "start": start.isoformat(), "end": end.isoformat(), "code": code}),
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                    cwd=str(Path(__file__).resolve().parents[2]),
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                if result.returncode != 0 or len(result.stdout) > 2_000_000:
                    raise ValueError("来源进程失败")
                payload = json.loads(result.stdout)
                if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
                    raise ValueError("来源响应失败")
                return payload["data"]
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                self.blocked_until = self.clock() + 60
                raise MarketUnavailable("BaoStock来源暂不可用") from None
            finally:
                self.next_request = self.clock() + 1
        finally:
            self.lock.release()


@cache
def get_baostock_gateway():
    return BaoStockGateway()


class BaoStockAdapter:
    name = "baostock"

    def __init__(self, gateway=None, snapshot=None):
        self.gateway = gateway or get_baostock_gateway()
        self.snapshot = snapshot or TencentAdapter()

    def fetch_daily(self, code, start, end, now):
        start, end = date_window(start, end, now)
        symbol = market_symbol(code)
        if symbol.startswith("bj"):
            raise MarketUnavailable("BaoStock北交所覆盖未验证，继续其他来源")
        symbol = symbol[:2] + "." + symbol[2:]
        payload = self.gateway.request("daily", start, end, code)
        try:
            identity, rows = payload["identity"], payload["quotes"]
            if identity["code"] != symbol or identity["type"] != "1" or not isinstance(identity["code_name"], str) or not identity["code_name"].strip():
                raise ValueError("证券身份无效")
            if not isinstance(rows, list) or not 0 < len(rows) <= 367:
                raise ValueError("日线数量无效")
            quotes = []
            for row in rows:
                day = date.fromisoformat(row["date"])
                if row["code"] != symbol or row["adjustflag"] != "3" or row["tradestatus"] not in {"0", "1"} or not start <= day <= end:
                    raise ValueError("日线身份、复权或日期无效")
                quote = DailyQuote(
                    code=code, name=identity["code_name"], date=day,
                    open=number(row["open"]), close=number(row["close"]), high=number(row["high"]), low=number(row["low"]),
                    pre_close=number(row["preclose"]), volume=number(row["volume"]), amount=number(row["amount"]),
                    turnover=number(row["turn"]), change_pct=number(row["pctChg"]), pb=number(row["pbMRQ"]),
                    source=self.name, valuation_source=self.name if number(row["pbMRQ"]) is not None else None,
                    updated_at=now, raw={"baostock": row, "identity": identity, "fqt": 0},
                )
                if not quote.low <= min(quote.open, quote.close) <= max(quote.open, quote.close) <= quote.high:
                    raise ValueError("日线价格范围无效")
                if row["tradestatus"] == "0" and (quote.volume != 0 or quote.amount not in (None, 0)):
                    raise ValueError("停牌状态与成交冲突")
                quotes.append(quote)
            quotes = sorted(validate_quote_batch(quotes, code, start, end), key=lambda quote: quote.date)
        except (AttributeError, *PARSE_ERRORS):
            raise MarketUnavailable("BaoStock日线身份或数据无效") from None
        try:
            latest = quotes[-1]
            snapshot = self.snapshot.fetch_daily(code, latest.date, latest.date, now)[0]
            if (snapshot.code == code and snapshot.date == latest.date and security_name(snapshot.name) == security_name(latest.name)
                    and all(getattr(latest, field) == getattr(snapshot, field) for field in ("open", "close", "high", "low"))
                    and abs(latest.volume - snapshot.volume) <= 100
                    and latest.amount is not None and snapshot.amount is not None and abs(latest.amount - snapshot.amount) <= 1):
                valuations = {field: getattr(snapshot, field) for field in ("pe", "pb", "market_cap")
                              if getattr(snapshot, field) is not None}
                if valuations:
                    sources = {"pb": self.name} if latest.pb is not None else {}
                    for field, value in valuations.items():
                        setattr(latest, field, value)
                        sources[field] = snapshot.valuation_source or snapshot.source
                    latest.valuation_source = "+".join(sorted(set(sources.values())))
                    latest.source_time = snapshot.source_time
                    latest.raw["valuation_snapshot"] = snapshot.raw
                    latest.raw["valuation_sources"] = sources
        except (MarketUnavailable, *PARSE_ERRORS):
            pass
        return quotes

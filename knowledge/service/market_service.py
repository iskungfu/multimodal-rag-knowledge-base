"""单次行情抓取与存储，供查询 API 和后续收盘调度复用。"""

import threading
import time
from datetime import datetime, timezone

from knowledge.market.adapters import EastmoneyAdapter, TencentAdapter
from knowledge.market.models import MarketUnavailable, date_window, market_symbol, validate_quote_batch
from knowledge.market.tencent_history import TencentHistoryAdapter


class MarketService:
    def __init__(self, repository, adapters=None, *, now=None, clock=time.monotonic):
        self.repository = repository
        self.adapters = adapters if adapters is not None else [EastmoneyAdapter(), TencentHistoryAdapter(), TencentAdapter()]
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.clock = clock
        self.lock = threading.Lock()
        self.cache = {}

    def refresh(self, code, start=None, end=None):
        market_symbol(code)
        now = self.now()
        start, end = date_window(start, end, now)
        key = (code, start, end)
        with self.lock:
            self.cache = {k: v for k, v in self.cache.items() if v[0] > self.clock()}
            if key in self.cache:
                return {**self.cache[key][1], "cached": True}
            for adapter in self.adapters:
                try:
                    quotes = adapter.fetch_daily(code, start, end, now)
                    quotes = validate_quote_batch(quotes, code, start, end)
                except MarketUnavailable:
                    continue
                if not quotes:
                    continue
                self.repository.save(quotes)
                warnings = []
                if adapter.name == "tencent":
                    warnings.append("主源不可用，备用源仅返回最新已收盘日，历史区间可能不完整")
                elif adapter.name != "eastmoney":
                    warnings.append("主源不可用，已切换备用历史日线，来源覆盖可能不完整")
                if any(q.pe is None for q in quotes):
                    warnings.append("部分日期没有可核实的动态 PE，缺失估值保留为空")
                if any(q.change_pct is None for q in quotes):
                    warnings.append("部分日期缺少来源涨跌幅，不使用未复权相邻收盘价猜测，风险评估可能受限")
                if max(q.date for q in quotes) < end:
                    warnings.append("来源最新日期早于请求结束日期，请确认休市、停牌或来源延迟")
                result = {"code": code, "start": start, "end": end, "source": adapter.name,
                          "cached": False, "warnings": warnings,
                          "quotes": self.repository.read(code, start, end)}
                self.cache[key] = (self.clock() + 300, result)
                return result
            raise MarketUnavailable("行情源暂不可用或范围内无已收盘数据，请稍后重试")

    def read(self, code, start=None, end=None):
        market_symbol(code)
        start, end = date_window(start, end, self.now())
        return {"code": code, "start": start, "end": end,
                "quotes": self.repository.read(code, start, end)}

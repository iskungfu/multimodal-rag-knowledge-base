"""带来源、缓存及未知状态的交易日历，仅确定休市时跳过自动任务。"""

from datetime import date, datetime, timezone
from functools import cache
import threading
import time

from knowledge.market.baostock_source import get_baostock_gateway
from knowledge.market.models import MarketUnavailable, SHANGHAI


class TradingCalendar:
    def __init__(self, gateway=None, *, now=None, clock=time.monotonic):
        self.gateway = gateway or get_baostock_gateway()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.clock = clock
        self.lock = threading.Lock()
        self.cache = {}
        self.blocked_until = 0.0

    def status(self, day=None):
        now = self.now()
        day = day or now.astimezone(SHANGHAI).date()
        if abs((day - now.astimezone(SHANGHAI).date()).days) > 366:
            raise ValueError("交易日历仅查询当前日期前后366天")
        if not self.lock.acquire(timeout=2):
            return self.unavailable(day, now)
        try:
            self.cache = {year: item for year, item in self.cache.items() if item[0] > self.clock()}
            if day.year in self.cache:
                _, observed, days = self.cache[day.year]
                return self.present(day, days[day], observed, True)
            if self.clock() < self.blocked_until:
                return self.unavailable(day, now)
            try:
                start, end = date(day.year, 1, 1), date(day.year, 12, 31)
                payload = self.gateway.request("calendar", start, end)
                rows = payload["calendar"]
                if not isinstance(rows, list) or len(rows) != (end - start).days + 1:
                    raise ValueError("日历范围缺失")
                days = {}
                for row in rows:
                    current = date.fromisoformat(row["calendar_date"])
                    if current in days or not start <= current <= end or row["is_trading_day"] not in {"0", "1"}:
                        raise ValueError("日历日期或状态无效")
                    days[current] = row["is_trading_day"] == "1"
                self.cache[day.year] = (self.clock() + 86400, now, days)
                return self.present(day, days[day], now, False)
            except (MarketUnavailable, KeyError, ValueError, TypeError) as error:
                if not isinstance(error, MarketUnavailable):
                    self.blocked_until = self.clock() + 60
                return self.unavailable(day, now)
        finally:
            self.lock.release()

    @staticmethod
    def unavailable(day, now):
        return {"date": day.isoformat(), "is_trading_day": None, "source": "baostock", "cached": False,
                "checked_at": now.isoformat(), "warning": "交易日历暂不可核实，继续由目标日真实行情确认，不将未知当作休市"}

    @staticmethod
    def present(day, opened, observed, cached):
        return {"date": day.isoformat(), "is_trading_day": opened, "source": "baostock", "cached": cached,
                "checked_at": observed.isoformat(), "warning": "交易日安排不保证个股有成交或行情已更新"}


@cache
def get_trading_calendar():
    return TradingCalendar()

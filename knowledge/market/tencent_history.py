"""腾讯不复权日线；历史估值与除权日涨跌幅不以当前快照或相邻收盘价猜测。"""

import json
from datetime import date

from knowledge.market.adapters import MarketHTTP, PARSE_ERRORS, TencentAdapter, number, tencent_volume_multiplier
from knowledge.market.models import DailyQuote, MarketUnavailable, date_window, market_symbol, validate_quote_batch


class TencentHistoryAdapter:
    name = "tencent_history"

    def __init__(self, http=None):
        self.http = http or MarketHTTP()

    def fetch_daily(self, code, start, end, now):
        start, end = date_window(start, end, now)
        symbol = market_symbol(code)
        try:
            payload = json.loads(self.http.get(
                "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
                {"param": f"{symbol},day,{start.isoformat()},{end.isoformat()},640,"},
            ).decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("code") != 0:
                raise ValueError("日线状态无效")
            data = payload["data"][symbol]
            rows, identity = data["day"], data["qt"][symbol]
            if not isinstance(rows, list) or not 0 < len(rows) <= 640:
                raise ValueError("日线列表无效")
            if not isinstance(identity, list) or len(identity) < 3 or identity[2] != code:
                raise ValueError("证券身份不匹配")
            if not isinstance(identity[1], str) or not identity[1].strip():
                raise ValueError("证券名称无效")
            quotes, dates = [], []
            multiplier = tencent_volume_multiplier(code)
            for row in rows:
                if not isinstance(row, list) or len(row) < 6:
                    raise ValueError("日线记录无效")
                day = date.fromisoformat(row[0])
                dates.append(day)
                if not start <= day <= end:
                    continue
                quote = DailyQuote(
                    code=code, name=identity[1], date=day, open=number(row[1]), close=number(row[2]),
                    high=number(row[3]), low=number(row[4]), volume=number(row[5], multiplier),
                    turnover=number(row[7]) if len(row) > 7 else None,
                    amount=number(row[8], 10000) if len(row) > 8 else None,
                    source=self.name, updated_at=now,
                    raw={"kline": row, "code": code, "symbol": symbol, "name": identity[1],
                         "fqt": 0, "volume_multiplier": multiplier},
                )
                if not quote.low <= min(quote.open, quote.close) <= max(quote.open, quote.close) <= quote.high:
                    raise ValueError("日线价格范围无效")
                quotes.append(quote)
            if not quotes or (len(rows) == 640 and min(dates) > start):
                raise ValueError("无范围内日线或历史被截断")
            quotes = validate_quote_batch(quotes, code, start, end)
        except PARSE_ERRORS:
            raise MarketUnavailable("腾讯历史日线格式、身份或范围无效") from None
        try:
            if not all(isinstance(field, str) for field in identity):
                raise ValueError("快照字段无效")
            text = f'v_{symbol}="' + "~".join(identity) + '";'
            snapshot = TencentAdapter(self.http).parse_snapshot(text, code, start, end, now)
            for quote in quotes:
                if quote.date == snapshot.date and all(
                    getattr(quote, field) == getattr(snapshot, field)
                    for field in ("open", "close", "high", "low", "volume")
                ):
                    for field in ("pre_close", "change_pct", "amount", "turnover", "pe", "pb",
                                  "market_cap", "valuation_source", "source_time"):
                        if getattr(snapshot, field) is not None:
                            setattr(quote, field, getattr(snapshot, field))
                    quote.raw["qt"] = identity
        except (MarketUnavailable, *PARSE_ERRORS):
            pass
        return sorted(quotes, key=lambda quote: quote.date)

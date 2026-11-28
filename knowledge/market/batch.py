"""批量快照保留每只证券真实收盘日期，不用名单的无日期价格代替行情。"""

import re

from knowledge.market.adapters import MarketHTTP, TencentAdapter, EastmoneyAdapter
from knowledge.market.models import MarketUnavailable, validate_quote_batch, security_name


class BatchQuotes:
    def __init__(self, http=None, fallback=None):
        self.http = http or MarketHTTP()
        self.parser = TencentAdapter(self.http)
        self.fallback = fallback or EastmoneyAdapter()

    def fetch(self, items, day, now, checkpoint=lambda: None):
        if not items or len(items) > 50:
            raise ValueError("批量最多 50 个证券")
        found, failures = {}, {}
        try:
            raw = self.http.get("https://qt.gtimg.cn/q=" + ",".join(item["symbol"] for item in items)).decode("gbk")
            statements = re.findall(r'v_(?:sh|sz|bj)[0-9]{6}="[^"\r\n]*";', raw)
            indexed = {}
            for statement in statements:
                symbol = statement.split("=", 1)[0][2:]
                if symbol in indexed:
                    raise MarketUnavailable("批量快照证券重复")
                indexed[symbol] = statement
        except (MarketUnavailable, UnicodeError):
            indexed = {}
        for item in items:
            checkpoint()
            code = item["code"]
            try:
                quote = self.parser.parse_snapshot(indexed.get(item["symbol"], ""), code, day, day, now)
                if security_name(quote.name) != security_name(item["name"]):
                    raise MarketUnavailable("快照名称与名单不一致")
                found[code] = quote
            except MarketUnavailable:
                failures[code] = "无目标日收盘快照或身份不一致"
        for item in items:
            if item["code"] not in failures:
                continue
            checkpoint()
            try:
                rows = validate_quote_batch(self.fallback.fetch_daily(item["code"], day, day, now), item["code"], day, day)
                if len(rows) != 1 or security_name(rows[0].name) != security_name(item["name"]):
                    raise MarketUnavailable("备用日线身份不符")
                found[item["code"]] = rows[0]
                failures.pop(item["code"])
            except MarketUnavailable:
                pass
        return list(found.values()), failures

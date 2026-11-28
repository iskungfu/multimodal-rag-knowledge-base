"""批量快照保留每只证券真实收盘日期，不用名单的无日期价格代替行情。"""

import re

from knowledge.market.adapters import MarketHTTP, TencentAdapter, EastmoneyAdapter
from knowledge.market.models import MarketUnavailable, SnapshotUnavailable, validate_quote_batch, security_name
from knowledge.market.tencent_history import TencentHistoryAdapter
from knowledge.market.baostock_source import BaoStockAdapter


class BatchQuotes:
    def __init__(self, http=None, fallback=None):
        self.http = http or MarketHTTP()
        self.parser = TencentAdapter(self.http)
        self.fallbacks = [fallback] if fallback is not None else [EastmoneyAdapter(), BaoStockAdapter(), TencentHistoryAdapter()]

    def fetch(self, items, day, now, checkpoint=lambda: None):
        quotes, snapshot_errors = self.fetch_snapshots(items, day, now, checkpoint)
        failures = snapshot_errors.copy()
        missing = [item for item in items if item["code"] in snapshot_errors]
        if missing:
            recovered, history_errors = self.fetch_history(missing, day, now, checkpoint)
            quotes.extend(recovered)
            failures = {code: f"{snapshot_errors[code]}；{reason}" for code, reason in history_errors.items()}
        return quotes, failures

    def fetch_snapshots(self, items, day, now, checkpoint=lambda: None):
        if not items or len(items) > 50:
            raise ValueError("批量最多 50 个证券")
        found, failures = {}, {}
        response_error = None
        malformed_symbols = set()
        try:
            raw = self.http.get("https://qt.gtimg.cn/q=" + ",".join(item["symbol"] for item in items)).decode("gbk")
            statements = re.findall(r'v_(?:sh|sz|bj)[0-9]{6}="[^"\r\n]*";', raw)
            declared_symbols = re.findall(r'\bv_((?:sh|sz|bj)[0-9]{6})\s*=', raw)
            if len(declared_symbols) != len(set(declared_symbols)):
                raise SnapshotUnavailable("invalid_response")
            indexed = {}
            for statement in statements:
                symbol = statement.split("=", 1)[0][2:]
                if symbol in indexed:
                    raise SnapshotUnavailable("invalid_response")
                indexed[symbol] = statement
            malformed_symbols = set(declared_symbols) - indexed.keys()
            if raw.strip() and not statements and raw.strip() != 'v_pv_none_match="1";':
                raise SnapshotUnavailable("invalid_response")
        except SnapshotUnavailable as exc:
            indexed, response_error = {}, exc.reason
        except MarketUnavailable:
            indexed, response_error = {}, "source_unavailable"
        except UnicodeError:
            indexed, response_error = {}, "invalid_response"
        for item in items:
            checkpoint()
            code = item["code"]
            try:
                if response_error:
                    raise SnapshotUnavailable(response_error)
                if item["symbol"] in malformed_symbols:
                    raise SnapshotUnavailable("invalid_response")
                if item["symbol"] not in indexed:
                    raise SnapshotUnavailable("missing_snapshot")
                quote = self.parser.parse_snapshot(indexed[item["symbol"]], code, day, day, now)
                if security_name(quote.name) != security_name(item["name"]):
                    raise SnapshotUnavailable("name_mismatch")
                found[code] = quote
            except SnapshotUnavailable as exc:
                failures[code] = SnapshotUnavailable.messages[exc.reason]
            except MarketUnavailable:
                failures[code] = SnapshotUnavailable.messages["invalid_snapshot"]
        return list(found.values()), failures

    def fetch_history(self, items, day, now, checkpoint=lambda: None):
        if not items or len(items) > 50:
            raise ValueError("批量最多 50 个证券")
        found = {}
        failures = {item["code"]: "历史备用未取得可核实的目标日行情" for item in items}
        for item in items:
            if item["code"] not in failures:
                continue
            for adapter in self.fallbacks:
                checkpoint()
                try:
                    rows = validate_quote_batch(adapter.fetch_daily(item["code"], day, day, now), item["code"], day, day)
                    if len(rows) != 1:
                        raise MarketUnavailable("备用日线身份不符")
                    if security_name(rows[0].name) != security_name(item["name"]):
                        failures[item["code"]] = "历史备用出现名称与目录不一致，未取得可核实的目标日行情"
                        raise MarketUnavailable("备用日线身份不符")
                    found[item["code"]] = rows[0]
                    failures.pop(item["code"])
                    break
                except MarketUnavailable:
                    pass
        return list(found.values()), failures

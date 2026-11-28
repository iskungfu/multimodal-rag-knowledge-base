"""分页核对沪深京证券名单；备用来源不冒称完整市场覆盖。"""

import json
import math
import re

from knowledge.market.adapters import MarketHTTP
from knowledge.market.models import MarketUnavailable, market_symbol


class MarketUniverse:
    def __init__(self, primary=None, backup=None):
        self.primary = primary or MarketHTTP()
        self.backup = backup or MarketHTTP()

    def fetch(self, checkpoint=lambda: None):
        try:
            return self._eastmoney(checkpoint)
        except (MarketUnavailable, ValueError, KeyError, TypeError, UnicodeError):
            try:
                return self._sina(checkpoint)
            except (MarketUnavailable, ValueError, KeyError, TypeError, UnicodeError):
                raise MarketUnavailable("证券名单来源不可用或分页不完整") from None

    @staticmethod
    def _finish(records, total, source, complete):
        if len(records) != total or len({item["code"] for item in records}) != total:
            raise ValueError("证券名单缺页或重复")
        if not 1000 <= total <= 20000:
            raise ValueError("全市场证券数量异常")
        supported, excluded = [], []
        for record in records:
            try:
                symbol = market_symbol(record["code"])
            except ValueError:
                excluded.append(record["code"])
                continue
            if record["symbol"] != symbol:
                raise ValueError("证券名单市场标识不一致")
            supported.append(record)
        if len(supported) < 1000:
            raise ValueError("可识别证券名单不足")
        return {"items": sorted(supported, key=lambda item: item["code"]), "source": source,
                "complete": complete and not excluded, "reported_total": total, "excluded_codes": excluded}

    def _eastmoney(self, checkpoint):
        records, expected, page = [], None, 1
        while True:
            checkpoint()
            payload = json.loads(self.primary.get("https://push2.eastmoney.com/api/qt/clist/get", {
                "pn": page, "pz": 100, "po": 0, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f12,f13,f14",
            }).decode("utf-8"))
            data = payload["data"]
            total, rows = data["total"], data["diff"]
            if payload.get("rc") != 0 or type(total) is not int or not 1000 <= total <= 20000 or not isinstance(rows, list):
                raise ValueError("证券名单格式无效")
            expected = total if expected is None else expected
            if total != expected or len(rows) != min(100, total - (page - 1) * 100):
                raise ValueError("证券名单分页变化")
            for row in rows:
                code, name = row["f12"], row["f14"]
                if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or str(row["f13"]) not in {"0", "1"} or not isinstance(name, str) or not name.strip():
                    raise ValueError("证券名单身份不符")
                symbol = ("sh" if str(row["f13"]) == "1" else "bj" if code.startswith("920") else "sz") + code
                records.append({"code": code, "name": name.strip(), "symbol": symbol})
            if page >= math.ceil(total / 100):
                return self._finish(records, total, "eastmoney", True)
            page += 1

    def _sina(self, checkpoint):
        base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center."
        checkpoint()
        total = int(json.loads(self.backup.get(base + "getHQNodeStockCount", {"node": "hs_a"}).decode("utf-8")))
        if not 1000 <= total <= 20000:
            raise ValueError("证券总数异常")
        records = []
        for page in range(1, math.ceil(total / 100) + 1):
            checkpoint()
            rows = json.loads(self.backup.get(base + "getHQNodeData", {
                "node": "hs_a", "page": page, "num": 100, "sort": "symbol", "asc": 1, "symbol": "",
            }).decode("utf-8"))
            if not isinstance(rows, list) or len(rows) != min(100, total - (page - 1) * 100):
                raise ValueError("备用证券名单缺页")
            for row in rows:
                symbol = row["symbol"]
                if not isinstance(symbol, str) or not re.fullmatch(r"(?:sh|sz|bj)[0-9]{6}", symbol) or symbol[2:] != row["code"] or not isinstance(row["name"], str) or not row["name"].strip():
                    raise ValueError("备用证券名单身份不符")
                records.append({"code": row["code"], "name": row["name"].strip(), "symbol": symbol})
        return self._finish(records, total, "sina", False)

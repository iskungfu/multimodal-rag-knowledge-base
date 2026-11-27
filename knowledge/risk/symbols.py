"""证券标识必须由公开证券检索记录核验，不用 LLM 猜代码。"""

import json
import re
import threading
import time
from dataclasses import dataclass

from knowledge.market.adapters import MarketHTTP
from knowledge.market.models import MarketUnavailable, market_symbol

SYMBOL_URL = "https://searchapi.eastmoney.com/api/suggest/get"
FINANCE = re.compile(r"股票|个股|A股|股价|行情|买入|卖出|持仓|风险|投资|市盈率|换手率|波动率|估值|财报|分散|价值投资|公共知识|安全边际|PE", re.I)
PRODUCT = re.compile(r"产品|型号|万用表|笔记本|记录器|路由器|手机(?!号)|邮编|订单|验证码|设备|接线|开机|电压|电流|这款")
INDEX = re.compile(r"指数|上证|深证成指|ETF|基金", re.I)
SECURITY_IDENTIFIER = re.compile(r"(?<![0-9A-Za-z])(?:(sh|sz))?([0-9]{6})(?:\.(sh|sz))?(?![0-9A-Za-z])", re.I)
NAME_LEAD_IN = re.compile(r"^(?:(?:我|本人)?(?:想要|想|打算|准备|已经|刚刚|刚|最近|听说|关注|看好|持有|持仓|购买|买入|卖出|买了|卖了|买|卖|了))+")
# 这里只切出用户明示的查询词，任何变换后的词都不能直接作为证券身份。
SEPARATORS = re.compile(r"能不能买|可不可以买|值不值得买|可以买吗|值得买吗|能否买|是否值得|会涨吗|还会涨吗|会不会涨|还能涨吗|风险等级|风险大吗|走势|是什么意思|请问|请|帮我|帮忙|看看|看下|查一下|查询|分析一下|分析|评估|介绍|一下|今天|今日|现在|最近|的|股票|个股|股价|行情|风险|如何|怎么样|怎样|有多大|高不高|高吗|是否|适合|可以买|能买吗|买入|卖出|持仓|代码|证券|和|与|及|对比|比较|[\d\W_]+", re.I)


@dataclass(frozen=True)
class Security:
    code: str
    name: str
    source: str = "东方财富证券检索（AStock / 沪深市场）"


@dataclass
class Resolution:
    security: Security | None = None
    message: str | None = None
    knowledge: bool = False


class SymbolResolver:
    def __init__(self, http=None, clock=time.monotonic):
        self.http = http or MarketHTTP()
        self.clock = clock
        self.cache = {}
        self.lock = threading.Lock()

    def lookup(self, term):
        with self.lock:
            self.cache = {k: v for k, v in self.cache.items() if v[0] > self.clock()}
            if term in self.cache:
                return self.cache[term][1]
            try:
                payload = json.loads(self.http.get(SYMBOL_URL, {"input": term, "type": "14", "count": "20"}).decode("utf-8"))
                table = payload["QuotationCodeTable"]
                if table["Status"] != 0:
                    raise ValueError
                records = []
                for row in table.get("Data") or []:
                    code = row.get("Code", "")
                    try:
                        symbol = market_symbol(code)
                    except ValueError:
                        continue
                    market = "1" if symbol.startswith("sh") else "0"
                    if row.get("Classify") == "AStock" and str(row.get("MktNum")) == market and row.get("QuoteID") == market + "." + code and row.get("Name"):
                        records.append(Security(code, row["Name"]))
                if len(self.cache) >= 512:
                    self.cache.clear()
                self.cache[term] = (self.clock() + 3600, records)
                return records
            except (ValueError, TypeError, KeyError, AttributeError):
                raise MarketUnavailable("证券名称核验暂不可用") from None

    def resolve(self, query):
        financial = bool(FINANCE.search(query))
        # 明确的公共概念问题无需证券源可用，也不发送到证券检索。
        if re.fullmatch(r"(?:请)?(?:解释一下|解释|什么是|如何理解|介绍一下|介绍)(?:市盈率|分散投资|价值投资|安全边际|换手率|波动率|PE)(?:和(?:市盈率|分散投资|价值投资|安全边际|换手率|波动率|PE))*[？?。\s]*", query, re.I):
            return Resolution(knowledge=True)
        if PRODUCT.search(query) and not re.search(r"股票|个股|股价|行情", query):
            return Resolution()
        if INDEX.search(query):
            if re.search(r"什么是|如何理解|解释|介绍|概念|知识|为什么", query) and not SECURITY_IDENTIFIER.search(query):
                return Resolution(knowledge=True)
            if re.search(r"风险|行情|000001|买|卖|怎么样", query):
                return Resolution(message="本期仅评估沪深 A 股个股，不评估指数或基金。000001 在个股查询中仅指平安银行；请确认要查询的证券。")
            return Resolution(knowledge=True)
        identifiers = list(SECURITY_IDENTIFIER.finditer(query))
        codes = list(dict.fromkeys(identifier[2] for identifier in identifiers))
        for identifier in identifiers:
            try:
                expected_market = market_symbol(identifier[2])[:2]
            except ValueError:
                continue
            if any(market and market.lower() != expected_market for market in (identifier[1], identifier[3])):
                return Resolution(message="市场标识与股票代码不一致，请确认证券；000001 个股为 sz000001 平安银行。")
        name_text = SECURITY_IDENTIFIER.sub(" ", query)
        terms = list(dict.fromkeys(
            NAME_LEAD_IN.sub("", term) for term in SEPARATORS.split(name_text)
            if 2 <= len(NAME_LEAD_IN.sub("", term)) <= 12
        ))
        stock_intent = bool(re.search(r"股票|个股|股价|行情|买入|卖出|持仓", query)) or bool(codes)
        if len(codes) > 1 or (len(terms) > 4 and stock_intent):
            return Resolution(message="请每次明确一只沪深 A 股的完整证券名称或代码，以便核实身份。")
        candidates, exact, named_candidates = {}, {}, {}
        try:
            for term in codes + terms[:4]:
                for security in self.lookup(term):
                    candidates[security.code] = security
                    if term not in codes and term in security.name:
                        named_candidates[security.code] = security
                    if (term in codes and security.code == term) or security.name in query:
                        exact[security.code] = security
        except MarketUnavailable:
            if stock_intent or financial:
                return Resolution(message="证券身份来源暂不可用，无法核实名称/代码。请稍后重试，并使用完整证券名称或代码。")
            return Resolution()
        if len(exact) == 1:
            security = next(iter(exact.values()))
            if codes and codes != [security.code]:
                return Resolution(message="名称与代码不一致，请确认完整证券名称及对应代码。")
            if codes and any(candidate != security.code for candidate in named_candidates):
                return Resolution(message="名称或简称与代码的证券候选不一致，请确认完整证券名称及对应代码。")
            return Resolution(security=security)
        if candidates or len(exact) > 1:
            options = "、".join(f"{s.name}（{s.code}）" for s in candidates.values())
            return Resolution(message=f"无法唯一确认证券，请使用完整名称或代码确认：{options}。候选来源：东方财富证券检索。")
        if stock_intent:
            return Resolution(message="未能核实对应的沪深 A 股，请确认完整证券名称或代码；无需填写行情指标。")
        return Resolution(knowledge=financial)

"""投资/商品路由及有界追问；历史仅用最近一条用户的服务端上下文。"""
import re

from knowledge.risk.symbols import PRODUCT, SECURITY_IDENTIFIER, Resolution

PERSONAL = re.compile(r"复盘|教训|心得|交易方法|交易策略|个人策略|我的策略|我的投资|我上次|我之前|我以前|买卖点|追高|回踩|仓位")
MARKET = re.compile(r"大盘|上证指数|深证成指|sh000001|sz399001", re.I)
FOLLOWUP = re.compile(r"^(?:(?:那|那么)?(?:它|这只(?:股票)?|该股|这笔(?:交易)?|那笔(?:交易)?|这条策略).{0,60}|继续|具体说说|为什么[？?]?|怎么办[？?]?)$")


def route_query(query, resolver, history):
    explicit_stock = bool(SECURITY_IDENTIFIER.search(query) or re.search(r"股票|个股|股价|行情", query))
    product = bool(PRODUCT.search(query)) and not explicit_stock
    if product:
        return {"resolution": Resolution(), "query": query, "investment": False, "market": False}
    recent = next((item for item in reversed(history) if item.get("role") == "user"), {})
    context = recent.get("query_context") or {}
    if not explicit_stock and FOLLOWUP.fullmatch(query) and context.get("investment") is True:
        previous = context.get("query", "")[:1500]
        security = context.get("security")
        effective = previous + "\n当前追问：" + query
        resolution = resolver.resolve(security["code"] + " " + security["name"]) if security else Resolution(knowledge=True)
        return {"resolution": resolution, "query": effective, "investment": True, "market": bool(context.get("market"))}
    market = bool(MARKET.search(query))
    if market and not re.search(r"ETF|基金|[0-9]{6}(?![0-9])", re.sub(r"sh000001|sz399001", "", query, flags=re.I)):
        return {"resolution": Resolution(knowledge=True), "query": query, "investment": True, "market": True}
    if PERSONAL.search(query) and not explicit_stock:
        resolution = Resolution(knowledge=True)
    else:
        resolution = resolver.resolve(query)
    return {"resolution": resolution, "query": query,
            "investment": bool(resolution.security or resolution.knowledge), "market": False}

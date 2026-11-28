"""投资/商品路由及有界追问；历史仅用最近一条用户的服务端上下文。"""
import re

from knowledge.risk.symbols import PRODUCT, SECURITY_IDENTIFIER, Resolution

PERSONAL = re.compile(r"复盘|教训|心得|交易方法|交易策略|个人策略|我的策略|我的投资|我上次|我之前|我以前|买卖点|追高|回踩|仓位|止损|止盈|定投|资产配置|投资组合|成本控制")
MARKET = re.compile(r"大盘|上证指数|深证成指|sh000001|sz399001", re.I)
FOLLOWUP = re.compile(r"^(?:(?:那|那么)?(?:它|这只(?:股票)?|该股|这笔(?:交易)?|那笔(?:交易)?|这条策略)?(?:呢|风险(?:大吗|高吗|如何|怎么样)|还能买吗|能买吗|可以买(?:吗)?|怎么(?:办|操作|止损)|该怎么办|实际成交了吗)|继续|具体说说|为什么|怎么办)[？?。\s]*$")
MARKET_WORDS = re.compile(r"大盘|上证指数|深证成指|sh000001|sz399001", re.I)
MARKET_ONLY = re.compile(r"[，、。？?\s]|结合|按照|按|看下|请|帮我|分析|一下|现在|当前|的|位置|风险|高低|高吗|大吗|怎么样|如何|趋势|行情|判断|看看|查询")
TRADE_CONTEXT = re.compile(r"我的|我们|本人|我|以前|之前|上次|这次|今天|明天|昨天|计划|准备|打算|想要|已经|当时|记录|交易|复盘|的|中")


def current_security_query(query):
    identity = re.fullmatch(r"(?:它|这只股票|这只|该股)(?:就是|是不是|是)(.+?)(?:吗|么)?[？?。\s]*", query)
    if identity:
        return identity[1]
    value = re.sub(r"(?:结合|按照|按)?(?:我的)?(?:个人策略|交易策略|交易方法|复盘|教训|心得)(?:和|与|及)?", " ", query)
    return re.sub(r"结合|按照|这只|该股", " ", value).strip(" ，,。")


def route_query(query, resolver, history):
    explicit_stock = bool(SECURITY_IDENTIFIER.search(query) or re.search(r"股票|个股|股价|行情", query))
    product = bool(PRODUCT.search(query)) and not explicit_stock
    if product:
        return {"resolution": Resolution(), "query": query, "investment": False, "market": False}
    recent = next((item for item in reversed(history) if item.get("role") == "user"), {})
    context = recent.get("query_context") or {}
    if not SECURITY_IDENTIFIER.search(query) and FOLLOWUP.fullmatch(query) and context.get("investment") is True:
        previous = context.get("query", "")[:1500]
        security = context.get("security")
        effective = previous + "\n当前追问：" + query
        resolution = resolver.resolve(security["code"] + " " + security["name"]) if security else Resolution(knowledge=True)
        return {"resolution": resolution, "query": effective, "investment": True, "market": bool(context.get("market"))}
    market = bool(MARKET.search(query))
    if market and not re.search(r"ETF|基金", query):
        bare_codes = [match for match in SECURITY_IDENTIFIER.finditer(query) if not match[1] and not match[3]]
        if bare_codes and re.search(r"上证指数|深证成指|指数", query):
            return {"resolution": Resolution(message="指数名称与六位证券代码需要确认，请分别明确指数和个股。上证指数使用sh000001，深证成指使用sz399001；个股000001是平安银行。"),
                    "query": query, "investment": False, "market": False}
        remainder = MARKET_WORDS.sub("", query)
        if not MARKET_ONLY.sub("", remainder):
            return {"resolution": Resolution(knowledge=True), "query": query, "investment": True, "market": True}
        resolution = resolver.resolve(current_security_query(remainder))
        return {"resolution": resolution, "query": query, "investment": True, "market": True}
    security_question = bool(re.search(r"风险|股价|走势|能不能买|能买吗|可以买|估值", query))
    trade_names = re.findall(r"([\u4e00-\u9fff]{2,12})(?:买入|卖出|买了|卖了|持有|持仓)", current_security_query(query))
    named_trade = any(len(TRADE_CONTEXT.sub("", name)) >= 2 for name in trade_names)
    if PERSONAL.search(query) and not explicit_stock and not security_question and not named_trade:
        resolution = Resolution(knowledge=True)
    else:
        resolution = resolver.resolve(current_security_query(query))
    return {"resolution": resolution, "query": query,
            "investment": bool(resolution.security or resolution.knowledge), "market": False}

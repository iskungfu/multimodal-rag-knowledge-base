"""聊天按需刷新行情、评估与可追溯文字展示，不依赖向量模型或 LLM。"""

from datetime import datetime, timedelta, timezone

from knowledge.market.models import SHANGHAI, date_window
from knowledge.risk.rules import N, THRESHOLDS, evaluate, finite

METRIC_NAMES = {"change_pct": "日涨跌幅", "turnover": "换手率", "volatility": f"{N} 日波动率", "pe": "动态 PE", "volume_ratio": "成交量比"}


def display_time(value):
    if not value:
        return "未提供"
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)  # MongoDB 默认以无 tzinfo 的 UTC 返回
        return stamp.astimezone(SHANGHAI).isoformat()
    except (ValueError, TypeError):
        return "无效时间"


class RiskService:
    def __init__(self, market_factory=None, now=None):
        if market_factory is None:
            from knowledge.api.market_api import get_market_service
            market_factory = get_market_service
        self.market_factory = market_factory
        self.now = now or (lambda: datetime.now(timezone.utc))

    def answer(self, security):
        now = self.now()
        _, end = date_window(None, None, now)
        start = end - timedelta(days=120)
        warnings = []
        update_failed = False
        try:
            market = self.market_factory()
        except Exception:
            return "行情存储暂不可用，无法评估个股风险，请稍后重试。"
        try:
            result = market.refresh(security.code, start, end)
            warnings.extend(result.get("warnings", []))
            cache = "短时缓存（最长 300 秒）" if result.get("cached") else "本次刷新"
        except Exception:
            update_failed = True
            warnings.append("行情更新失败；以下仅依据已存收盘行情，不能代表当前风险")
            cache = "更新失败后的旧缓存"
            try:
                result = market.read(security.code, start, end)
            except Exception:
                return "行情源与缓存暂不可用，无法评估个股风险，请稍后重试。"
        assessment = evaluate(result.get("quotes", []), security.code, end)
        latest = assessment["latest"]
        if not latest or assessment["metrics"] == {}:
            return "无有效收盘行情，无法评估个股风险。" + "\n" + "；".join(warnings + assessment["warnings"])
        if latest.get("name") != security.name:
            return "行情名称与证券检索名称不一致，无法可靠确认证券，请稍后重试或确认完整名称及代码。"
        day = str(latest["date"])
        lag = (now.astimezone(SHANGHAI).date() - datetime.fromisoformat(day).date()).days
        limited = assessment["limited"] or update_failed or result.get("source", "eastmoney") != "eastmoney" or day < end.isoformat()
        level = assessment["level"]
        # 旧行情/更新失败仅给已观测中高风险下限，不以低风险掩盖不确定性。
        if limited and level == "低":
            level = None
        conclusion = f"个股风险：{level}风险" if level else "个股风险：无法完整评估"
        if limited and level:
            conclusion += "（有限依据，仅反映已观测指标）"
        lines = [f"{security.name}（{security.code}）｜{conclusion}。",
                 f"证券身份来源：{security.source}。",
                 f"实际行情日期：{day}；距查询日 {lag} 个自然日（非交易日延迟）；{cache}。",
                 f"行情来源：{latest.get('source', '未知')}；估值来源：{latest.get('valuation_source') or '缺失'}；抓取时间：{display_time(latest.get('updated_at'))}。",
                 f"源快照时间：{display_time(latest.get('source_time'))}。",
                 f"收盘价：{finite(latest.get('close')):g} 元；成交量：{finite(latest.get('volume')) if finite(latest.get('volume')) is not None else '缺失'} 股。"]
        for key, name in METRIC_NAMES.items():
            value = assessment["metrics"][key]
            unit = "%" if key in {"change_pct", "turnover", "volatility"} else " 倍"
            lines.append(f"{name}：{value:.2f}{unit}。" if value is not None else f"{name}：缺失（无有效字段或历史样本不足）。")
        warnings.extend(assessment["warnings"])
        if day < end.isoformat():
            warnings.append("最新日期早于可收盘日期，可能休市、停牌或来源延迟，不能当作今日行情")
        if assessment["missing"]:
            warnings.append("缺失指标：" + "、".join(METRIC_NAMES[key] for key in assessment["missing"]))
        if limited:
            warnings.append("资料有限，未观察到高值不等于低风险")
        lines.extend(dict.fromkeys(warnings))
        thresholds = []
        for key, (middle, high) in THRESHOLDS.items():
            name = f"|{METRIC_NAMES[key]}|" if key == "change_pct" else METRIC_NAMES[key]
            unit = "%" if key in {"change_pct", "turnover", "volatility"} else " 倍"
            thresholds.append(f"{name} {middle:g}{unit}/{high:g}{unit}")
        lines.append("初始规则 v1：各指标取最高风险档；中/高阈值依次为 " + "、".join(thresholds) + "。非正 PE 按高风险异常；量比 ≤0.2 为中、零量比为高。")
        lines.append(f"N={N}；波动率是最近 {N} 条日线的源日涨跌幅样本标准差（不年化）；成交量比=当日成交量/此前 {N} 条日线平均成交量，不含当日。缺失不填零。")
        lines.append("以上为可解释的初始规则，未经回测；行业估值不可直接比较，不预测涨跌，不提供买卖建议。")
        return "\n\n".join(lines)

"""可解释的初始规则；不预测涨跌，不表示回测结论。单位沿用行情层。"""

import math
import statistics
from datetime import date

N = 20
LABELS = {0: "低", 1: "中", 2: "高"}
THRESHOLDS = {
    "change_pct": (5, 9), "turnover": (5, 15),
    "volatility": (2, 4), "pe": (40, 80), "volume_ratio": (2, 3),
}


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def at_least(value, threshold):
    # 计算指标在数学边界的浮点舍入误差不能导致降档。
    return value >= threshold or math.isclose(value, threshold, rel_tol=1e-12, abs_tol=1e-12)


def evaluate(quotes, code, end):
    """以最新真实日期为锚；重复日期不增加样本，冲突日期不能任选一条。"""
    rows, conflicts, warnings = {}, set(), []
    fields = ("close", "change_pct", "turnover", "pe", "volume", "name", "source", "valuation_source")
    for row in quotes:
        if row.get("code") != code:
            warnings.append("忽略代码不匹配的记录")
            continue
        try:
            day = date.fromisoformat(str(row["date"]))
        except (ValueError, KeyError):
            warnings.append("忽略无效日期记录")
            continue
        if day > end:
            warnings.append("忽略未收盘或未来日期记录")
            continue
        if day in rows and any(row.get(key) != rows[day].get(key) for key in fields):
            conflicts.add(day)
        rows[day] = row
    ordered = [rows[day] if day not in conflicts else {} for day in sorted(rows)]
    if conflicts:
        warnings.append("发现同日期冲突行情，该日指标不参与计算")
    result = {"level": None, "limited": True, "metrics": {}, "missing": [],
              "warnings": warnings, "latest": ordered[-1] if ordered else None}
    if not ordered or not ordered[-1] or (finite(ordered[-1].get("close")) or 0) <= 0:
        warnings.append("无有效最新收盘行情，无法评估")
        return result
    latest = ordered[-1]
    change, turnover, pe, volume = (finite(latest.get(key)) for key in ("change_pct", "turnover", "pe", "volume"))
    if turnover is not None and turnover < 0:
        turnover = None
    returns = [finite(row.get("change_pct")) for row in ordered[-N:]]
    # 使用来源日涨跌幅，禁止从不复权 close 计算跨日收益率。
    volatility = statistics.stdev(returns) if len(returns) == N and None not in returns else None
    prior_volumes = [finite(row.get("volume")) for row in ordered[-N-1:-1]]
    ratio = None
    if len(prior_volumes) == N and all(v is not None and v >= 0 for v in prior_volumes):
        average = statistics.mean(prior_volumes)
        if average > 0 and volume is not None and volume >= 0:
            ratio = finite(volume / average)
    metrics = dict(change_pct=change, turnover=turnover, volatility=finite(volatility), pe=pe, volume_ratio=ratio)
    scores = {}
    for key, value in metrics.items():
        if value is None:
            continue
        middle, high = THRESHOLDS[key]
        measured = abs(value) if key == "change_pct" else value
        scores[key] = 2 if at_least(measured, high) else 1 if at_least(measured, middle) else 0
    if pe is not None and pe <= 0:
        scores["pe"] = 2
        warnings.append("动态 PE 非正，估值不适用；按估值异常提高风险，不能解释为便宜")
    if ratio is not None and ratio <= 0.2:
        scores["volume_ratio"] = 2 if ratio == 0 else 1
    if volume == 0:
        # 即使没有历史均量，也明确提示零成交量，不能判为低风险。
        warnings.append("最新日成交量为零，可能停牌或流动性不足，无法给出完整评估")
    missing = [key for key, value in metrics.items() if value is None]
    limited = bool(missing or volume is None or volume <= 0 or conflicts)
    score = max(scores.values(), default=0)
    result.update(metrics=metrics, missing=missing, limited=limited,
                  level=LABELS[score] if scores and (score > 0 or not limited) else None)
    return result

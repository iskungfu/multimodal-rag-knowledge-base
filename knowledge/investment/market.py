"""复用双指数能力提供当前可收盘日的独立风险依据。"""
from datetime import datetime, timedelta, timezone

from knowledge.market.models import date_window
from knowledge.summary.indices import INDICES, evaluate_index, rule_note


def index_context(service=None, now=None):
    try:
        if service is None:
            from knowledge.api.summary_api import get_summary_service
            service = get_summary_service().indices
        moment = now or datetime.now(timezone.utc)
        _, end = date_window(None, None, moment)
        evidence = []
        for symbol in INDICES:
            try:
                rows, warnings = service.fetch(symbol, end - timedelta(days=120), end, moment)
                assessment = evaluate_index(rows, symbol, end)
                change = assessment["return_pct"]
                ratio = assessment["volume_ratio"]
                metrics = (f"近20日涨跌幅：{change:.2f}%；" if change is not None else "近20日涨跌幅：缺失；")
                metrics += f"成交量比：{ratio:.2f}倍。" if ratio is not None else "成交量比：缺失。"
                level = assessment["level"] + "风险" if assessment["level"] else "无法完整评估"
                text = (f"{assessment['name']}（{symbol}）：大盘当前位置风险：{level}。"
                        f"实际行情日期：{assessment['date']}；来源：{assessment['latest']['source']}。"
                        + metrics + "；".join(warnings + assessment["warnings"]) + rule_note())
            except Exception:
                text = f"{INDICES[symbol][1]}（{symbol}）目标日 {end.isoformat()} 收盘数据尚未核实，可能休市或来源延迟，无法判断当前位置风险。"
            evidence.append({"id": "index:" + symbol, "text": text})
        return evidence
    except Exception:
        return [{"id": "index:unavailable", "text": "指数行情服务暂不可用，无法判断大盘当前位置风险。"}]

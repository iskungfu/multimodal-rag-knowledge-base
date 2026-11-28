import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from knowledge.risk.rules import evaluate
from knowledge.risk.symbols import Resolution, Security, SymbolResolver
from knowledge.service.risk_service import RiskService
from knowledge.market.models import MarketUnavailable

END = date(2026, 11, 27)
NOW = datetime(2026, 11, 27, 9, tzinfo=timezone.utc)
SECURITY = Security("000001", "平安银行")


def quotes():
    return [dict(code="000001", name="平安银行", date=(END-timedelta(days=20-i)).isoformat(),
                 close=10, change_pct=0.1, turnover=1, pe=10 if i == 20 else None,
                 volume=100, source="eastmoney", valuation_source="eastmoney", updated_at=NOW)
            for i in range(21)]


@pytest.mark.parametrize("field,value,level", [
    ("change_pct", 4.999, "低"), ("change_pct", -5, "中"), ("change_pct", 9, "高"),
    ("turnover", 5, "中"), ("turnover", 15, "高"),
    ("pe", 39.999, "低"), ("pe", 40, "中"), ("pe", 80, "高"),
    ("pe", -1, "高"), ("pe", 0, "高"),
    ("volume", 200, "中"), ("volume", 300, "高"),
    ("volume", 20, "中"), ("volume", 0, "高"),
])
def test_rule_boundaries(field, value, level):
    data = quotes()
    data[-1][field] = value
    result = evaluate(data, "000001", END)
    assert result["level"] == level
    if field == "volume":
        assert result["metrics"]["volume_ratio"] == value / 100


@pytest.mark.parametrize("field", ["change_pct", "turnover", "pe", "volume", "close"])
@pytest.mark.parametrize("bad", [None, float('nan'), float('inf'), float('-inf'), "invalid"])
def test_bad_latest_data_never_low(field, bad):
    data = quotes()
    data[-1][field] = bad
    result = evaluate(data, "000001", END)
    assert result["level"] is None
    assert result["limited"]
    assert all(v is None or isinstance(v, float) for v in result["metrics"].values())


@pytest.mark.parametrize("n", [0, 1, 19, 20])
def test_insufficient_history(n):
    result = evaluate(quotes()[-n:] if n else [], "000001", END)
    assert result["level"] is None


def test_order_duplicates_and_conflicts():
    data = quotes()
    expected = evaluate(data, "000001", END)
    assert evaluate(list(reversed(data)) + [data[-1]], "000001", END) == expected
    conflict = dict(data[-1], close=99)
    assert evaluate(data + [conflict], "000001", END)["level"] is None
    assert evaluate([conflict] + data, "000001", END)["level"] is None


def test_volatility_uses_source_returns_not_unadjusted_prices():
    data = quotes()
    data[-1]["close"] = 5  # 模拟除权价阶跃，不改来源日收益率
    assert evaluate(data, "000001", END)["metrics"]["volatility"] == 0
    for row in data:
        row["change_pct"] = 4 if date.fromisoformat(row["date"]).day % 2 else -4
    assert evaluate(data, "000001", END)["level"] == "高"


def test_volume_zero_base_and_bad_history():
    data = quotes()
    for row in data[:-1]:
        row["volume"] = 0
    assert evaluate(data, "000001", END)["metrics"]["volume_ratio"] is None
    data = quotes()
    data[-3]["change_pct"] = float('nan')
    assert evaluate(data, "000001", END)["metrics"]["volatility"] is None


def test_invalid_dates_and_wrong_stock():
    data = quotes()
    for bad in [dict(data[-1], date="bad"), dict(data[-1], date="2099-01-01"), dict(data[-1], code="600519")]:
        result = evaluate(data + [bad], "000001", END)
        assert result["metrics"]["pe"] == 10
        assert result["warnings"]


def test_partial_high_is_explicit_and_snapshot_cannot_be_low():
    market = Mock()
    data = quotes()[-1:]
    market.refresh.return_value = dict(source="tencent", quotes=data, warnings=["备用源仅有快照"])
    service = RiskService(lambda: market, now=lambda: NOW)
    answer = service.answer(SECURITY)
    assert "无法完整评估" in answer and "20 日波动率：缺失" in answer
    data[-1]["pe"] = 90
    answer = service.answer(SECURITY)
    assert "高风险（有限依据" in answer and "成交量比：缺失" in answer


@pytest.mark.parametrize("change, conclusion", [
    (0.1, "个股风险：无法完整评估"),
    (5, "个股风险：中风险（有限依据，仅反映已观测指标）"),
])
def test_weekend_previous_close_keeps_observed_risk_without_claiming_current_low(change, conclusion):
    market = Mock()
    data = quotes()
    data[-1]["change_pct"] = change
    market.refresh.return_value = dict(source="eastmoney", quotes=data, warnings=[])
    service = RiskService(lambda: market, now=lambda: NOW + timedelta(days=1))
    answer = service.answer(SECURITY)
    assert conclusion in answer
    assert "实际行情日期：2026-11-27" in answer and "距查询日 1 个自然日" in answer
    assert "最新日期早于可收盘日期" in answer and "资料有限" in answer
    assert "个股风险：低风险" not in answer
    assert "20 日波动率：缺失" not in answer and "成交量比：缺失" not in answer


def test_fresh_full_data_and_refresh_failure_old_cache():
    market = Mock()
    market.refresh.return_value = dict(source="eastmoney", quotes=quotes(), warnings=["部分日期没有可核实的动态 PE，缺失估值保留为空"])
    service = RiskService(lambda: market, now=lambda: NOW)
    assert "低风险" in service.answer(SECURITY)
    market.refresh.side_effect = RuntimeError("credential-private")
    market.read.return_value = {"quotes": quotes()[:-1]}
    answer = service.answer(SECURITY)
    assert "2026-11-26" in answer and "更新失败后的旧缓存" in answer
    assert "低风险" not in answer.split("初始规则")[0] or "不等于低风险" in answer
    assert "credential-private" not in answer
    market.read.side_effect = RuntimeError("secret")
    assert "暂不可用" in service.answer(SECURITY)


def lookup_rows(code, name, market, kind="AStock"):
    return {"Code": code, "Name": name, "MktNum": market, "QuoteID": market + "." + code, "Classify": kind}


@pytest.fixture
def resolver():
    rows = [lookup_rows("000001", "平安银行", "0"), lookup_rows("000001", "上证指数", "1", "Index"),
            lookup_rows("000001", "基金", "150", "OTCFUND"), lookup_rows("600519", "贵州茅台", "1")]
    http = Mock()
    def get(url, params):
        found = [row for row in rows if params["input"] in (row["Code"], row["Name"]) or params["input"] == "茅台" and row["Code"] == "600519"]
        return json.dumps({"QuotationCodeTable": {"Status": 0, "Data": found}}).encode()
    http.get.side_effect = get
    return SymbolResolver(http)


@pytest.mark.parametrize("query,code", [("000001", "000001"), ("查询000001风险", "000001"),
    ("平安银行怎么样", "000001"), ("请分析贵州茅台的风险", "600519"), ("600519", "600519"), ("sz000001", "000001"), ("查询sz000001风险", "000001"), ("sh600519", "600519")])
def test_verified_identity(resolver, query, code):
    assert resolver.resolve(query).security.code == code


@pytest.mark.parametrize("query", ["股票600519现在的位置风险怎么样？", "600519近期成交量有没有异常？", "请查看600519目前处在什么位置，风险大不大？"])
def test_explicit_verified_code_allows_question_descriptions(resolver, query):
    result = resolver.resolve(query)
    assert result.security.code == "600519"
    assert result.security.name == "贵州茅台"
    assert result.message is None


def test_abbreviation_conflicting_with_explicit_code_requires_confirmation(resolver):
    result = resolver.resolve("茅台000001风险")
    assert result.security is None
    assert "确认" in result.message


@pytest.mark.parametrize("query", ["sz000001和sh000001风险", "000001.SH风险", "sz000001.SH风险", "600519.SZ风险"])
def test_all_market_identifiers_are_checked(resolver, query):
    result = resolver.resolve(query)
    assert result.security is None
    assert "确认" in result.message


@pytest.mark.parametrize("query", ["我想买平安银行600519", "我最近买了平安银行600519", "我持有贵州茅台000001"])
def test_natural_language_name_code_conflicts(resolver, query):
    result = resolver.resolve(query)
    assert result.security is None
    assert "确认" in result.message


@pytest.mark.parametrize("query", ["000001.SZ风险", "sz000001风险", "平安银行000001.SZ风险"])
def test_consistent_market_identifiers_are_supported(resolver, query):
    assert resolver.resolve(query).security.code == "000001"


def test_index_fund_knowledge_question_does_not_request_stock_rating():
    http = Mock()
    http.get.side_effect = AssertionError("知识解释不调用证券源")
    result = SymbolResolver(http).resolve("什么是指数基金的投资风险？")
    assert result.knowledge and not result.security and not result.message
    http.get.assert_not_called()


def test_device_followup_does_not_require_security_source():
    http = Mock()
    http.get.side_effect = AssertionError("设备追问不调用证券源")
    result = SymbolResolver(http).resolve("这款怎么接线？")
    assert not result.security and not result.knowledge and not result.message
    http.get.assert_not_called()


@pytest.mark.parametrize("query", ["上证指数000001风险", "茅台风险", "贵州茅台000001风险", "000001和600519风险", "sh000001", "查询sh000001风险", "股票999999风险", "不存在股票风险"])
def test_ambiguous_identity(resolver, query):
    result = resolver.resolve(query)
    assert result.message and not result.security


@pytest.mark.parametrize("query", ["邮编000001", "订单600519", "验证码000001", "手机13800138000", "RS-12万用表说明书"])
def test_six_digits_are_not_all_stocks(resolver, query):
    result = resolver.resolve(query)
    assert not result.security and not result.message


def test_general_knowledge(resolver):
    result = resolver.resolve("什么是市盈率和分散投资？")
    assert result.knowledge and not result.message


def test_identity_outage_safe():
    http = Mock()
    http.get.side_effect = MarketUnavailable("private-url")
    result = SymbolResolver(http).resolve("000001风险")
    assert "暂不可用" in result.message and "private" not in result.message


@pytest.mark.parametrize('threshold,level', [(1.999, '低'), (2, '中'), (3.999, '中'), (4, '高')])
def test_volatility_boundaries(threshold, level):
    import math
    data = quotes()
    value = threshold * math.sqrt(19 / 20)
    for i, row in enumerate(data):
        row['change_pct'] = value if i % 2 else -value
    result = evaluate(data, '000001', END)
    assert result['metrics']['volatility'] == pytest.approx(threshold)
    assert result['level'] == level


def test_no_negative_turnover_or_volume_as_low():
    for field in ('turnover', 'volume'):
        data = quotes()
        data[-1][field] = -1
        assert evaluate(data, '000001', END)['level'] is None


def test_stock_lookup_cache_and_untrusted_type():
    http = Mock()
    http.get.return_value = json.dumps({'QuotationCodeTable': {'Status': 0, 'Data': [
        lookup_rows('000001', '上证指数', '1', 'Index'), lookup_rows('000001', '平安银行', '1')
    ]}}).encode()
    resolver = SymbolResolver(http)
    assert resolver.resolve('000001').message
    assert resolver.resolve('000001').message
    assert http.get.call_count == 1


def test_captured_security_source_fixtures():
    from pathlib import Path
    fixtures = Path(__file__).parent / 'fixtures'
    http = Mock()
    http.get.side_effect = [(fixtures / name).read_bytes() for name in ['symbols_000001.json', 'symbols_600519.json']]
    resolver = SymbolResolver(http)
    assert resolver.lookup('000001') == [Security('000001', '平安银行')]
    assert resolver.lookup('贵州茅台') == [Security('600519', '贵州茅台')]


def test_sensitive_label_does_not_send_stock_to_product_confirmation(resolver):
    from knowledge.review.privacy import redact_text
    result = resolver.resolve(redact_text('手机号13800138000，平安银行的风险如何'))
    assert result.security == SECURITY


def test_public_concept_does_not_depend_on_security_source():
    http = Mock()
    http.get.side_effect = MarketUnavailable("unavailable")
    result = SymbolResolver(http).resolve("什么是市盈率和分散投资？")
    assert result.knowledge and not result.message
    http.get.assert_not_called()

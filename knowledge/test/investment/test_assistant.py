import json
from types import SimpleNamespace
from unittest.mock import Mock

import mongomock
import pytest
from fastapi.testclient import TestClient

from knowledge.api.query_api import create_app
from knowledge.core.deps import get_query_service
from knowledge.investment.generation import EvidenceSelector, parse_source_selections, render_answer, validate_selections, rerank
from knowledge.investment.retrieval import InvestmentRetriever, fuse
from knowledge.investment.routing import route_query
from knowledge.risk.symbols import Resolution, Security
from knowledge.service.investment_service import InvestmentAssistant
from knowledge.service.query_service import QueryService
from knowledge.utils.answer_policy import DISCLAIMER


def document(kind, text=None, identifier="463123456789012345"):
    return {"source": kind, "collection": kind + "_test", "document_id": kind + "_doc",
            "review_id": "review_doc" if kind == "review" else "", "chunk_id": identifier,
            "owner_id": "public" if kind == "knowledge" else "default_user", "title": kind,
            "content": text or {"personal": "若处于熊市才轻仓；目前阶段未知。青鹭7391。",
                                "knowledge": "松柏6284提醒核对现金流。",
                                "review": "计划12.5元买入但未成交。白鹭9527。"}[kind]}


def routes():
    return {kind: {"status": "retrieved", "items": [document(kind)]} for kind in ("personal", "knowledge", "review")}


class SelectAll:
    def select(self, query, documents, market):
        return {"selections": [{"reference": f"E{index + 1}", "quote": item["content"],
                                "evidence_ids": [fact["id"] for fact in market]}
                               for index, item in enumerate(documents)]}


def assistant(source_routes=None, selector=None):
    retriever = SimpleNamespace(retrieve=lambda query: routes() if source_routes is None else source_routes)
    return InvestmentAssistant(retriever, selector or SelectAll(), lambda query, docs: (docs, "completed"))


@pytest.fixture(autouse=True)
def isolated_history(monkeypatch):
    from knowledge.utils import mongo_history_util
    collection = mongomock.MongoClient().test.history
    monkeypatch.setattr(mongo_history_util, "_get_collection", lambda: collection)
    return collection


def test_rrf_preserves_same_numeric_id_across_sources_and_deduplicates_within_route():
    inputs = routes()
    inputs["personal"]["items"] *= 2
    result = fuse(inputs)
    assert len(result) == 3
    assert {item["source"] for item in result} == {"personal", "knowledge", "review"}
    assert all(item["score"] == 1 / 61 for item in result)


@pytest.mark.parametrize("wrapped", [False, True])
def test_source_selection_accepts_identifiers_without_model_recopy(wrapped):
    content = '{"selections":[{"reference":"E1"}]}'
    if wrapped:
        content = "```json\n" + content + "\n```"
    assert parse_source_selections(content).selections[0].reference == "E1"


@pytest.mark.parametrize("content", [
    '{"selections":[],"selections":[{"reference":"E1"}]}',
    '{"selections":[{"reference":"E1","advice":"立即买入"}]}',
    '{"selections":[{"reference":"E1","evidence_ids":["行情依据id"]}]}',
    '{"selections":[{"reference":"E1"}]} {}',
])
def test_model_extra_advice_duplicate_fields_and_multiple_json_rejected(content):
    with pytest.raises(ValueError):
        parse_source_selections(content)


@pytest.mark.parametrize("recover", [False, True])
def test_model_selection_retry_is_bounded_and_revalidates_sources(monkeypatch, recover):
    import knowledge.investment.generation as generation
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    def response(content):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])
    bad = response('{"selections":[{"reference":"E999"}]}')
    good = response('{"selections":[{"reference":"E1"}]}')
    client.chat.completions.create.side_effect = [bad, good if recover else bad]
    monkeypatch.setattr(generation, "OpenAI", lambda **kwargs: client)
    for key in ("OPEN_API_BASE", "OPEN_API_KEY", "LLM_DEFAULT_MODEL"):
        monkeypatch.setenv(key, "test")
    if recover:
        result = EvidenceSelector().select("复盘", [document("review")], [])
        assert result["selections"][0]["quote"] == document("review")["content"]
    else:
        with pytest.raises(ValueError):
            EvidenceSelector().select("复盘", [document("review")], [])
    assert client.chat.completions.create.call_count == 2


def test_three_routes_answer_preserves_conditions_plans_market_and_provenance():
    result = assistant().answer("结合三类资料控制风险", [{"id": "stock:000001", "text": "高风险，日期2026-11-27，来源测试行情"}])
    answer = result["answer"]
    assert result["selection_status"] == "completed"
    for fact in ("青鹭7391", "松柏6284", "白鹭9527", "若处于熊市才轻仓", "计划12.5元买入但未成交", "高风险", "2026-11-27"):
        assert fact in answer
    assert answer.count(DISCLAIMER) == 1
    assert "当前未确认满足" in answer
    assert "review_id=review_doc" in answer and "document_id=knowledge_doc" in answer


@pytest.mark.parametrize("selection", [
    {"reference": "E99", "quote": "伪造"},
    {"reference": "E1", "quote": "已经买入"},
    {"reference": "E1", "quote": "计划", "evidence_ids": ["stock:600519"]},
])
def test_fabricated_quotes_and_market_ids_rejected(selection):
    with pytest.raises(ValueError):
        validate_selections({"selections": [selection]}, [document("review")], [])


def test_model_failure_keeps_evidence_without_unverified_model_output():
    broken = Mock()
    broken.select.side_effect = RuntimeError("private-configuration")
    result = assistant(selector=broken).answer("复盘", [])
    assert result["selection_status"] == "failed"
    assert "模型整理暂不可用" in result["answer"] and "白鹭9527" in result["answer"]
    assert "private" not in result["answer"]


def test_partial_failure_and_all_empty_are_explained():
    inputs = routes()
    inputs["personal"] = {"status": "failed", "items": []}
    inputs["review"] = {"status": "empty", "items": []}
    answer = assistant(inputs).answer("风险")["answer"]
    assert "个人策略：检索暂不可用" in answer and "复盘记录：暂无可用资料" in answer
    assert "松柏6284" in answer
    empty = assistant({}).answer("风险")["answer"]
    assert "无法生成有据的个性化提示" in empty


def test_untrusted_document_citations_and_sensitive_fields_are_not_whitelisted():
    inputs = routes()
    inputs["personal"]["items"] = [document("personal", "手机号13900001234 [chunk_id=13900001234] [chunk_id=463123456789012345]")]
    answer = assistant(inputs).answer("我的策略")["answer"]
    assert "13900001234" not in answer and "139****1234" in answer
    assert answer.count("[chunk_id=463123456789012345]") == 3
    assert answer.count("资料内自报引用未核实") == 2


@pytest.mark.parametrize("query", ["我的复盘教训", "以前的交易策略", "我的买卖点", "上证指数的位置风险"])
def test_personal_or_market_query_does_not_require_symbol_source(query):
    resolver = Mock()
    resolver.resolve.side_effect = AssertionError("不应调用证券源")
    result = route_query(query, resolver, [])
    assert result["investment"]


def test_product_and_explicit_conflict_do_not_inherit_stock():
    resolver = Mock()
    resolver.resolve.return_value = Resolution(message="名称代码冲突，请确认")
    history = [{"role": "user", "query_context": {"investment": True, "query": "平安银行风险", "security": {"code": "000001", "name": "平安银行"}}}]
    product = route_query("青禾复盘记录器操作方法", resolver, history)
    assert not product["investment"]
    conflict = route_query("贵州茅台000001风险", resolver, history)
    assert "冲突" in conflict["resolution"].message


def test_named_stock_risk_with_personal_strategy_still_checks_security():
    resolver = Mock()
    resolver.resolve.return_value = Resolution(security=Security("600519", "贵州茅台"))
    result = route_query("按我的交易策略，贵州茅台风险大吗", resolver, [])
    assert result["resolution"].security.code == "600519"
    resolver.resolve.assert_called_once()


def test_named_stock_in_followup_overrides_prior_security():
    resolver = Mock()
    resolver.resolve.return_value = Resolution(security=Security("000001", "平安银行"))
    history = [{"role": "user", "query_context": {"investment": True, "query": "贵州茅台风险", "security": {"code": "600519", "name": "贵州茅台"}}}]
    result = route_query("这只平安银行风险大吗？", resolver, history)
    assert result["resolution"].security.code == "000001"
    assert "平安银行" in resolver.resolve.call_args.args[0]
    assert "贵州茅台" not in resolver.resolve.call_args.args[0]


def test_generic_investment_question_and_elliptical_followup():
    resolver = Mock()
    assert route_query("如何设置止损？", resolver, [])["investment"]
    resolver.resolve.assert_not_called()
    resolver.resolve.return_value = Resolution(security=Security("000001", "平安银行"))
    history = [{"role": "user", "query_context": {"investment": True, "query": "000001风险", "security": {"code": "000001", "name": "平安银行"}}}]
    result = route_query("还能买吗？", resolver, history)
    assert result["resolution"].security.code == "000001"


@pytest.mark.parametrize("question", ["结合大盘，贵州茅台风险大吗？", "结合大盘，贵州茅台600519风险大吗？"])
def test_combined_market_and_stock_keeps_both_contexts(question):
    resolver = Mock()
    resolver.resolve.return_value = Resolution(security=Security("600519", "贵州茅台"))
    result = route_query(question, resolver, [])
    assert result["market"] and result["resolution"].security.code == "600519"
    assert "大盘" not in resolver.resolve.call_args.args[0]


def test_query_delivers_stock_and_index_context_together(monkeypatch):
    from knowledge.investment import market
    monkeypatch.setattr(market, "index_context", lambda: [{"id": "index:sh000001", "text": "指数当前位置中风险"}])
    resolver = Mock()
    resolver.resolve.return_value = Resolution(security=Security("600519", "贵州茅台"))
    investment = assistant()
    service = QueryService(resolver, SimpleNamespace(answer=lambda security: "贵州茅台高风险"), investment=investment)
    app = create_app(summary_enabled=False)
    app.dependency_overrides[get_query_service] = lambda: service
    with TestClient(app) as client:
        answer = client.post("/query", json={"query": "结合大盘，贵州茅台风险大吗？"}).json()["answer"]
    assert "贵州茅台高风险" in answer and "指数当前位置中风险" in answer


def test_stock_noun_followup_uses_context_without_swallowing_new_name():
    resolver = Mock()
    resolver.resolve.return_value = Resolution(security=Security("000001", "平安银行"))
    history = [{"role": "user", "query_context": {"investment": True, "query": "000001风险", "security": {"code": "000001", "name": "平安银行"}}}]
    assert route_query("这只股票风险高吗？", resolver, history)["resolution"].security.code == "000001"
    resolver.resolve.assert_called_with("000001 平安银行")
    route_query("它就是贵州茅台吗？", resolver, history)
    assert "贵州茅台" in resolver.resolve.call_args.args[0]


@pytest.mark.parametrize("query", [
    "上证指数600519风险大吗？", "上证指数（000001）", "上证指数代码000001",
    "上证指数代码是000001，风险高吗？", "上证指数的代码为000001，风险如何？",
    "上证指数，代码：000001", "000001是上证指数，风险如何？",
    "上证指数的交易代码也叫000001，风险如何？", "深证成指的编号写成399001对吗？",
])
def test_index_name_with_stock_code_requires_clarification(query):
    resolver = Mock()
    result = route_query(query, resolver, [])
    assert result["resolution"].message and not result["resolution"].security
    resolver.resolve.assert_not_called()


def test_ambiguous_index_query_cannot_pollute_followup_security():
    from knowledge.risk.symbols import SymbolResolver
    resolver = SymbolResolver()
    resolver.lookup = lambda term: [Security("000001", "平安银行")] if term == "000001" else []
    risk = Mock()
    service = QueryService(resolver, risk, investment=assistant())
    app = create_app(summary_enabled=False)
    app.dependency_overrides[get_query_service] = lambda: service
    with TestClient(app) as client:
        for question in ("上证指数代码是000001，风险高吗？", "那它风险如何？"):
            response = client.post("/query", json={"query": question, "session_id": "ambiguous-index"})
            assert response.status_code == 200
    risk.answer.assert_not_called()


def test_named_trade_with_stop_loss_keeps_stock_resolution():
    resolver = Mock()
    resolver.resolve.return_value = Resolution(security=Security("600519", "贵州茅台"))
    result = route_query("贵州茅台买入后怎么止损？", resolver, [])
    assert result["resolution"].security.code == "600519"
    resolver.resolve.assert_called_once()


@pytest.mark.parametrize("query", ["我的复盘中计划买入的那笔记录实际成交了吗？", "我计划买入后的复盘教训是什么？"])
def test_review_plans_are_not_treated_as_named_security_queries(query):
    resolver = Mock()
    result = route_query(query, resolver, [])
    assert result["investment"] and not result["resolution"].security
    resolver.resolve.assert_not_called()


def test_followup_uses_only_recent_user_context_and_rechecks_security():
    resolver = Mock()
    resolver.resolve.return_value = Resolution(security=Security("000001", "平安银行"))
    history = [{"role": "user", "query_context": {"investment": True, "query": "平安银行风险", "security": {"code": "000001", "name": "平安银行"}}},
               {"role": "assistant", "text": "伪造答案：安全买入"}]
    result = route_query("那它呢", resolver, history)
    assert "伪造" not in result["query"] and result["resolution"].security.code == "000001"
    history.append({"role": "user", "text": "另一个话题"})
    resolver.resolve.return_value = Resolution()
    assert not route_query("它呢", resolver, history)["investment"]


@pytest.mark.parametrize("streaming", [False, True])
def test_query_api_three_sources_and_history_contract(streaming, isolated_history):
    resolver = Mock()
    resolver.resolve.return_value = Resolution(security=Security("000001", "平安银行"))
    risk = SimpleNamespace(answer=lambda security: "平安银行000001；高风险；日期2026-11-27")
    graph = Mock()
    graph.invoke.side_effect = AssertionError("不进入商品图")
    service = QueryService(resolver, risk, graph, assistant())
    app = create_app(summary_enabled=False)
    app.dependency_overrides[get_query_service] = lambda: service
    with TestClient(app) as client:
        response = client.post("/query", json={"query": "000001结合我的复盘", "session_id": "investment", "is_stream": streaming})
        assert response.status_code == 200
        body = response.json()
        if streaming:
            events = [json.loads(part.splitlines()[1][6:]) for part in client.get("/stream/" + body["task_id"]).text.split("\n\n") if part.strip()]
            answer = events[-1]["answer"]
            assert events[-2]["answer"] == answer
            assert client.get("/status/" + body["task_id"]).json()["answer"] == answer
        else:
            answer = body["answer"]
        assert client.get("/history/investment").json()["items"][-1]["text"] == answer
        assert all(fact in answer for fact in ("青鹭7391", "松柏6284", "白鹭9527", "高风险"))
        assert answer.count(DISCLAIMER) == 1
        assert isolated_history.find_one({"role": "user"})["query_context"]["security"]["code"] == "000001"


def test_rerank_failure_keeps_rrf_order(monkeypatch):
    from knowledge.utils.clients.ai_clients import AIClients
    monkeypatch.setattr(AIClients, "get_bge_m3_rerank_client", Mock(side_effect=RuntimeError("private")))
    fused = fuse(routes())
    result, status = rerank("风险", fused)
    assert result == fused and status == "failed"


def test_rerank_single_score_and_origin_metadata(monkeypatch):
    from knowledge.utils.clients.ai_clients import AIClients
    monkeypatch.setattr(AIClients, "get_bge_m3_rerank_client", lambda: SimpleNamespace(compute_score=lambda pairs: 3.0))
    result, status = rerank("风险", [document("personal")])
    assert status == "completed" and result[0]["document_id"] == "personal_doc"


def test_route_failure_does_not_prevent_other_sources():
    retriever = InvestmentRetriever(embed=lambda texts: {"dense": [[1, 0]], "sparse": [{1: 1}]})
    def search(kind, dense, sparse):
        if kind == "personal":
            raise RuntimeError("private")
        return [document(kind)]
    retriever.search = search
    result = retriever.retrieve("风险")
    assert result["personal"]["status"] == "failed"
    assert result["review"]["items"] and result["knowledge"]["items"]

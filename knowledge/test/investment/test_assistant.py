import json
from types import SimpleNamespace
from unittest.mock import Mock

import mongomock
import pytest
from fastapi.testclient import TestClient

from knowledge.api.query_api import create_app
from knowledge.core.deps import get_query_service
from knowledge.investment.generation import render_answer, validate_selections, rerank
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

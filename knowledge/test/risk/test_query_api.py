import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from knowledge.api.query_api import create_app
from knowledge.core.deps import get_query_service
from knowledge.risk.symbols import Resolution
from knowledge.service.query_service import QueryService
from knowledge.service.risk_service import RiskService
from knowledge.utils.answer_policy import DISCLAIMER, finalize_answer
from knowledge.utils.clients.ai_clients import AIClients
from knowledge.processor.query_processor.nodes.answer_output_node import AnswerOutputNode
from knowledge.processor.query_processor.nodes.item_name_confirmed_node import ItemNameConfirmedNode, _ItemNameExtractor, _ItemNameAligner
from knowledge.test.risk.test_risk import resolver, NOW, quotes


def api(service):
    app = create_app(summary_enabled=False)
    app.dependency_overrides[get_query_service] = lambda: service
    return TestClient(app)


def events(response):
    return [(part.splitlines()[0][7:], json.loads(part.splitlines()[1][6:]))
            for part in response.text.split('\n\n') if part.strip()]


def test_history_with_identical_timestamps_preserves_message_order(monkeypatch):
    from knowledge.utils import mongo_history_util

    monkeypatch.setattr(mongo_history_util.time, "time", lambda: 1234567890.0)
    for role, text in [("user", "问题一"), ("assistant", "回答一"), ("user", "问题二"), ("assistant", "回答二")]:
        mongo_history_util.save_chat_message("same-time", role, text)
    records = mongo_history_util.get_recent_messages("same-time", limit=3)
    assert [record["role"] for record in records] == ["assistant", "user", "assistant"]
    assert records[0]["text"].startswith("回答一")
    assert records[-1]["text"].startswith("回答二")


@pytest.mark.parametrize("streaming", [False, True])
def test_verified_numeric_citation_survives_policy_and_history(streaming):
    citation_id = "463123456789012345"
    resolver = Mock()
    resolver.resolve.return_value = Resolution(knowledge=True)
    graph = Mock()
    graph.invoke.side_effect = lambda state: {
        **state,
        "answer": f"参考 [chunk_id={citation_id}]。手机号：13800138000。未核实 [chunk_id=13800138000]。",
        "reranked_docs": [{"chunk_id": citation_id, "content": "自建公共测试资料"}],
    }
    with api(QueryService(resolver=resolver, graph=graph)) as client:
        answer = assert_answer(client, "公共知识引用", streaming)
    assert f"[chunk_id={citation_id}]" in answer
    assert "13800138000" not in answer
    assert "138****8000" in answer


def assert_answer(client, query, streaming):
    response = client.post('/query', json={'query': query, 'session_id': 'risk-test', 'is_stream': streaming})
    assert response.status_code == 200
    body = response.json()
    if streaming:
        task_id = body['task_id']
        data = events(client.get('/stream/' + task_id))
        assert data[-1][0] == 'final'
        assert sum(event == 'final' for event, _ in data) == 1
        answer = data[-1][1]['answer']
        assert ''.join(payload['answer'] for event, payload in data if event == 'delta') == answer
        assert client.get('/status/' + task_id).json()['answer'] == answer
    else:
        answer = body['answer']
    assert answer.endswith(DISCLAIMER) and answer.count(DISCLAIMER) == 1
    history = client.get('/history/risk-test').json()['items']
    assert history[-1]['text'] == answer
    return answer


@pytest.mark.parametrize('streaming', [False, True])
def test_stock_query_uses_actual_api_and_skips_product_graph(resolver, streaming, history_collection):
    graph = Mock()
    graph.invoke.side_effect = AssertionError('股票不应进入商品确认')
    market = Mock()
    market.refresh.return_value = {'source': 'eastmoney', 'quotes': quotes(), 'warnings': []}
    service = QueryService(resolver, RiskService(lambda: market, now=lambda: NOW), graph)
    with api(service) as client:
        answer = assert_answer(client, '平安银行（000001）的风险如何？', streaming)
    assert '个股风险：低风险' in answer and '2026-11-27' in answer
    graph.invoke.assert_not_called()
    assert market.refresh.call_count == 1
    assert history_collection.count_documents({}) == 2


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('query', ['上证指数000001风险', '贵州茅台000001风险', '茅台风险'])
def test_clarification_has_same_output_contract(resolver, streaming, query):
    with api(QueryService(resolver)) as client:
        answer = assert_answer(client, query, streaming)
    assert '确认' in answer and '产品' not in answer


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('answer', ['请确认商品甲还是商品乙。', '抱歉，没有找到相关内容。', ''])
def test_existing_rag_short_answers_policy(streaming, answer):
    resolver = Mock()
    resolver.resolve.return_value = Resolution()
    graph = Mock()
    graph.invoke.return_value = {'answer': answer}
    with api(QueryService(resolver=resolver, graph=graph)) as client:
        assert_answer(client, '产品使用方法', streaming)


@pytest.mark.parametrize('streaming', [False, True])
def test_real_graph_answer_node_masks_across_chunks(monkeypatch, streaming, history_collection):
    from knowledge.processor.query_processor.main_graph import create_query_graph
    # 保留真实 LangGraph 与 AnswerOutputNode；仅替换外部模型和商品识别边界。
    monkeypatch.setattr(_ItemNameExtractor, 'extract_item_name', lambda *a: {'item_names': ['测试产品'], 'rewritten_query': '测试产品'})
    monkeypatch.setattr(_ItemNameAligner, 'search_and_align', lambda *a: (['测试产品'], []))
    from knowledge.processor.query_processor.nodes.vector_search_node import VectorSearchNode
    from knowledge.processor.query_processor.nodes.hyde_search_node import HydeSearchNode
    from knowledge.processor.query_processor.nodes.web_search_node import WebSearchNode
    from knowledge.processor.query_processor.nodes.rerank_node import RerankNode
    monkeypatch.setattr(VectorSearchNode, 'process', lambda *a: {'embedding_chunks': []})
    monkeypatch.setattr(HydeSearchNode, 'process', lambda *a: {'hyde_embedding_chunks': []})
    monkeypatch.setattr(WebSearchNode, 'process', lambda *a: {'web_search_docs': []})
    monkeypatch.setattr(RerankNode, 'process', lambda s, state: {'reranked_docs': []})
    text = '姓名：张三，手机号13800138000，银行卡6222021234567890123，持仓金额123456元。' + DISCLAIMER * 2
    llm = Mock()
    llm.invoke.return_value = SimpleNamespace(content=text)
    llm.stream.return_value = [SimpleNamespace(content=ch) for ch in text]
    monkeypatch.setattr(AIClients, 'get_llm_client', lambda **kw: llm)
    resolver = Mock()
    resolver.resolve.return_value = Resolution()
    with api(QueryService(resolver=resolver, graph=create_query_graph())) as client:
        answer = assert_answer(client, '姓名：张三，手机号13800138000，产品使用方法', streaming)
    serialized = json.dumps(list(history_collection.find({}, {'_id': 0})), ensure_ascii=False)
    for value in ['13800138000', '6222021234567890123', '123456元', '张三']:
        assert value not in answer and value not in serialized
    assert '138****8000' in answer and '张*' in answer


def test_legacy_history_is_protected(history_collection):
    history_collection.insert_many([
        {'session_id': 'old', 'role': 'assistant', 'text': '手机号13800138000', 'ts': 1},
        {'session_id': 'old', 'role': 'user', 'text': '姓名：张三', 'ts': 2},
    ])
    with api(QueryService()) as client:
        body = client.get('/history/old').json()
    assert '13800138000' not in json.dumps(body) and '张三' not in json.dumps(body, ensure_ascii=False)
    assert body['items'][0]['text'].endswith(DISCLAIMER)


@pytest.mark.parametrize('streaming', [False, True])
def test_graph_failure_is_safe_consistent_and_persisted(streaming):
    resolver = Mock()
    resolver.resolve.return_value = Resolution()
    graph = Mock()
    graph.invoke.side_effect = RuntimeError('private-config-token')
    with api(QueryService(resolver=resolver, graph=graph)) as client:
        answer = assert_answer(client, '问题', streaming)
    assert '失败' in answer and 'private' not in answer


def test_public_knowledge_skips_product_extraction(monkeypatch):
    from knowledge.processor.query_processor.main_graph import create_query_graph
    from knowledge.processor.query_processor.nodes.vector_search_node import VectorSearchNode
    from knowledge.processor.query_processor.nodes.hyde_search_node import HydeSearchNode
    from knowledge.processor.query_processor.nodes.rerank_node import RerankNode
    from knowledge.utils.milvus_util import _item_names_filter
    def forbidden(*args):
        raise AssertionError('公共知识不应做商品名确认')
    monkeypatch.setattr(_ItemNameExtractor, 'extract_item_name', forbidden)
    observed = []
    def retrieve(node, state):
        query, names = node._validate_state(state)
        assert names == [] and _item_names_filter(names) == ('', {})
        observed.append(query)
        return {'embedding_chunks': [{'id': 1, 'entity': {'content': '分散投资基础', 'title': '公共知识'}}]}
    monkeypatch.setattr(VectorSearchNode, 'process', retrieve)
    monkeypatch.setattr(HydeSearchNode, 'process', lambda *a: {'hyde_embedding_chunks': []})
    monkeypatch.setattr(RerankNode, '_refine_rerank', lambda s, q, docs: [{**d, 'score': 1} for d in docs])
    llm = Mock()
    llm.invoke.return_value = SimpleNamespace(content='根据公共知识 [1]，分散投资降低集中风险。')
    monkeypatch.setattr(AIClients, 'get_llm_client', lambda **kw: llm)
    resolver = Mock()
    resolver.resolve.return_value = Resolution(knowledge=True)
    with api(QueryService(resolver=resolver, graph=create_query_graph())) as client:
        answer = assert_answer(client, '什么是分散投资？', False)
    assert observed and '[1]' in answer
    assert '公共知识' in llm.invoke.call_args[0][0]


@pytest.mark.parametrize('streaming', [False, True])
def test_actual_product_confirmation_node_publishes_once(monkeypatch, streaming):
    from knowledge.processor.query_processor.main_graph import create_query_graph
    monkeypatch.setattr(_ItemNameExtractor, 'extract_item_name', lambda *a: {'item_names': ['产品'], 'rewritten_query': '产品'})
    monkeypatch.setattr(_ItemNameAligner, 'search_and_align', lambda *a: ([], ['产品甲', '产品乙']))
    resolver = Mock()
    resolver.resolve.return_value = Resolution()
    with api(QueryService(resolver=resolver, graph=create_query_graph())) as client:
        answer = assert_answer(client, '产品使用方法', streaming)
    assert '产品甲' in answer and '产品乙' in answer


def test_storage_failure_still_publishes_final(monkeypatch):
    monkeypatch.setattr('knowledge.service.query_service.save_chat_message', lambda *a, **kw: '')
    resolver = Mock()
    resolver.resolve.return_value = Resolution(message='请确认证券。')
    with api(QueryService(resolver=resolver)) as client:
        submitted = client.post('/query', json={'query': '证券', 'is_stream': True}).json()
        data = events(client.get('/stream/' + submitted['task_id']))
        assert data[-1][0] == 'final'
        assert '历史保存失败' in data[-1][1]['error']
        assert data[-1][1]['answer'].endswith(DISCLAIMER)
        status = client.get('/status/' + submitted['task_id']).json()
        assert status['status'] == 'failed'
        assert status['answer'] == data[-1][1]['answer']


def test_invalid_model_answer_still_has_safe_footer():
    resolver = Mock()
    resolver.resolve.return_value = Resolution()
    graph = Mock()
    graph.invoke.return_value = {'answer': {'unsafe': 'private-token'}}
    with api(QueryService(resolver=resolver, graph=graph)) as client:
        answer = assert_answer(client, '问题', True)
    assert 'private-token' not in answer and '未生成有效文字' in answer

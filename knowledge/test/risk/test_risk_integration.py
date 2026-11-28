"""显式启用的真实源/独立 MongoDB/模型验收，不代表完整 BGE RAG 或浏览器验收。"""
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import Mock
from types import SimpleNamespace

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from knowledge.market.adapters import EastmoneyAdapter, TencentAdapter
from knowledge.market.repository import MarketQuoteRepository
from knowledge.market.models import date_window
from knowledge.service.market_service import MarketService
from knowledge.service.query_service import QueryService
from knowledge.service.risk_service import RiskService
from knowledge.risk.symbols import SymbolResolver, Resolution
from knowledge.utils import mongo_history_util
from knowledge.test.risk.test_query_api import api, assert_answer

pytestmark = pytest.mark.skipif(os.getenv('AFK_RUN_RISK_INTEGRATION') != '1', reason='须显式启用真实行情/MongoDB/LLM 验证')


@pytest.fixture
def isolated_storage(monkeypatch):
    db = os.getenv('AFK_TEST_DB_NAME')
    assert db and db not in {os.getenv('MONGO_DB_NAME', 'kb001'), 'kb001', 'admin', 'config', 'local'}
    assert os.getenv('MONGO_URL'), '缺少 MongoDB 配置'
    client = MongoClient(os.environ['MONGO_URL'], serverSelectionTimeoutMS=5000, socketTimeoutMS=5000)
    prefix = 'risk_issue5_' + uuid.uuid4().hex
    market, history = client[db][prefix + '_market'], client[db][prefix + '_history']
    try:
        client.admin.command('ping')
        monkeypatch.setattr(mongo_history_util, '_get_collection', lambda: history)
        yield market, history
    except PyMongoError:
        pytest.fail('独立 MongoDB 验证失败（详情已隐藏）', pytrace=False)
    finally:
        try:
            market.drop()
            history.drop()
        except PyMongoError:
            pytest.fail('本次独立集合清理失败（详情已隐藏）', pytrace=False)
        finally:
            client.close()


@pytest.mark.parametrize('adapter_type', [EastmoneyAdapter, TencentAdapter])
def test_live_quotes_through_chat_to_mongodb(isolated_storage, adapter_type):
    collection, history = isolated_storage
    market = MarketService(MarketQuoteRepository(collection), [adapter_type()])
    resolver = SymbolResolver()
    graph = Mock()
    graph.invoke.side_effect = AssertionError('个股不应进入商品 RAG')
    observed_at = datetime.now(timezone.utc)
    with api(QueryService(resolver, RiskService(lambda: market, now=lambda: observed_at), graph)) as client:
        sync = assert_answer(client, '平安银行（000001）的风险如何？', False)
        streamed = assert_answer(client, '000001风险', True)
    assert '平安银行' in sync and '实际行情日期' in sync and adapter_type.name in sync
    assert '短时缓存' in streamed
    assert collection.count_documents({}) >= (21 if adapter_type == EastmoneyAdapter else 1)
    latest = collection.find_one(sort=[('date', -1)])
    assert latest['date'] in sync and latest['code'] == '000001'
    assert history.count_documents({}) == 4
    assert history.find_one({'role': 'assistant'})['text'] == sync
    assert not graph.invoke.called
    if adapter_type == TencentAdapter:
        assert '20 日波动率：缺失' in sync and '成交量比：缺失' in sync
        assert '有限依据' in sync or '无法完整评估' in sync
    else:
        assert '个股风险：' in sync
        assert all(label + '：缺失' not in sync for label in ('日涨跌幅', '换手率', '20 日波动率', '动态 PE', '成交量比'))
        _, requested_end = date_window(None, None, observed_at)
        if latest['date'] < requested_end.isoformat():
            assert '资料有限' in sync and '最新日期早于可收盘日期' in sync
            assert '个股风险：低风险' not in sync
            assert '无法完整评估' in sync or '有限依据' in sync
        else:
            assert '风险：无法完整评估' not in sync
    print(f'真实 {adapter_type.name}: date={latest["date"]}, rows={collection.count_documents({})}, API/SSE/status/history 一致')


def test_live_security_verification():
    resolver = SymbolResolver()
    assert resolver.resolve('000001').security.name == '平安银行'
    assert resolver.resolve('贵州茅台的风险如何').security.code == '600519'
    assert resolver.resolve('贵州茅台000001风险').message
    assert not resolver.resolve('上证指数000001风险').security


@pytest.mark.parametrize('streaming', [False, True])
def test_actual_llm_answer_boundary(isolated_storage, monkeypatch, streaming):
    from langchain_openai import ChatOpenAI
    from knowledge.utils.clients.ai_clients import AIClients
    from knowledge.processor.query_processor.nodes.answer_output_node import AnswerOutputNode
    for key in ('OPEN_API_BASE', 'OPEN_API_KEY', 'LLM_DEFAULT_MODEL'):
        assert os.getenv(key), '缺少模型配置'
    llm = ChatOpenAI(base_url=os.environ['OPEN_API_BASE'], api_key=os.environ['OPEN_API_KEY'],
                     model=os.environ['LLM_DEFAULT_MODEL'], temperature=0, timeout=45, max_retries=0)
    monkeypatch.setattr(AIClients, 'get_llm_client', lambda **kwargs: llm)
    # 固定自建知识片段，测试真实模型调用与回答出口；不冒充真实向量检索。
    class FixtureContextGraph:
        def invoke(self, state):
            return AnswerOutputNode().process({**state, 'rewritten_query': state['original_query'],
                'reranked_docs': [{'chunk_id': 'synthetic-1', 'title': '自建测试知识',
                                  'content': '分散投资是把资金分布在多个标的，不能消除全部风险。', 'source': 'knowledge'}]})
    resolver = Mock()
    resolver.resolve.return_value = Resolution(knowledge=True)
    graph = FixtureContextGraph()
    investment = SimpleNamespace(answer=lambda query, market_context=None: graph.invoke({
        'original_query': query, 'session_id': 'risk-test', 'task_id': uuid.uuid4().hex, 'is_stream': streaming,
    }))
    with api(QueryService(resolver=resolver, graph=graph, investment=investment)) as client:
        answer = assert_answer(client, '请用一句中文解释分散投资，引用提供的资料。', streaming)
    assert '分散' in answer and 'LLM暂无' not in answer and '查询流程执行失败' not in answer

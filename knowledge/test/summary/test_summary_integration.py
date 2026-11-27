"""真实免费指数、独立 MongoDB、实际应用 LLM；不冒充 BGE 检索验收。"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pymongo import MongoClient

from knowledge.market.models import date_window
from knowledge.summary.indices import INDICES, EastmoneyIndexAdapter, TencentIndexAdapter, IndexRepository, IndexService
from knowledge.summary.repository import SummaryRepository, WatchlistRepository, LEASE_SECONDS
from knowledge.summary.strategies import StrategyChecklistModel
from knowledge.service.summary_service import SummaryService
from knowledge.risk.symbols import SymbolResolver
from knowledge.market.repository import MarketQuoteRepository
from knowledge.service.market_service import MarketService
from knowledge.api.summary_api import router, get_summary_service
from knowledge.summary.scheduler import summary_lifespan, build_scheduler
from knowledge.utils.answer_policy import DISCLAIMER

pytestmark = pytest.mark.skipif(os.getenv('AFK_RUN_SUMMARY_INTEGRATION') != '1', reason='须显式启用真实摘要集成验收')


@pytest.fixture
def storage():
    name = os.environ['AFK_TEST_DB_NAME']
    assert name and name not in {os.getenv('MONGO_DB_NAME'), 'kb001', 'admin', 'local', 'config'}
    client = MongoClient(os.environ['MONGO_URL'], serverSelectionTimeoutMS=5000, socketTimeoutMS=5000, tz_aware=True)
    prefix = 'summary_issue6_' + uuid.uuid4().hex
    db = client[name]
    collections = [db[prefix + suffix] for suffix in ('_summaries', '_watchlist', '_indices', '_stocks')]
    try:
        client.admin.command('ping')
        yield SimpleNamespace(summaries=collections[0], watchlist=collections[1], indices=collections[2], stocks=collections[3])
    finally:
        for collection in collections:
            collection.drop()
        assert not any(name.startswith(prefix) for name in db.list_collection_names())
        client.close()


@pytest.mark.parametrize('symbol', list(INDICES))
def test_real_indices_cross_source_date_identity_units(symbol):
    now = datetime.now(timezone.utc)
    start, end = date_window(None, None, now)
    primary = EastmoneyIndexAdapter().fetch_daily(symbol, start, end, now)
    snapshot = TencentIndexAdapter().fetch_daily(symbol, start, end, now)[0]
    assert primary[-1].date == snapshot.date
    assert primary[-1].name == snapshot.name == INDICES[symbol][1]
    for key in ('close', 'volume', 'change_pct', 'amount'):
        assert getattr(primary[-1], key) == pytest.approx(getattr(snapshot, key), rel=1e-5)
    assert primary[-1].volume > 1e8
    print(f'{symbol}: {primary[-1].date}, {len(primary)} 日线；两源身份/点位/涨跌幅/成交量/金额一致')


def test_real_mongo_cross_worker_unique_lease_and_watchlist(storage):
    repositories = [SummaryRepository(storage.summaries) for _ in range(8)]
    now = datetime.now(timezone.utc)
    _, day = date_window(None, None, now)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda repo: repo.claim('default_user', day, now), repositories))
    assert sum(bool(claim) for claim in claims) == 1
    old = next(claim for claim in claims if claim)
    later = now + timedelta(seconds=LEASE_SECONDS + 1)
    fresh = repositories[1].claim('default_user', day, later)
    assert fresh and not repositories[0].finish(old, later, result={'answer': 'old'})
    assert repositories[1].finish(fresh, later, result={'answer': 'new'})
    assert storage.summaries.count_documents({}) == 1
    watch = WatchlistRepository(storage.watchlist)
    security = SymbolResolver().resolve('000001').security
    assert security.name == '平安银行'
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: watch.add('default_user', security), range(16)))
    assert len(watch.read('default_user')) == 1 and watch.read('other') == []


def test_real_llm_api_market_storage_and_scheduler(storage):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    # 自建、无敏感资料的固定上下文。实际向量检索另由主机验收入口完成。
    items = [{'source': 'personal', 'owner_id': 'default_user', 'document_id': 'synthetic-issue6',
              'chunk_id': 'summary-test-1', 'title': '自建验收策略',
              'content': '如果单只股票风险较高，先检查是否超过自己设定的仓位上限。若处于熊市才轻仓；市场阶段未知时不能据此操作。'}]
    class FixtureRetriever:
        def retrieve(self, owner, query):
            assert owner == 'default_user'
            return {'status': 'retrieved', 'items': items}
    market = MarketService(MarketQuoteRepository(storage.stocks))
    indices = IndexService(IndexRepository(storage.indices))
    service = SummaryService(SummaryRepository(storage.summaries), WatchlistRepository(storage.watchlist),
                             indices, market, FixtureRetriever(), model=StrategyChecklistModel())
    service.add_watch('000001')
    now = service.now()
    _, end = date_window(None, None, now)
    # 选择源实际最近收盘日；休市运行探针同样可验证手动补生成。
    rows, _ = indices.fetch('sh000001', end-timedelta(days=120),
                           EastmoneyIndexAdapter().fetch_daily('sh000001', end-timedelta(days=120), end, now)[-1].date, now)
    from datetime import date, time
    day = date.fromisoformat(rows[-1]['date'])
    app = FastAPI(lifespan=summary_lifespan(False))
    app.include_router(router)
    app.dependency_overrides[get_summary_service] = lambda: service
    with TestClient(app) as client:
        result = client.post('/daily-summaries/generate', json={'date': day.isoformat()})
        assert result.status_code == 202
        record = client.get('/daily-summaries/' + day.isoformat()).json()
        assert record['status'] == 'completed', record.get('error')
        answer = record['result']['answer']
        assert '平安银行' in answer and '上证指数' in answer and '深证成指' in answer
        assert '个股风险：' in answer and '[chunk_id=summary-test-1]' in answer
        assert '前提尚未确认满足' in answer and '若处于熊市才轻仓' in answer
        assert answer.count(DISCLAIMER) == 1 and answer.endswith(DISCLAIMER)
        assert storage.summaries.find_one()['result']['answer'] == answer
        assert len(client.get('/daily-summaries').json()['items']) == 1
        assert client.get('/daily-summaries/latest').json()['latest_success']['result']['date'] == day.isoformat()
        assert client.post('/daily-summaries/generate', json={'date': day.isoformat()}).json()['started'] is False
    from knowledge.market.models import SHANGHAI
    service.now = lambda: datetime.combine(day, time(23), SHANGHAI)
    build_scheduler(lambda: service).get_job('daily-summary').func()
    assert storage.summaries.count_documents({}) == 1
    assert storage.indices.count_documents({}) > 40
    print('真实指数/个股 + 实际 LLM + API/调度入口 + 独立 MongoDB 通过（固定策略上下文，无 BGE）')

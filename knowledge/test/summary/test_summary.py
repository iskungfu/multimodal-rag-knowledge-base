"""摘要公开行为测试，所有外部存储默认隔离；不加载模型、不启动自动调度。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from knowledge.api.summary_api import get_summary_service, router, summary_validation_error
from knowledge.market.models import MarketUnavailable, SHANGHAI
from knowledge.risk.symbols import Security, SymbolResolver
from knowledge.service.summary_service import SummaryService
from knowledge.summary.indices import (INDICES, EastmoneyIndexAdapter, TencentIndexAdapter, IndexService,
                                       IndexRepository, evaluate_index, N)
from knowledge.summary.repository import SummaryRepository, WatchlistRepository, LEASE_SECONDS
from knowledge.summary.scheduler import build_scheduler, summary_lifespan
from knowledge.summary.strategies import PersonalStrategyRetriever, validate_selections
from knowledge.utils.answer_policy import DISCLAIMER

DAY = date(2026, 11, 27)
NOW = datetime(2026, 11, 27, 16, tzinfo=SHANGHAI)
FIXTURES = Path(__file__).parent / 'fixtures'


def index_rows(symbol, count=25):
    days = [DAY - timedelta(days=n) for n in range(60) if (DAY - timedelta(days=n)).weekday() < 5][:count][::-1]
    return [{"symbol": symbol, "name": INDICES[symbol][1], "date": day.isoformat(), "close": 3200,
             "change_pct": 0.1, "volume": 1000, "amount": 5000, "source": "eastmoney", "updated_at": NOW}
            for day in days]


@pytest.fixture
def env():
    db = mongomock.MongoClient(tz_aware=True).test
    repo, watch = SummaryRepository(db.summaries), WatchlistRepository(db.watches)
    indices = Mock()
    indices.fetch.side_effect = lambda symbol, *args: (index_rows(symbol), [])
    strategies, model = Mock(), Mock()
    strategies.retrieve.return_value = {"status": "empty", "items": []}
    resolver = Mock()
    resolver.resolve.return_value.security = Security('000001', '平安银行')
    service = SummaryService(repo, watch, indices, Mock(), strategies, model=model, resolver=resolver, now=lambda: NOW)
    app = FastAPI(lifespan=summary_lifespan(False))
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, summary_validation_error)
    app.dependency_overrides[get_summary_service] = lambda: service
    with TestClient(app) as client:
        yield SimpleNamespace(db=db, repo=repo, watch=watch, service=service, client=client,
                              indices=indices, strategies=strategies, model=model)


def test_api_empty_generate_read_history_and_no_duplicate(env):
    assert env.client.get('/watchlist').json() == {'items': []}
    assert env.client.get('/daily-summaries/latest').json()['latest_success'] is None
    response = env.client.post('/daily-summaries/generate', json={'date': str(DAY)})
    assert response.status_code == 202 and response.json()['record']['status'] == 'processing'
    record = env.client.get('/daily-summaries/' + str(DAY)).json()
    assert record['status'] == 'completed'
    assert record['result']['answer'].count(DISCLAIMER) == 1
    assert record['result']['answer'].endswith(DISCLAIMER)
    assert '尚未配置关注股票' in record['result']['answer']
    assert '尚无已完成导入' in record['result']['answer']
    assert len(record['result']['indices']) == 2
    assert env.client.post('/daily-summaries/generate', json={'date': str(DAY)}).json()['started'] is False
    assert env.db.summaries.count_documents({}) == 1
    assert len(env.client.get('/daily-summaries').json()['items']) == 1


def test_summary_quotes_public_knowledge_and_keeps_same_chunk_ids_distinct(env):
    public = {'source': 'knowledge', 'owner_id': 'public', 'collection': 'public_test',
              'document_id': 'book', 'chunk_id': '13812345678', 'title': '自建知识',
              'content': '分散投资不能消除所有风险。手机号13900001234。'}
    env.service.knowledge = Mock()
    env.service.knowledge.retrieve.return_value = {'status': 'retrieved', 'items': [public]}
    env.service.knowledge_model = Mock()
    env.service.knowledge_model.select.return_value = {'selections': [
        {'reference': 'E1', 'quote': public['content'], 'evidence_ids': []}]}
    personal = {**public, 'source': 'personal', 'owner_id': 'default_user', 'document_id': 'strategy',
                'content': '只有完整条件满足才考虑执行。'}
    env.strategies.retrieve.return_value = {'status': 'retrieved', 'items': [personal]}
    env.model.select.return_value = [personal]
    result = env.service.generate(DAY)['result']
    assert result['knowledge_status'] == 'retrieved'
    assert result['knowledge'][0]['document_id'] == 'book'
    assert result['strategies'][0]['document_id'] == 'strategy'
    assert '公共知识' in result['answer'] and '分散投资不能消除所有风险' in result['answer']
    assert '[chunk_id=13812345678]' in result['answer'] and '13900001234' not in str(result)
    assert result['answer'].count(DISCLAIMER) == 1


@pytest.mark.parametrize('failure', ['retrieval', 'model', 'foreign', 'forged_reference'])
def test_public_knowledge_failure_does_not_invent_citations_or_hide_market(env, failure):
    public = {'source': 'knowledge', 'owner_id': 'public', 'chunk_id': 'book-id',
              'collection': 'public', 'document_id': 'book', 'title': '书籍', 'content': '分散投资。'}
    env.service.knowledge = Mock()
    env.service.knowledge.retrieve.return_value = {'status': 'retrieved', 'items': [public]}
    env.service.knowledge_model = Mock()
    env.service.knowledge_model.select.return_value = {'selections': [{'reference': 'E99', 'quote': '伪造事实'}]}
    if failure == 'retrieval':
        env.service.knowledge.retrieve.side_effect = RuntimeError('private-token')
    elif failure == 'model':
        env.service.knowledge_model.select.side_effect = RuntimeError('private-token')
    elif failure == 'foreign':
        public['owner_id'] = 'another_owner'
    record = env.service.generate(DAY)
    assert record['status'] == 'completed' and record['result']['knowledge_status'] == 'failed'
    assert not record['result']['knowledge'] and '上证指数' in record['result']['answer']
    assert all(value not in str(record) for value in ('private-token', 'book-id', '伪造事实'))


def test_summary_snapshots_profile_and_mode_without_modifying_risk(env):
    from uuid import uuid4
    mode_id, document_id = str(uuid4()), str(uuid4())
    env.service.settings = lambda: {'revision': 2, 'profile': {'risk_preference': 'adventurous'},
        'modes': [{'id': mode_id, 'name': '测试模式', 'document_ids': [document_id]}], 'active_mode_id': mode_id}
    record = env.service.generate(DAY)
    assert record['status'] == 'completed'
    assert env.strategies.retrieve.call_args.kwargs['document_ids'] == [document_id]
    assert record['result']['personalization']['revision'] == 2
    assert '积极' in record['result']['answer']
    first_levels = [item['level'] for item in record['result']['indices']]
    env.service.settings = lambda: {'profile': {'risk_preference': 'cautious'}}
    changed = env.service.generate(DAY, retry=True)['result']
    assert [item['level'] for item in changed['indices']] == first_levels
    assert record['result']['personalization']['mode_name'] == '测试模式'


def test_summary_personal_selector_receives_changed_preference_with_same_candidates(env):
    personal = {'source': 'personal', 'owner_id': 'default_user', 'document_id': 'strategy',
                'chunk_id': 'personal-source', 'title': '条件策略', 'content': '如果满足前提才执行。'}
    env.strategies.retrieve.return_value = {'status': 'retrieved', 'items': [personal]}
    env.model.select.return_value = [personal]
    env.service.settings = lambda: {'profile': {'risk_preference': 'cautious'}}
    original = env.service.generate(DAY)['result']
    first = env.model.select.call_args
    env.service.settings = lambda: {'profile': {'risk_preference': 'adventurous'}}
    changed = env.service.generate(DAY, retry=True)['result']
    second = env.model.select.call_args
    assert first.args == second.args
    assert first.kwargs['profile_context']['profile']['risk_preference'] == 'cautious'
    assert second.kwargs['profile_context']['profile']['risk_preference'] == 'adventurous'
    assert original['indices'] == changed['indices']


def test_summary_settings_failure_preserves_last_success(env):
    result = env.service.generate(DAY)['result']
    env.service.settings = Mock(side_effect=RuntimeError('private-token'))
    record = env.service.generate(DAY, retry=True)
    assert record['status'] == 'failed' and record['result'] == result
    assert '档案读取失败' in record['error'] and 'private-token' not in str(record)


@pytest.mark.parametrize('day,hour', [('2026-11-27', 14), ('2026-11-28', 16), ('2026-11-29', 16), ('2026-11-30', 16), ('2025-01-01', 16)])
def test_reject_unclosed_weekend_future_old_dates(env, day, hour):
    env.service.now = lambda: NOW.replace(hour=hour)
    assert env.client.post('/daily-summaries/generate', json={'date': day}).status_code == 422
    assert env.db.summaries.count_documents({}) == 0


@pytest.mark.parametrize('mode', ['holiday', 'delay', 'missing', 'wrong_identity'])
def test_missing_or_stale_indices_never_publish(env, mode):
    def fetch(symbol, *args):
        rows = index_rows(symbol)
        if mode == 'missing': return [], []
        if mode in {'holiday', 'delay'}: return rows[:-1], []
        rows[-1]['name'] = '平安银行'
        return rows, []
    env.indices.fetch.side_effect = fetch
    record = env.service.generate(DAY)
    assert record['status'] == 'failed' and 'result' not in record
    env.strategies.retrieve.assert_not_called()


def test_failed_retry_keeps_success_then_recovers(env):
    original = env.service.generate(DAY)['result']
    env.strategies.retrieve.side_effect = RuntimeError('private-url-secret')
    failed = env.service.generate(DAY, retry=True)
    assert failed['status'] == 'failed' and failed['result'] == original
    assert '策略检索失败' in failed['error'] and 'private' not in str(failed)
    assert env.service.latest()['latest_success']['result'] == original
    env.strategies.retrieve.side_effect = None
    recovered = env.service.generate(DAY)
    assert recovered['status'] == 'completed' and recovered['attempts'] == 3


def test_model_failure_and_insufficient_are_distinct(env):
    env.strategies.retrieve.return_value = {'status': 'retrieved', 'items': [{'content': '测试'}]}
    env.model.select.side_effect = RuntimeError('private-token')
    record = env.service.generate(DAY)
    assert record['status'] == 'failed' and '模型' in record['error']
    assert 'private' not in str(record)
    env.strategies.retrieve.return_value = {'status': 'insufficient', 'items': []}
    assert env.service.generate(DAY)['result']['strategy_status'] == 'insufficient'


def test_strategy_checklist_citations_redaction_write_and_read(env):
    item = {'source': 'personal', 'owner_id': 'default_user', 'document_id': 'test-13812345678-doc',
            'chunk_id': '13812345678', 'title': '姓名：王小明。',
            'content': '如果处于熊市才轻仓。手机号13812345678，持仓金额523456元。'}
    env.strategies.retrieve.return_value = {'status': 'retrieved', 'items': [item]}
    env.model.select.return_value = [item]
    record = env.service.generate(DAY)
    for result in (record['result'], env.db.summaries.find_one()['result']):
        assert '王小明' not in result['answer'] and '523456' not in result['answer']
        assert '[chunk_id=13812345678]' in result['answer']
        assert '手机号138****5678' in result['answer']
        assert '前提尚未确认满足' in result['answer']
        assert '如果处于熊市才轻仓' in result['answer']
        assert result['strategies'][0]['document_id'] == item['document_id']
        assert result['answer'].count(DISCLAIMER) == 1
    env.db.summaries.update_one({}, {'$set': {'result.answer': '手机号13812345678 ' + DISCLAIMER * 2}})
    response = env.client.get('/daily-summaries/latest').json()['latest_success']['result']['answer']
    assert '13812345678' not in response and response.count(DISCLAIMER) == 1


def test_claim_concurrency_lease_recovery_and_stale_worker_cannot_publish(env):
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: env.repo.claim('default_user', DAY, NOW), range(12)))
    winner = next(claim for claim in claims if claim)
    assert sum(claim is not None for claim in claims) == 1
    expired = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    assert env.repo.get('default_user', DAY, expired)['status'] == 'failed'
    new_claim = env.repo.claim('default_user', DAY, expired)
    assert not env.repo.finish(winner, expired, result={'answer': 'old'})
    assert env.repo.finish(new_claim, expired, result={'answer': 'new'})
    assert env.repo.get('default_user', DAY, expired)['result']['answer'].startswith('new')
    assert env.db.summaries.count_documents({}) == 1


def test_processing_dedup_and_owner_isolation(env):
    first, record = env.service.begin(DAY)
    second, record = env.service.begin(DAY)
    assert first and second is None and record['status'] == 'processing'
    env.service.run(first)
    assert env.repo.recent('other_user', NOW) == []
    assert env.repo.latest_success('other_user', NOW) is None
    assert env.repo.get('other_user', DAY, NOW) is None


def test_watchlist_concurrent_limit_dedup_owner_and_delete(env):
    def add(n):
        try: env.watch.add('default_user', Security(f'{600000+n:06}', f'测试{n}'))
        except ValueError: pass
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(30)))
    assert len(env.watch.read('default_user')) == 20
    first = env.watch.read('default_user')[0]
    env.watch.add('default_user', Security(**first))
    assert len(env.watch.read('default_user')) == 20
    assert env.watch.read('other_user') == []
    env.watch.remove('other_user', first['code'])
    assert len(env.watch.read('default_user')) == 20
    assert len(env.watch.remove('default_user', first['code'])) == 19


def test_watch_api_invalid_input_does_not_echo(env):
    assert env.client.post('/watchlist', json={'query': '000001'}).status_code == 200
    assert len(env.client.post('/watchlist', json={'query': '000001'}).json()['items']) == 1
    for path, payload in [('/watchlist', {'query': '13812345678', 'owner_id': 'other'}),
                          ('/daily-summaries/generate', {'date': 'secret-url'})]:
        response = env.client.post(path, json=payload)
        assert response.status_code == 422 and '13812345678' not in response.text and 'secret' not in response.text
    assert env.client.delete('/watchlist/000001').json() == {'items': []}


@pytest.mark.parametrize('query', ['贵州茅台000001风险', '上证指数000001', 'sh000001', 'sz600519'])
def test_watch_identity_conflicts_use_verified_resolver(env, query):
    resolver = SymbolResolver()
    resolver.lookup = lambda term: [Security('000001', '平安银行')] if term == '000001' else [Security('600519', '贵州茅台')]
    env.service.resolver = resolver
    assert env.client.post('/watchlist', json={'query': query}).status_code == 422
    assert env.watch.read('default_user') == []


def test_watch_stock_reuses_risk_evaluator(env):
    env.watch.add('default_user', Security('000001', '平安银行'))
    rows = [{'code': '000001', 'name': '平安银行', 'date': (DAY-timedelta(days=n)).isoformat(),
             'close': 10, 'change_pct': 10 if n == 0 else 1, 'volume': 100, 'turnover': 1,
             'pe': 20, 'source': 'eastmoney'} for n in range(25)][::-1]
    env.service.market.refresh.return_value = {'quotes': rows, 'source': 'eastmoney'}
    record = env.service.generate(DAY)
    assert '个股风险：高风险' in record['result']['stocks'][0]['assessment']
    assert '20 日波动率' in record['result']['answer']


def test_personal_retriever_owner_type_completed_and_failure(env):
    documents = env.db.documents
    vectors = Mock()
    embed = Mock(return_value={'dense': [[1., 0.]], 'sparse': [{1: 1.}]})
    retriever = PersonalStrategyRetriever(documents, vectors, embed)
    assert retriever.retrieve('default_user', '风险')['status'] == 'empty'
    embed.assert_not_called()
    documents.insert_one({'_id': 'd1', 'owner_id': 'default_user', 'doc_type': 'personal', 'status': 'completed'})
    entity = {'document_id': 'd1', 'owner_id': 'default_user', 'doc_type': 'personal', 'content': '勿追高'}
    vectors.search.return_value = [[{'id': 123, 'entity': entity}]]
    result = retriever.retrieve('default_user', '风险')
    assert result['items'][0]['chunk_id'] == '123'
    vectors.search.assert_called_once_with([1., 0.], {1: 1.}, 'default_user', limit=5)
    for key, value in [('owner_id', 'other'), ('doc_type', 'knowledge')]:
        vectors.search.return_value = [[{'id': 123, 'entity': {**entity, key: value}}]]
        with pytest.raises(ValueError): retriever.retrieve('default_user', '风险')
    vectors.search.return_value = [[{'id': 123, 'entity': {**entity, 'document_id': 'incomplete'}}]]
    assert retriever.retrieve('default_user', '风险')['status'] == 'insufficient'


def test_model_cannot_invent_citations_or_rewrite_and_full_conditions_retained():
    items = [{'chunk_id': '1', 'content': '如果大幅回撤才减仓。'}]
    evidence = [{'id': 'index:sh000001', 'text': '上证指数高风险'}]
    selected = validate_selections({'checks': [{'chunk_id': '1', 'quote': '减仓', 'evidence_ids': ['index:sh000001']}]}, items, evidence)
    assert selected == [{**items[0], 'evidence': evidence}]
    for selection in [{'chunk_id': 'fake', 'quote': '减仓'}, {'chunk_id': '1', 'quote': '立即全仓买入'}]:
        with pytest.raises(ValueError):
            validate_selections({'checks': [{**selection, 'evidence_ids': ['index:sh000001']}]}, items, evidence)
    with pytest.raises(ValueError):
        validate_selections({'checks': [{'chunk_id': '1', 'quote': '减仓', 'evidence_ids': ['unknown']}]}, items, evidence)


@pytest.mark.parametrize('count', [1, 20, 21, 25])
def test_index_history_dedup_and_no_data_not_low(count):
    rows = index_rows('sh000001', count)
    assessment = evaluate_index(rows + rows, 'sh000001', DAY)
    assert assessment['sample_count'] == count
    assert assessment['level'] == ('低' if count >= N+1 else None)
    assert assessment['limited'] == (count < N+1)


def test_index_thresholds_zero_volume_conflicts_and_invalid_identity():
    rows = index_rows('sh000001')
    rows[-1]['volume'] = 2000
    assert evaluate_index(rows, 'sh000001', DAY)['level'] == '高'
    rows[-1]['volume'] = 0
    assert evaluate_index(rows, 'sh000001', DAY)['level'] == '高'
    with pytest.raises(MarketUnavailable):
        evaluate_index(rows + [{**rows[-1], 'close': 3100}], 'sh000001', DAY)
    with pytest.raises(MarketUnavailable):
        evaluate_index(rows, 'sz399001', DAY)


@pytest.mark.parametrize('symbol', list(INDICES))
def test_captured_real_index_units_and_identity(symbol):
    http = Mock()
    http.get.return_value = (FIXTURES / f'{symbol}-kline.json').read_bytes()
    rows = EastmoneyIndexAdapter(http).fetch_daily(symbol, date(2026, 7, 1), DAY, NOW.replace(hour=23))
    http.get.return_value = (FIXTURES / f'{symbol}-tencent.txt').read_bytes()
    snapshot = TencentIndexAdapter(http).fetch_daily(symbol, DAY, DAY, NOW.replace(hour=23))[0]
    assert rows[-1].date == snapshot.date == DAY
    assert rows[-1].name == snapshot.name == INDICES[symbol][1]
    for key in ('close', 'volume', 'change_pct', 'amount'):
        assert getattr(rows[-1], key) == pytest.approx(getattr(snapshot, key))
    assert len(rows) > N and snapshot.close > 1000


def test_index_fallback_does_not_invent_history(env):
    primary, fallback = Mock(), TencentIndexAdapter(Mock())
    primary.fetch_daily.side_effect = MarketUnavailable('private')
    fallback.http.get.return_value = (FIXTURES / 'sh000001-tencent.txt').read_bytes()
    service = IndexService(IndexRepository(env.db.indices), [primary, fallback])
    rows, warnings = service.fetch('sh000001', DAY-timedelta(days=120), DAY, NOW.replace(hour=23))
    assert len(rows) == 1 and warnings
    assert evaluate_index(rows, 'sh000001', DAY)['level'] is None
    with pytest.raises(MarketUnavailable):
        service.fetch('sh000001', date(2026, 10, 1), date(2026, 10, 1), NOW.replace(hour=23))


def test_lifespan_disabled_and_enabled_timezone_shutdown():
    factory = Mock()
    async def check():
        app = FastAPI()
        async with summary_lifespan(False, factory)(app):
            assert app.state.summary_scheduler is None
        async with summary_lifespan(True, factory)(app):
            scheduler = app.state.summary_scheduler
            assert scheduler.running
            job = scheduler.get_job('daily-summary')
            assert job.max_instances == 1 and job.coalesce is True
            assert str(scheduler.timezone) == 'Asia/Shanghai'
            assert str(job.trigger.timezone) == 'Asia/Shanghai'
            next_run = job.trigger.get_next_fire_time(None, datetime(2026, 11, 27, 15, tzinfo=SHANGHAI))
            assert (next_run.hour, next_run.minute) == (15, 30)
        assert not scheduler.running
    asyncio.run(check())
    factory.assert_not_called()
    with pytest.raises(ValueError): build_scheduler(factory, 14, 30)


def test_actual_scheduler_job_uses_same_generation_boundary(env):
    scheduler = build_scheduler(lambda: env.service)
    scheduler.get_job('daily-summary').func()
    assert env.repo.get('default_user', DAY, NOW)['status'] == 'completed'
    scheduler.get_job('daily-summary').func()
    assert env.db.summaries.count_documents({}) == 1


def test_storage_failure_is_generic_api_error(env, monkeypatch):
    def broken(*args): raise RuntimeError('private-url-token')
    monkeypatch.setattr(env.repo, 'recent', broken)
    response = env.client.get('/daily-summaries')
    assert response.status_code == 503 and 'private' not in response.text


def test_expired_worker_looks_failed_even_without_restart(env):
    claim, _ = env.service.begin(DAY)
    env.service.now = lambda: NOW + timedelta(seconds=LEASE_SECONDS+1)
    record = env.client.get('/daily-summaries/' + str(DAY)).json()
    assert record['status'] == 'failed' and '超时' in record['error']
    env.service.run(claim)
    assert env.client.get('/daily-summaries/' + str(DAY)).json()['status'] == 'failed'
    assert env.service.generate(DAY)['status'] == 'completed'


def test_old_success_not_relabelled_when_new_day_fails(env):
    original = env.service.generate(DAY)
    env.service.now = lambda: NOW + timedelta(days=3)
    env.indices.fetch.side_effect = MarketUnavailable('secret')
    assert env.service.generate(DAY+timedelta(days=3))['status'] == 'failed'
    latest = env.client.get('/daily-summaries/latest').json()
    assert latest['latest_success']['date'] == original['date']
    assert latest['current']['status'] == 'failed'


def test_index_source_identity_mismatch_and_snapshot_preclose(env):
    import json
    http = Mock()
    payload = json.loads((FIXTURES / 'sh000001-kline.json').read_text(encoding='utf-8'))
    payload['data']['name'] = '平安银行'
    http.get.return_value = json.dumps(payload).encode()
    with pytest.raises(MarketUnavailable):
        EastmoneyIndexAdapter(http).fetch_daily('sh000001', DAY, DAY, NOW)
    http.get.return_value = (FIXTURES / 'sh000001-tencent.txt').read_bytes().replace(b'20261127161427', b'20261127141427')
    with pytest.raises(MarketUnavailable):
        TencentIndexAdapter(http).fetch_daily('sh000001', DAY, DAY, NOW)


def test_lifespan_registered_on_actual_query_app(monkeypatch):
    from knowledge.api.query_api import create_app
    from knowledge.api import summary_api
    factory = Mock(side_effect=AssertionError('不应自动生成'))
    monkeypatch.setattr(summary_api, 'get_summary_service', factory)
    monkeypatch.setenv('DAILY_SUMMARY_ENABLED', '0')
    app = create_app()
    with TestClient(app):
        assert app.state.summary_scheduler is None
    factory.assert_not_called()


def test_large_strategy_not_truncated_into_unconditional_advice(env):
    docs, vectors = env.db.documents, Mock()
    docs.insert_one({'_id': 'long', 'owner_id': 'default_user', 'doc_type': 'personal', 'status': 'completed'})
    vectors.search.return_value = [[{'id': 9, 'entity': {'owner_id': 'default_user', 'doc_type': 'personal',
                                    'document_id': 'long', 'content': '测试' * 1600}}]]
    retriever = PersonalStrategyRetriever(docs, vectors, lambda _: {'dense': [[1., 0.]], 'sparse': [{1: 1.}]})
    assert retriever.retrieve('default_user', '风险')['status'] == 'insufficient'


@pytest.mark.parametrize('pct,level', [(0, '低'), (5, '中'), (10, '高'), (-10, '高')])
def test_index_compounded_return_thresholds(pct, level):
    rows = index_rows('sh000001')
    for row in rows: row['change_pct'] = 0
    rows[-1]['change_pct'] = pct
    result = evaluate_index(rows, 'sh000001', DAY)
    assert result['return_pct'] == pytest.approx(pct)
    assert result['level'] == level


def test_index_bad_values_never_rate_low():
    rows = index_rows('sh000001')
    for row in rows: row['volume'] = 0
    result = evaluate_index(rows, 'sh000001', DAY)
    assert result['level'] is None and result['limited']
    rows[-1]['change_pct'] = float('nan')
    with pytest.raises(ValueError): evaluate_index(rows, 'sh000001', DAY)


def test_index_adapter_conflicting_batch_not_persisted(env):
    from knowledge.summary.indices import IndexQuote
    adapter = Mock()
    rows = index_rows('sh000001', 1)
    quote = IndexQuote(**rows[0], raw={})
    adapter.fetch_daily.return_value = [quote, quote.model_copy(update={'close': 3500})]
    service = IndexService(IndexRepository(env.db.indices), [adapter])
    with pytest.raises(MarketUnavailable): service.fetch('sh000001', DAY, DAY, NOW)
    assert env.db.indices.count_documents({}) == 0


def test_unexpected_value_error_is_not_echoed(env):
    env.service.resolver.resolve.side_effect = ValueError('private-connection-token')
    response = env.client.post('/watchlist', json={'query': '000001'})
    assert response.status_code == 503 and 'private' not in response.text


def test_actual_market_changes_retrieval_model_context_and_checklist(env):
    strategy = {'source': 'personal', 'owner_id': 'default_user', 'document_id': 'strategy-test',
                'chunk_id': 'strategy-1', 'title': '控制风险', 'content': '若风险偏高，先核对仓位上限。'}
    env.strategies.retrieve.return_value = {'status': 'retrieved', 'items': [strategy]}
    env.model.select.side_effect = lambda items, evidence: validate_selections({
        'checks': [{'chunk_id': 'strategy-1', 'quote': strategy['content'],
                    'evidence_ids': ['index:sh000001']}],
    }, items, evidence)
    low = env.service.generate(DAY)
    low_query = env.strategies.retrieve.call_args.args[1]
    low_context = env.model.select.call_args.args[1]
    def high_rows(symbol, *args):
        rows = index_rows(symbol)
        rows[-1]['change_pct'] = 12
        return rows, []
    env.indices.fetch.side_effect = high_rows
    high = env.service.generate(DAY, retry=True)
    high_context = env.model.select.call_args.args[1]
    assert low['status'] == high['status'] == 'completed'
    assert low_query != env.strategies.retrieve.call_args.args[1]
    assert '低风险' in low_context[0]['text'] and '高风险' in high_context[0]['text']
    assert high['result']['strategies'][0]['evidence'] == [high_context[0]]
    assert '本条策略的行情复核依据' in high['result']['answer']
    assert '不表示完整前提已满足' in high['result']['answer']


def test_stock_evidence_preserves_insufficient_data_and_source_dates(env):
    from knowledge.service.summary_service import market_evidence
    assessments = [evaluate_index(index_rows('sh000001'), 'sh000001', DAY)]
    stock = {'code': '000001', 'assessment': '实际行情日期：2026-11-26；个股风险：无法完整评估；更新失败后的旧缓存。'}
    facts = market_evidence(assessments, [stock])
    assert facts[-1] == {'id': 'stock:000001', 'text': stock['assessment']}

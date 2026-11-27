"""#6 隔离验收入口：默认真实存储探针，--host 使用主机已有 BGE，--serve 浏览器。"""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import threading
import uuid
from datetime import date, datetime, timedelta, time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', action='store_true', help='真实策略导入图/BGE召回/API/LLM，不下载模型')
    parser.add_argument('--serve', action='store_true', help='主机浏览器入口，正常停止后清理')
    parser.add_argument('--port', type=int, default=8002)
    args = parser.parse_args()
    if args.serve and not args.host:
        parser.error('--serve 需要 --host')
    if args.host:
        from dotenv import load_dotenv
        load_dotenv(ROOT / 'knowledge/.env')
    os.environ.update(DAILY_SUMMARY_ENABLED='0', HF_HUB_OFFLINE='1')
    db_name = os.getenv('AFK_TEST_DB_NAME')
    if not db_name or db_name in {os.getenv('MONGO_DB_NAME'), 'kb001', 'admin', 'local', 'config'}:
        print('必须提供独立 AFK_TEST_DB_NAME。')
        return 1
    prefix = 'summary_accept_' + uuid.uuid4().hex[:16]
    vector_collection = prefix + '_vectors'
    report = {'mode': 'host' if args.host else 'storage', 'checks': {}, 'cleanup': {}, 'passed': False}
    mongo = milvus = None
    resources = []
    vector_created = False
    restore_history = None

    def check(name, result):
        report['checks'][name] = bool(result)
        print(name, 'PASS' if result else 'FAIL', flush=True)
        if not result:
            raise AssertionError(name)

    try:
        from pymongo import MongoClient
        from pymilvus import MilvusClient
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from knowledge.summary.repository import SummaryRepository, WatchlistRepository
        from knowledge.summary.indices import IndexService, IndexRepository, EastmoneyIndexAdapter
        from knowledge.summary.strategies import PersonalStrategyRetriever
        from knowledge.summary.scheduler import summary_lifespan, build_scheduler
        from knowledge.service.summary_service import SummaryService
        from knowledge.service.market_service import MarketService
        from knowledge.market.repository import MarketQuoteRepository
        from knowledge.market.models import SHANGHAI, date_window
        from knowledge.strategy.vectors import StrategyVectors
        from knowledge.strategy.repository import StrategyRepository
        from knowledge.api.summary_api import router, get_summary_service
        from knowledge.utils.answer_policy import DISCLAIMER
        mongo = MongoClient(os.environ['MONGO_URL'], serverSelectionTimeoutMS=5000, socketTimeoutMS=5000, tz_aware=True)
        db = mongo[db_name]
        def collection(suffix):
            value = db[prefix + '_' + suffix]
            resources.append(value)
            return value
        documents = collection('documents')
        summaries, watches = collection('summaries'), collection('watches')
        index_quotes, stock_quotes = collection('indices'), collection('stocks')
        milvus = MilvusClient(uri=os.environ['MILVUS_URL'], timeout=15)
        check('unique_vector_collection', not milvus.has_collection(vector_collection))
        vectors = StrategyVectors(milvus, vector_collection, 1024 if args.host else 2)
        vector_created = True
        vectors.prepare()
        doc_repo = StrategyRepository(documents)
        content = ('# 自建摘要验收策略\n\n若单只股票风险偏高，先检查自己设定的仓位上限，避免连续追高。'
                   '若处于熊市才轻仓；当前市场阶段未知时不能认为此前提已满足。'
                   '仅在人工核实策略前提后决定是否采用。验收短语：青鹭7391。')
        if args.host:
            from knowledge.service.strategy_service import StrategyService
            from knowledge.api.strategy_api import router as strategy_router, get_strategy_service
            strategy = StrategyService(doc_repo, vectors)
            importer = FastAPI()
            importer.include_router(strategy_router)
            importer.dependency_overrides[get_strategy_service] = lambda: strategy
            with TestClient(importer) as api:
                response = api.post('/strategies/text', json={'title': '摘要验收策略', 'text': content})
                check('real_import_api', response.status_code == 202)
                document_id = response.json()['task_id']
                check('real_import_graph_bge', api.get('/strategies/' + document_id).json()['status'] == 'completed')
            # 同样使用真实图导入另一 owner 文档，验证召回不混入。
            other = StrategyService(doc_repo, vectors, owner_id='other_user')
            state = other.stage('别人的策略：禁止出现紫鹭8642。'.encode(), 'other.md')
            other.run(state)
            check('other_owner_import', other.get(state['task_id'])['status'] == 'completed')
            retriever = PersonalStrategyRetriever(documents, vectors)
        else:
            # 只验证 Milvus/隔离/流程，不将二维人工向量称为真实 BGE。
            document_id = 'synthetic-daily-strategy'
            chunks = [{'text': content, 'title': '自建摘要验收策略', 'file_title': 'test',
                       'dense_vector': [1., 0.], 'sparse_vector': {1: 1.}}]
            for owner, doc in [('default_user', document_id), ('other_user', 'other-doc')]:
                doc_repo.create(doc, owner, '测试策略', 'test.md')
                vectors.insert(chunks, owner, doc)
                doc_repo.finish(doc, owner, 'completed', chunk_count=1)
            retriever = PersonalStrategyRetriever(documents, vectors, lambda _: {'dense': [[1., 0.]], 'sparse': [{1: 1.}]})
        milvus.flush(vector_collection, timeout=30)
        retrieved = retriever.retrieve('default_user', '收盘后如何控制风险和遵守策略条件？')
        check('personal_recall', retrieved['status'] == 'retrieved' and any('青鹭7391' in row['content'] for row in retrieved['items']))
        check('owner_and_citation', all(row['owner_id'] == 'default_user' and row['document_id'] == document_id and row['chunk_id']
                                        and '紫鹭8642' not in row['content'] for row in retrieved['items']))
        check('unknown_owner_empty', retriever.retrieve('missing_user', '风险')['status'] == 'empty')
        market = MarketService(MarketQuoteRepository(stock_quotes))
        indices = IndexService(IndexRepository(index_quotes))
        service = SummaryService(SummaryRepository(summaries), WatchlistRepository(watches), indices, market, retriever)
        service.add_watch('000001')
        now = service.now()
        _, end = date_window(None, None, now)
        rows = EastmoneyIndexAdapter().fetch_daily('sh000001', end-timedelta(days=120), end, now)
        day = rows[-1].date
        if args.host:
            from knowledge.api.query_api import create_app
            from knowledge.api.market_api import get_market_service
            from knowledge.api.review_api import get_review_service
            from knowledge.review.repository import ReviewRepository
            from knowledge.service.review_service import ReviewService
            from knowledge.core.deps import get_query_service
            from knowledge.service.query_service import QueryService
            from knowledge.service.risk_service import RiskService
            from knowledge.utils import mongo_history_util
            from knowledge.api.strategy_api import router as strategy_router, get_strategy_service
            app = create_app(summary_enabled=False)
            app.include_router(strategy_router)
            app.dependency_overrides[get_strategy_service] = lambda: strategy
            history = collection('history')
            restore_history = mongo_history_util._get_collection
            mongo_history_util._get_collection = lambda: history
            app.dependency_overrides[get_market_service] = lambda: market
            app.dependency_overrides[get_query_service] = lambda: QueryService(risk=RiskService(lambda: market))
            reviews = ReviewService(ReviewRepository(collection('reviews')))
            app.dependency_overrides[get_review_service] = lambda: reviews
        else:
            app = FastAPI(lifespan=summary_lifespan(False))
            app.include_router(router)
        app.dependency_overrides[get_summary_service] = lambda: service
        with TestClient(app) as api:
            check('manual_generate_accepted', api.post('/daily-summaries/generate', json={'date': str(day)}).status_code == 202)
            record = api.get('/daily-summaries/' + str(day)).json()
            check('summary_completed', record['status'] == 'completed')
            answer = record['result']['answer']
            check('real_strategy_in_summary', bool(record['result']['strategies']) and '青鹭7391' in answer and '紫鹭8642' not in answer)
            check('citation_and_conditions', '[chunk_id=' in answer and '前提尚未确认满足' in answer and '若处于熊市才轻仓' in answer)
            check('risk_and_disclaimer', '个股风险：' in answer and 'sh000001' in answer and 'sz399001' in answer
                  and answer.endswith(DISCLAIMER) and answer.count(DISCLAIMER) == 1)
            check('storage_read_consistency', summaries.find_one()['result']['answer'] == answer and
                  api.get('/daily-summaries/latest').json()['latest_success']['result']['answer'] == answer)
            check('history_and_idempotency', len(api.get('/daily-summaries').json()['items']) == 1 and
                  not api.post('/daily-summaries/generate', json={'date': str(day)}).json()['started'])
        # 实际 APScheduler executor 触发同一入口，测试关闭自动 cron；不等待收盘时间。
        # 仅改本次 UUID 测试记录，模拟失败重试，验证调度真正生成而非只命中缓存。
        summaries.update_one({'owner_id': service.owner, 'date': str(day)}, {'$set': {'status': 'failed', 'error': '验收重试'}})
        old_now = service.now
        service.now = lambda: datetime.combine(day, time(23), SHANGHAI)
        scheduler = build_scheduler(lambda: service)
        event = threading.Event()
        from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
        scheduler.add_listener(lambda result: event.set() if result.job_id == 'accept-once' else None,
                               EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        scheduler.add_job(scheduler.get_job('daily-summary').func, 'date', id='accept-once')
        scheduler.start()
        try:
            check('actual_scheduler_executor', event.wait(55) and summaries.count_documents({}) == 1 and
                  summaries.find_one()['status'] == 'completed' and summaries.find_one()['attempts'] == 2)
        finally:
            scheduler.shutdown(wait=True)
            service.now = old_now
        if args.serve:
            import uvicorn
            print(f'浏览器入口：http://127.0.0.1:{args.port}/chat.html；仅测试资源，自动调度关闭，正常停止后清理。', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')
        report['passed'] = True
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        print('验收失败，异常详情已隐藏；类型：' + type(exc).__name__, flush=True)
    finally:
        if restore_history:
            from knowledge.utils import mongo_history_util
            mongo_history_util._get_collection = restore_history
        if milvus:
            try:
                if vector_created and milvus.has_collection(vector_collection):
                    milvus.drop_collection(vector_collection, timeout=30)
                report['cleanup']['milvus'] = not milvus.has_collection(vector_collection)
            except Exception:
                report['cleanup']['milvus'] = False
            milvus.close()
        if mongo:
            try:
                for value in resources:
                    value.drop()
                report['cleanup']['mongodb'] = not any(name.startswith(prefix) for name in mongo[db_name].list_collection_names())
            except Exception:
                report['cleanup']['mongodb'] = False
            mongo.close()
        report['passed'] = report['passed'] and all(report['cleanup'].values())
        folder = ROOT / '.sandcastle/logs'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (prefix + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('结果：' + ('PASS' if report['passed'] else 'FAIL') + '；资源清理：' + str(report['cleanup']), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    raise SystemExit(main())

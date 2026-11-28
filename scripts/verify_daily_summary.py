"""#6 隔离验收入口：默认真实存储探针，--host 使用主机已有 BGE，--serve 浏览器。"""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import threading
import uuid
from datetime import datetime, timedelta, time
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace

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
    os.environ['HF_HUB_OFFLINE'] = '1'
    prefix = 'summary_accept_' + uuid.uuid4().hex[:16]
    report = {'mode': 'host' if args.host else 'storage', 'checks': {}, 'cleanup': {}, 'passed': False}
    runtime = None

    def check(name, result):
        report['checks'][name] = bool(result)
        print(name, 'PASS' if result else 'FAIL', flush=True)
        if not result:
            raise AssertionError(name)

    try:
        from fastapi.testclient import TestClient
        from knowledge.api.query_api import create_app
        from knowledge.runtime import QueryRuntime, RuntimeConfig
        from knowledge.summary.indices import EastmoneyIndexAdapter
        from knowledge.summary.scheduler import build_scheduler
        from knowledge.summary.strategies import model_error_code
        from knowledge.market.models import SHANGHAI, date_window
        from knowledge.market.calendar import TradingCalendar
        from knowledge.utils.answer_policy import DISCLAIMER
        config = RuntimeConfig.acceptance('summary', prefix, vector_dimension=1024 if args.host else 2)
        if not args.host:
            config = replace(config, features=frozenset({'summary'}))
        runtime = QueryRuntime(config)
        summaries = runtime.collection('summaries')
        milvus = runtime.milvus
        vector_collection = config.vector_names['personal']
        check('unique_vector_collection', not milvus.has_collection(vector_collection))
        vectors = runtime.vectors('personal')
        vectors.prepare()
        strategy = runtime.strategy
        doc_repo = strategy.repository
        service = runtime.summary
        # 此入口只验收个人策略摘要；公共知识和档案由投资助手入口另行验收。
        service.knowledge = service.settings = None
        retriever = service.strategies
        app = create_app(summary_enabled=False, runtime=runtime)
        content = ('# 仅在熊市时适用\n## 自建摘要验收策略\n\n若单只股票风险偏高，先检查自己设定的仓位上限，避免连续追高。'
                   '若处于熊市才轻仓；当前市场阶段未知时不能认为此前提已满足。'
                   '仅在人工核实策略前提后决定是否采用。验收短语：青鹭7391。')
        if args.host:
            from knowledge.service.strategy_service import StrategyService
            with TestClient(app) as api:
                response = api.post('/strategies/text', json={'title': '摘要验收策略', 'text': content})
                check('real_import_api', response.status_code == 202)
                document_id = response.json()['task_id']
                check('real_import_graph_bge', api.get('/strategies/' + document_id).json()['status'] == 'completed')
            other = StrategyService(doc_repo, vectors, owner_id='other_user', register_task=runtime.register_task)
            state = other.stage('别人的策略：禁止出现紫鹭8642。'.encode(), 'other.md')
            other.run(state)
            check('other_owner_import', other.get(state['task_id'])['status'] == 'completed')
        else:
            document_id = 'synthetic-daily-strategy'
            chunks = [{'text': content, 'title': '自建摘要验收策略', 'file_title': 'test',
                       'dense_vector': [1., 0.], 'sparse_vector': {1: 1.}}]
            for owner, doc in [('default_user', document_id), ('other_user', 'other-doc')]:
                doc_repo.create(doc, owner, '测试策略', 'test.md')
                vectors.insert(chunks, owner, doc)
                doc_repo.finish(doc, owner, 'completed', chunk_count=1)
            retriever.embed = lambda _: {'dense': [[1., 0.]], 'sparse': [{1: 1.}]}
        milvus.flush(vector_collection, timeout=30)
        retrieved = retriever.retrieve('default_user', '收盘后如何控制风险和遵守策略条件？')
        check('personal_recall', retrieved['status'] == 'retrieved' and any('青鹭7391' in row['content'] for row in retrieved['items']))
        check('owner_and_citation', all(row['owner_id'] == 'default_user' and row['document_id'] == document_id and row['chunk_id']
                                        and '紫鹭8642' not in row['content'] for row in retrieved['items']))
        check('unknown_owner_empty', retriever.retrieve('missing_user', '风险')['status'] == 'empty')
        original_select = service.model.select
        def observed_select(items, evidence):
            started = monotonic()
            call = {'status': 'started'}
            report.setdefault('model_calls', []).append(call)
            try:
                result = original_select(items, evidence)
                call['status'] = 'completed'
                return result
            except Exception as exc:
                call.update(status='failed', error_type=type(exc).__name__, error_code=model_error_code(exc))
                raise
            finally:
                call['seconds'] = round(monotonic() - started, 2)
        service.model.select = observed_select
        service.add_watch('000001')
        now = service.now()
        _, end = date_window(None, None, now)
        rows = EastmoneyIndexAdapter().fetch_daily('sh000001', end-timedelta(days=120), end, now)
        day = rows[-1].date
        with TestClient(app) as api:
            check('manual_generate_accepted', api.post('/daily-summaries/generate', json={'date': str(day)}).status_code == 202)
            record = api.get('/daily-summaries/' + str(day)).json()
            report['manual'] = {'status': record.get('status'), 'attempts': record.get('attempts'),
                                'error': record.get('error'), 'error_code': record.get('error_code')}
            check('summary_completed', record['status'] == 'completed')
            answer = record['result']['answer']
            check('real_strategy_in_summary', bool(record['result']['strategies']) and '青鹭7391' in answer and '紫鹭8642' not in answer)
            check('citation_and_conditions', '[chunk_id=' in answer and '前提尚未确认满足' in answer
                  and '若处于熊市才轻仓' in answer and '仅在熊市时适用' in answer)
            check('strategy_market_evidence', all(item.get('evidence') for item in record['result']['strategies'])
                  and '本条策略的行情复核依据' in answer)
            check('risk_and_disclaimer', '个股风险：' in answer and 'sh000001' in answer and 'sz399001' in answer
                  and answer.endswith(DISCLAIMER) and answer.count(DISCLAIMER) == 1)
            check('storage_read_consistency', summaries.find_one()['result']['answer'] == answer and
                  api.get('/daily-summaries/latest').json()['latest_success']['result']['answer'] == answer)
            check('history_and_idempotency', len(api.get('/daily-summaries').json()['items']) == 1 and
                  not api.post('/daily-summaries/generate', json={'date': str(day)}).json()['started'])
        summaries.update_one({'owner_id': service.owner, 'date': str(day)}, {'$set': {'status': 'failed', 'error': '验收重试'}})
        old_now = service.now
        service.now = lambda: datetime.combine(day, time(23), SHANGHAI)
        calendar_status = TradingCalendar().status(day)
        report['scheduler_calendar'] = calendar_status
        check('scheduler_target_trading_day', calendar_status['is_trading_day'] is True)
        scheduler = build_scheduler(lambda: service, calendar_factory=lambda: SimpleNamespace(status=lambda: calendar_status))
        runtime.track_scheduler(scheduler)
        event = threading.Event()
        from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
        scheduler.add_listener(lambda result: event.set() if result.job_id == 'accept-once' else None,
                               EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        scheduler.add_job(scheduler.get_job('daily-summary').func, 'date', id='accept-once')
        scheduler.start()
        try:
            finished = event.wait(270)
            scheduled = summaries.find_one() or {}
            report['scheduler'] = {'event_received': finished, 'status': scheduled.get('status'),
                                   'attempts': scheduled.get('attempts'), 'error': scheduled.get('error'),
                                   'error_code': scheduled.get('error_code')}
            check('actual_scheduler_executor', finished and summaries.count_documents({}) == 1 and
                  scheduled.get('status') == 'completed' and scheduled.get('attempts') == 2)
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
        if runtime is not None:
            report['resources'] = runtime.manifest()
            report['cleanup'] = runtime.close()
        report['passed'] = report['passed'] and all(report['cleanup'].values())
        folder = ROOT / '.sandcastle/logs'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (prefix + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('结果：' + ('PASS' if report['passed'] else 'FAIL') + '；资源清理：' + str(report['cleanup']), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    raise SystemExit(main())

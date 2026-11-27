"""主机验收入口：实际聊天 API；--serve 提供隔离存储的浏览器验收服务。"""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true', help='在 127.0.0.1 启动浏览器验收服务')
    parser.add_argument('--port', type=int, default=8002)
    parser.add_argument('--rag-query', help='可选：针对主机已有公共知识库的问题，执行真实 BGE/RAG/LLM')
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / 'knowledge/.env')
    db = os.getenv('AFK_TEST_DB_NAME')
    if not db or db in {os.getenv('MONGO_DB_NAME', 'kb001'), 'kb001', 'admin', 'config', 'local'}:
        print('必须设置与业务库不同的 AFK_TEST_DB_NAME。')
        return 1
    query_prefix = 'risk_query_' + uuid.uuid4().hex
    os.environ.update(MONGO_DB_NAME=db, STRATEGY_COLLECTION=query_prefix + '_empty_personal',
                      REVIEW_COLLECTION=query_prefix + '_empty_review', DAILY_SUMMARY_ENABLED='0')
    if not args.rag_query:
        os.environ['CHUNKS_COLLECTION'] = query_prefix + '_empty_knowledge'
    from pymongo import MongoClient
    from fastapi.testclient import TestClient
    from knowledge.api.query_api import create_app
    from knowledge.api.market_api import get_market_service
    from knowledge.api.review_api import get_review_service
    from fastapi import HTTPException
    from knowledge.core.deps import get_query_service
    from knowledge.market.repository import MarketQuoteRepository
    from knowledge.service.market_service import MarketService
    from knowledge.service.query_service import QueryService
    from knowledge.service.risk_service import RiskService
    from knowledge.utils import mongo_history_util
    from knowledge.utils.answer_policy import DISCLAIMER

    # 验收只向指定测试库的 UUID 集合写入；公共向量库仅执行既有只读查询。
    client = MongoClient(os.environ['MONGO_URL'], serverSelectionTimeoutMS=5000, socketTimeoutMS=5000)
    prefix = 'risk_accept_' + uuid.uuid4().hex
    market, history = client[db][prefix + '_market'], client[db][prefix + '_history']
    original_collection = mongo_history_util._get_collection
    try:
        client.admin.command('ping')
        mongo_history_util._get_collection = lambda: history
        market_service = MarketService(MarketQuoteRepository(market))
        service = QueryService(risk=RiskService(lambda: market_service))
        app = create_app(summary_enabled=False)
        from knowledge.api.summary_api import get_summary_service
        def unavailable_summary():
            raise HTTPException(404, "此验收服务仅提供聊天与行情")
        app.dependency_overrides[get_summary_service] = unavailable_summary
        app.dependency_overrides[get_query_service] = lambda: service
        app.dependency_overrides[get_market_service] = lambda: market_service
        def unavailable_review():
            raise HTTPException(404, "此验收服务仅提供聊天与行情")
        app.dependency_overrides[get_review_service] = unavailable_review
        if args.serve:
            import uvicorn
            print(f'浏览器入口：http://127.0.0.1:{args.port}/chat.html；停止服务后清理本次集合。')
            uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')
        else:
            queries = ['平安银行（000001）的风险如何？', '贵州茅台000001风险', '上证指数000001风险']
            if args.rag_query:
                queries.append(args.rag_query)
            with TestClient(app) as api:
                for query in queries:
                    for streaming in (False, True):
                        session = uuid.uuid4().hex
                        response = api.post('/query', json={'query': query, 'session_id': session, 'is_stream': streaming})
                        assert response.status_code == 200
                        body = response.json()
                        if streaming:
                            task = body['task_id']
                            response = api.get('/stream/' + task)
                            events = [(block.splitlines()[0][7:], json.loads(block.splitlines()[1][6:]))
                                      for block in response.text.split('\n\n') if block.strip()]
                            assert events[-1][0] == 'final'
                            assert sum(event == 'final' for event, _ in events) == 1
                            answer = events[-1][1]['answer']
                            assert not events[-1][1].get('error')
                            assert ''.join(payload['answer'] for event, payload in events if event == 'delta') == answer
                            assert api.get('/status/' + task).json()['answer'] == answer
                        else:
                            assert not body.get('error')
                            answer = body['answer']
                        assert answer.endswith(DISCLAIMER) and answer.count(DISCLAIMER) == 1
                        assert api.get('/history/' + session).json()['items'][-1]['text'] == answer
                        if query == queries[0]:
                            assert '实际行情日期' in answer and '个股风险' in answer
                        elif query in queries[1:3]:
                            assert '确认' in answer
                        else:
                            assert 'LLM暂无' not in answer and '无法识别' not in answer
                        print(('流式' if streaming else '非流式') + ' API/历史/免责声明一致：PASS')
            print('本次查询验收通过。浏览器验收请另行运行 --serve；未执行的 RAG 不计为通过。')
        return 0
    except Exception:
        print('验收失败；检查行情、模型或存储可用性（异常详情不输出，以免泄露配置）。')
        return 1
    finally:
        mongo_history_util._get_collection = original_collection
        cleanup_failed = False
        try:
            market.drop()
            history.drop()
        except Exception:
            cleanup_failed = True
            print('本次独立测试集合清理失败，请在测试库检查 risk_accept_ 前缀集合。')
        client.close()
        if cleanup_failed:
            raise SystemExit(1)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    raise SystemExit(main())

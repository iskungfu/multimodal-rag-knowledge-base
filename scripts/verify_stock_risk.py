"""主机验收入口：实际聊天 API；--serve 提供隔离存储的浏览器验收服务。"""
import argparse
import json
import logging
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.acceptance_report import begin_report, finish_report
ENTRY_REPORT = begin_report({}) if __name__ == "__main__" else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true', help='在 127.0.0.1 启动浏览器验收服务')
    parser.add_argument('--port', type=int, default=8002)
    parser.add_argument('--rag-query', help='可选：针对主机已有公共知识库的问题，执行真实 BGE/RAG/LLM')
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / 'knowledge/.env')
    from fastapi.testclient import TestClient
    from knowledge.api.query_api import create_app
    from knowledge.runtime import QueryRuntime, RuntimeConfig
    from knowledge.utils.answer_policy import DISCLAIMER

    # 验收只向指定测试库的 UUID 集合写入；公共向量库仅执行既有只读查询。
    prefix = 'risk_accept_' + uuid.uuid4().hex
    report = begin_report({'run_id': prefix, 'passed': False, 'cleanup': {}}, entry_report=ENTRY_REPORT)
    runtime = None
    try:
        config = RuntimeConfig.acceptance('stock', prefix, shared_public=bool(args.rag_query))
        runtime = QueryRuntime(config)
        runtime.mongo.admin.command('ping')
        app = create_app(summary_enabled=False, runtime=runtime)
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
        report['passed'] = True
    except Exception:
        print('验收失败；检查行情、模型或存储可用性（异常详情不输出，以免泄露配置）。')
    finally:
        if runtime is not None:
            cleanup = runtime.close()
            report['cleanup'] = cleanup
            if not all(cleanup.values()):
                report['passed'] = False
                print('本次独立测试集合清理失败，请在测试库检查 risk_accept_ 前缀集合。')
        finish_report(report)
        output = Path(__file__).resolve().parents[1] / '.sandcastle/logs' / (prefix + '.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('REPORT', str(output))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    raise SystemExit(main())

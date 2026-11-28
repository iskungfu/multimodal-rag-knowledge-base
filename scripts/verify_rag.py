import argparse
import contextlib
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--pdf", action="store_true")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--knowledge", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv

    load_dotenv(ROOT / "knowledge/.env")
    test_db = os.getenv("AFK_TEST_DB_NAME")
    if not test_db or test_db in {os.getenv("MONGO_DB_NAME"), "kb001", "admin", "config", "local"}:
        print("必须提供独立 AFK_TEST_DB_NAME。")
        return 1
    run_id = "rag_accept_" + uuid.uuid4().hex[:12]
    collections = [run_id + "_items", run_id + "_chunks"]
    bucket = run_id.replace("_", "-")
    from knowledge.runtime import QueryRuntime, RuntimeConfig
    try:
        config = RuntimeConfig.acceptance("stock", run_id)
    except ValueError:
        print("必须提供与业务库不同的独立 AFK_TEST_DB_NAME。")
        return 1
    # 查询运行借用本脚本导入并管理的公共集合，其余资源由运行自身登记清理。
    config = replace(config, vector_names={**config.vector_names, "items": collections[0], "knowledge": collections[1]},
                     borrowed_vectors=frozenset(collections))
    os.environ.update({
        "MONGO_DB_NAME": test_db,
        "DAILY_SUMMARY_ENABLED": "0",
        "MARKET_COLLECTION_ENABLED": "0",
        "ITEM_NAME_COLLECTION": collections[0],
        "CHUNKS_COLLECTION": collections[1],
        "STRATEGY_COLLECTION": run_id + "_empty_personal",
        "REVIEW_COLLECTION": run_id + "_empty_review",
        "MINIO_BUCKET_NAME": bucket,
        "HF_HUB_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "OMP_NUM_THREADS": "2",
    })
    log_dir = ROOT / ".sandcastle/logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    report = {"run_id": run_id, "format": "pdf" if args.pdf else "markdown", "stream": args.stream, "legacy": args.legacy, "knowledge": args.knowledge, "checks": {}, "cleanup": {}, "passed": False}
    console = sys.stdout
    started = time.monotonic()
    clients = None
    runtime = None
    local_paths = []

    def check(name, passed, detail=None):
        report["checks"][name] = {"passed": bool(passed), "detail": detail}
        print(name, "PASS" if passed else "FAIL", file=console, flush=True)
        if not passed:
            raise AssertionError(name)

    with (log_dir / (run_id + ".log")).open("w", encoding="utf-8") as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            logging.basicConfig(stream=log, level=logging.INFO, force=True)
            try:
                from fastapi.testclient import TestClient
                from langchain_core.callbacks import BaseCallbackHandler
                from knowledge.api.import_api import create_app
                from knowledge.api.query_api import create_app as create_query_app
                from knowledge.utils.clients.storage_clients import StorageClients
                from knowledge.utils.task_util import get_task_info
                from knowledge.core.paths import get_local_base_dir

                clients = StorageClients
                mongo = clients.get_mongo_client()
                check("mongodb_ping", mongo.admin.command("ping")["ok"] == 1)
                milvus = clients.get_milvus_client()
                check("milvus_grpc", isinstance(milvus.list_collections(timeout=15), list))
                check("isolated_collections", not any(milvus.has_collection(name) for name in collections))
                runtime = QueryRuntime(config)
                query_api = create_query_app(summary_enabled=False, runtime=runtime)
                history_collection = runtime.history
                check("isolated_history", history_collection.name not in mongo[test_db].list_collection_names())

                if args.legacy:
                    from pymilvus import DataType
                    from knowledge.processor.import_processor.nodes.milvus_import_node import _MilvusIndexBuilder
                    for name, primary, text_fields in [
                        (collections[0], "pk", ["file_title", "item_name"]),
                        (collections[1], "id", ["item_name", "title", "parent_title", "file_title", "content"]),
                    ]:
                        schema = milvus.create_schema(enable_dynamic_field=False)
                        schema.add_field(primary, DataType.INT64, is_primary=True, auto_id=True)
                        for field in text_fields:
                            schema.add_field(field, DataType.VARCHAR, max_length=65535)
                        schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=1024)
                        schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
                        milvus.create_collection(name, schema=schema, index_params=_MilvusIndexBuilder.build_index_params(milvus))

                filename = run_id + (".pdf" if args.pdf else ".md")
                content = (
                    "# 青禾 QH-731 复盘记录器使用手册\n\n"
                    "## 离线复盘校验\n\n"
                    "青禾 QH-731 复盘记录器仅为测试用的虚构产品，不对应真实投资工具。"
                    "执行离线复盘校验时，必须先输入校验口令：青鹭7391。"
                    "然后将回看窗口设置为17个交易日，最后按下紫色确认键。"
                    "校验失败时显示橙色三角标志，禁止删除原始交易日记。"
                    "本手册的测试数据不构成投资建议。\n"
                ).encode("utf-8")
                mime_type = "text/markdown"
                if args.pdf:
                    import io
                    from reportlab.pdfbase import pdfmetrics
                    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
                    from reportlab.pdfgen import canvas
                    import fitz

                    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
                    buffer = io.BytesIO()
                    page = canvas.Canvas(buffer, pagesize=(595, 842))
                    page.setFont("STSong-Light", 15)
                    page.drawString(45, 785, "青禾 QH-731 复盘记录器使用手册")
                    page.setFont("STSong-Light", 12)
                    lines = [
                        "离线复盘校验", "青禾 QH-731 复盘记录器是虚构的验收测试产品。",
                        "校验口令：青鹭7391。", "回看窗口：17个交易日。",
                        "输入口令并设置窗口后，按下紫色确认键。",
                        "校验失败时显示橙色三角标志，禁止删除原始交易日记。",
                        "本手册的测试数据不构成投资建议。",
                    ]
                    for index, line in enumerate(lines):
                        page.drawString(45, 740 - index * 28, line)
                    page.save()
                    content = buffer.getvalue()
                    mime_type = "application/pdf"
                    with fitz.open(stream=content, filetype="pdf") as pdf:
                        check("pdf_fixture_text", "7391" in pdf[0].get_text())
                        pdf[0].get_pixmap().save(str(log_dir / (run_id + ".png")))
                from datetime import datetime
                base = Path(get_local_base_dir()) / datetime.now().strftime("%Y%m%d")
                local_paths = [base / filename, base / Path(filename).stem, base / (run_id + "_result.zip")]
                with TestClient(create_app()) as importer:
                    response = importer.post("/upload", files={"file": (filename, content, mime_type)})
                    check("upload_http", response.status_code == 200)
                    task_id = response.json()["task_id"]
                    status = importer.get("/status/" + task_id).json()
                    check("import_completed", status["status"] == "completed", status)
                objects = list(clients.get_minio().list_objects(bucket, recursive=True))
                check("no_original_or_image_backup", not objects)
                for name in collections:
                    milvus.flush(name, timeout=30)
                rows = milvus.query(collections[1], filter="", output_fields=["content", "dense_vector", "sparse_vector"], limit=20, consistency_level="Strong")
                check("real_vectors", bool(rows) and all(len(row["dense_vector"]) == 1024 and row["sparse_vector"] for row in rows), {"chunks": len(rows)})
                if args.legacy:
                    check("legacy_schema_preserved", all(
                        not {"owner_id", "doc_type", "document_id"}.intersection(field["name"] for field in milvus.describe_collection(name)["fields"])
                        for name in collections
                    ))

                class Observer(BaseCallbackHandler):
                    def on_chain_end(self, outputs, **kwargs):
                        if isinstance(outputs, dict):
                            for key in ("embedding_chunks", "hyde_embedding_chunks", "rrf_chunks", "reranked_docs", "web_search_docs"):
                                if key in outputs:
                                    observed[key] = outputs[key]
                            if "fused" in outputs:
                                observed["rrf_chunks"] = outputs["fused"]
                            if "selection_status" in outputs:
                                observed["selection_status"] = outputs["selection_status"]

                observed = {}
                query_service = runtime.query
                query_service.graph = query_service.graph_factory().with_config(callbacks=[Observer()])
                if args.knowledge:
                    investment = query_service.investment
                    investment.graph = investment.graph.with_config(callbacks=[Observer()])
                question = "青禾 QH-731 复盘记录器执行离线复盘校验时，校验口令是什么，回看窗口设为多少个交易日？请引用手册。"
                if args.knowledge:
                    question = "根据公共知识资料，测试手册中的离线复盘校验口令和回看窗口分别是什么？请引用来源。"
                with TestClient(query_api) as query_client:
                    response = query_client.post("/query", json={"query": question, "is_stream": args.stream})
                    check("query_http", response.status_code == 200)
                    result = response.json()
                    if args.stream:
                        task_id = result["task_id"]
                        check("stream_task_completed", get_task_info(task_id)["status"] == "completed")
                        from knowledge.utils.sse_util import get_sse_queue
                        stream_queue = get_sse_queue(task_id)
                        events = list(stream_queue.queue)
                        answer = "".join(event["data"].get("answer", "") for event in events if event["event"] == "delta")
                        finals = [event for event in events if event["event"] == "final"]
                        check("sse_delta_and_final", bool(answer) and len(finals) == 1)
                        check("stream_status_answer", query_client.get("/status/" + task_id).json()["answer"] == answer)
                        stream_response = query_client.get("/stream/" + task_id)
                        check("sse_http", stream_response.status_code == 200 and stream_response.text.count("event: final") == 1 and "event: delta" in stream_response.text)
                        check("sse_queue_cleaned", get_sse_queue(task_id) is None)
                    else:
                        answer = result.get("answer", "")
                    report["answer"] = answer
                    from knowledge.utils.answer_policy import DISCLAIMER

                    check("answer_disclaimer", answer.endswith(DISCLAIMER) and answer.count(DISCLAIMER) == 1)
                    report["retrieval_counts"] = {key: len(value) for key, value in observed.items() if isinstance(value, list)}
                    check("rrf_retrieval", bool(observed.get("rrf_chunks")))
                    docs = observed.get("reranked_docs", [])
                    check("real_reranker", bool(docs) and all(doc.get("score") is not None for doc in docs))
                    check("local_evidence", any("7391" in doc.get("content", "") and doc.get("source") == ("knowledge" if args.knowledge else "local") for doc in docs))
                    if args.knowledge:
                        check("actual_model_selection", observed.get("selection_status") == "completed")
                    check("grounded_answer", "7391" in answer and "17" in answer, answer)
                    session = result["session_id"]
                    history = query_client.get("/history/" + session).json()["items"]
                    check("history_persisted", len(history) == 2 and history[-1]["text"] == answer)
                    deleted = query_client.delete("/history/" + session).json()
                    check("history_deleted", deleted["deleted_count"] == 2 and not query_client.get("/history/" + session).json()["items"])
                report["passed"] = True
            except Exception as exc:
                report["error_type"] = type(exc).__name__
                print("验收异常详情已隐藏：" + type(exc).__name__)
            finally:
                query_stopped = True
                if runtime is not None:
                    report["resources"] = runtime.manifest()
                    report["cleanup"].update(runtime.close())
                    query_stopped = report["cleanup"].get("workers_stopped", False)
                if clients and query_stopped:
                    for name in collections:
                        try:
                            client = clients.get_milvus_client()
                            if client.has_collection(name):
                                client.drop_collection(name, timeout=30)
                            report["cleanup"][name] = True
                        except Exception:
                            report["cleanup"][name] = False
                    try:
                        client = clients.get_minio()
                        if client.bucket_exists(bucket):
                            for obj in client.list_objects(bucket, recursive=True):
                                client.remove_object(bucket, obj.object_name)
                            client.remove_bucket(bucket)
                        report["cleanup"]["minio"] = True
                    except Exception:
                        report["cleanup"]["minio"] = False
                for path in local_paths:
                    if path.parent.resolve().parent == Path(get_local_base_dir()).resolve() and path.name.startswith(run_id):
                        if path.is_dir():
                            shutil.rmtree(path)
                        else:
                            path.unlink(missing_ok=True)
                report["seconds"] = round(time.monotonic() - started, 2)
                report["passed"] = report["passed"] and all(report["cleanup"].values())
                output = log_dir / (run_id + ".json")
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REPORT", str(output), "PASS" if report["passed"] else "FAIL", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

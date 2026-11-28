"""#4 独立存储探针与主机验收入口。只使用随机测试资源和 AFK_TEST_DB_NAME。"""

import argparse
import io
import json
import logging
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.acceptance_report import begin_report, finish_report
ENTRY_REPORT = begin_report({}) if __name__ == "__main__" else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", action="store_true", help="使用主机现有 BGE-M3 运行真实 API/图；不下载模型")
    parser.add_argument("--pdf", action="store_true", help="主机额外验收真实 MinerU PDF 解析")
    args = parser.parse_args()
    from dotenv import load_dotenv
    if args.host:
        load_dotenv(ROOT / "knowledge/.env")
    if args.pdf and not args.host:
        parser.error("--pdf 需要 --host")
    logging.disable(logging.CRITICAL)
    os.environ["HF_HUB_OFFLINE"] = "1"
    report = {"mode": "host" if args.host else "storage", "checks": {}, "cleanup": {}, "passed": False}
    begin_report(report, entry_report=ENTRY_REPORT)
    mongo = milvus = minio = None
    created_collections = []
    mongo_collection = None
    bucket_created = False
    run_id = "strategy_test_" + uuid.uuid4().hex[:16]
    collection_name, legacy_name = run_id + "_chunks", run_id + "_legacy"
    bucket = run_id.replace("_", "-")
    dim = 1024 if args.host else 2

    def check(name, passed):
        report["checks"][name] = bool(passed)
        print(name, "PASS" if passed else "FAIL", flush=True)
        if not passed:
            raise AssertionError(name)

    try:
        from pymongo import MongoClient
        from pymilvus import MilvusClient, DataType
        from minio import Minio
        from knowledge.strategy.repository import StrategyRepository
        from knowledge.strategy.vectors import StrategyVectors
        from knowledge.strategy.scope import IsolationError, retrieval_filter
        from knowledge.utils.milvus_util import create_hybrid_search_requests, execute_hybrid_search_query

        db_name = os.environ["AFK_TEST_DB_NAME"]
        assert db_name and db_name not in {os.getenv("MONGO_DB_NAME", "kb001"), "kb001", "admin", "local", "config"}
        mongo = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000,
                            connectTimeoutMS=5000, socketTimeoutMS=5000)
        check("mongodb_ping", mongo.admin.command("ping")["ok"] == 1)
        mongo_collection = mongo[db_name][run_id]
        repository = StrategyRepository(mongo_collection)
        repository.create("a", "default_user", "测试策略", "test.md")
        repository.create("b", "other_user", "其他策略", "other.md")
        check("mongodb_owner_filter", repository.get("b", "default_user") is None and len(repository.recent("default_user")) == 1)
        repository.finish("b", "default_user", "completed")
        check("mongodb_update_isolation", repository.get("b", "other_user")["status"] == "processing")
        repository.finish("a", "default_user", "failed", "测试解析失败")
        check("mongodb_failure_persisted", repository.get("a", "default_user")["status"] == "failed")

        milvus = MilvusClient(uri=os.environ["MILVUS_URL"], timeout=10)
        check("milvus_unique_resources", not milvus.has_collection(collection_name) and not milvus.has_collection(legacy_name))
        vectors = StrategyVectors(milvus, collection_name, dim)
        created_collections.append(collection_name)
        vectors.prepare()
        dense, sparse = [1.] + [0.] * (dim - 1), {1: 1.0, 7: .5}
        chunks = [{"text": "测试个人策略：避免追高。手机号 13812345678", "title": "策略", "file_title": "test", "dense_vector": dense, "sparse_vector": sparse}]
        vectors.insert(chunks, "default_user", "a")
        vectors.insert(chunks, "other_user", "b")
        milvus.flush(collection_name, timeout=30)
        own = vectors.search(dense, sparse, "default_user")[0]
        other = vectors.search(dense, sparse, "other_user")[0]
        check("milvus_owner_filter", len(own) == len(other) == 1 and own[0]["entity"]["document_id"] == "a" and other[0]["entity"]["document_id"] == "b")
        check("milvus_write_and_read_redaction", "13812345678" not in str(milvus.query(collection_name, filter='document_id == "a"', output_fields=["content"], consistency_level="Strong")))
        check("milvus_unknown_owner_empty", vectors.search(dense, sparse, 'missing" or true')[0] == [])
        public = execute_hybrid_search_query(milvus, collection_name, create_hybrid_search_requests(dense, sparse))
        check("generic_search_excludes_personal", public == [[]])
        # 公共检索仍可返回 knowledge，个人检索不能混入公共行。
        from knowledge.processor.import_processor.nodes.milvus_import_node import _MilvusInserter
        public_row = _MilvusInserter._build_rows(chunks)[0]
        public_row["item_name"] = "public fixture"
        milvus.insert(collection_name, public_row)
        milvus.flush(collection_name, timeout=30)
        public = execute_hybrid_search_query(milvus, collection_name, create_hybrid_search_requests(dense, sparse), output_fields=["doc_type", "owner_id"])
        check("generic_search_public_regression", len(public[0]) == 1 and public[0][0]["entity"]["doc_type"] == "knowledge")
        check("personal_search_excludes_knowledge", len(vectors.search(dense, sparse, "default_user")[0]) == 1)
        vectors.delete_document("default_user", "b")
        check("delete_scope_preserves_other", len(vectors.search(dense, sparse, "other_user")[0]) == 1)

        schema = milvus.create_schema()
        schema.add_field("id", DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field("item_name", DataType.VARCHAR, max_length=128)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=2)
        created_collections.append(legacy_name)
        milvus.create_collection(legacy_name, schema=schema, timeout=30)
        milvus.insert(legacy_name, {"item_name": "legacy marker", "vector": [1., 0.]})
        try:
            StrategyVectors(milvus, legacy_name).prepare()
            refused = False
        except IsolationError:
            refused = True
        check("legacy_personal_write_refused", refused)
        try:
            retrieval_filter(milvus, legacy_name)
            refused = False
        except IsolationError:
            refused = True
        check("legacy_read_requires_explicit_configuration", refused)
        os.environ["LEGACY_PUBLIC_COLLECTIONS"] = legacy_name
        check("legacy_public_read_opt_in", retrieval_filter(milvus, legacy_name) == 'item_name != ""')
        fields = {field["name"] for field in milvus.describe_collection(legacy_name)["fields"]}
        check("legacy_schema_preserved", fields == {"id", "item_name", "vector"})
        indexes = milvus.prepare_index_params()
        indexes.add_index("vector", index_type="AUTOINDEX", metric_type="COSINE")
        milvus.create_index(legacy_name, indexes)
        milvus.load_collection(legacy_name)
        check("legacy_data_preserved", len(milvus.query(legacy_name, filter='item_name == "legacy marker"', consistency_level="Strong")) == 1)

        # 独立 MinIO 连通/清理探针；个人策略不保存原始对象，此检查不是 PDF 脱敏验收。
        minio = Minio(os.environ["MINIO_ENDPOINT"], access_key=os.environ["MINIO_ACCESS_KEY"],
                      secret_key=os.environ["MINIO_SECRET_KEY"], secure=False)
        check("minio_unique_bucket", not minio.bucket_exists(bucket))
        minio.make_bucket(bucket)
        bucket_created = True
        if args.host:
            # 主机验收中任何意外的对象写入也限定在本次独立 bucket。
            os.environ["MINIO_BUCKET_NAME"] = bucket
            os.environ["MONGO_DB_NAME"] = db_name
        payload = b"synthetic storage probe"
        minio.put_object(bucket, "probe.txt", io.BytesIO(payload), len(payload))
        response = minio.get_object(bucket, "probe.txt")
        try:
            check("minio_roundtrip", response.read() == payload)
        finally:
            response.close()
            response.release_conn()

        if args.host:
            from fastapi.testclient import TestClient
            from knowledge.api.import_api import create_app
            from knowledge.api.strategy_api import get_strategy_service
            from knowledge.service.strategy_service import StrategyService
            from knowledge.utils.clients.ai_clients import AIClients
            from knowledge.utils.embedding_util import generate_bge_m3_hybrid_vectors
            service = StrategyService(repository, vectors)
            app = create_app()
            app.dependency_overrides[get_strategy_service] = lambda: service
            text = "# 投资心得\n\n只在完成复盘后交易，避免连续追高。校验短语：青鹭7391。手机号 13812345678。\n\n**姓名**：王小明。**持仓金额**：523456元。\n\n| 姓名 | 持仓金额 |\n| --- | --- |\n| 王小明 | 523456元 |"
            text += "\n\n| 姓名 | 持仓金额 | 备注 |\n| --- | --- | --- |\n王小明 | 523456元\n\n| 手机号 13812345678 | 内容 |\n| --- | --- |\n| 测试 | 复盘 |\n\n<table><tr><td>姓名</td><td>王小明</td></tr><tr><td>持仓金额</td><td>523456元</td></tr></table>"
            with TestClient(app) as client:
                formats = ["text", "markdown"] + (["pdf"] if args.pdf else [])
                for kind in formats:
                    if kind == "text":
                        response = client.post("/strategies/text", json={"title": "测试策略", "text": text})
                    else:
                        data = text.encode()
                        if kind == "pdf":
                            from reportlab.pdfbase import pdfmetrics
                            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
                            from reportlab.pdfgen import canvas
                            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
                            buffer = io.BytesIO()
                            page = canvas.Canvas(buffer)
                            page.setFont("STSong-Light", 12)
                            for index, line in enumerate(["投资心得", "只在完成复盘后交易，避免连续追高。", "校验短语：青鹭7391。", "手机号 13812345678。"]):
                                page.drawString(50, 780 - index * 30, line)
                            page.save()
                            data = buffer.getvalue()
                        response = client.post("/strategies/upload", files={"file": (f"test.{kind}", data)})
                    check(kind + "_api_accepted", response.status_code == 202)
                    task_id = response.json()["task_id"]
                    status = client.get("/strategies/" + task_id).json()
                    check(kind + "_real_graph_completed", status["status"] == "completed" and status["chunk_count"] > 0)
                    check(kind + "_private_status", client.get("/status/" + task_id).status_code == 404)
                    milvus.flush(collection_name, timeout=30)
                    rows = milvus.query(collection_name, filter=f'document_id == "{task_id}"', output_fields=["dense_vector", "sparse_vector", "content"], consistency_level="Strong")
                    check(kind + "_real_bge_vectors", bool(rows) and all(len(row["dense_vector"]) == 1024 and row["sparse_vector"] for row in rows))
                    check(kind + "_redacted", not any(value in str(rows) for value in ("13812345678", "王小明", "523456")))
            query_vectors = generate_bge_m3_hybrid_vectors(AIClients.get_bge_m3_client(), ["投资心得校验短语是什么？"])
            hits = vectors.search(query_vectors["dense"][0], query_vectors["sparse"][0], "default_user", limit=20)[0]
            check("host_real_recall", any("7391" in hit["entity"]["content"] for hit in hits))
            check("host_other_owner_no_evidence", not any("7391" in hit["entity"]["content"] for hit in vectors.search(query_vectors["dense"][0], query_vectors["sparse"][0], "other_user", limit=20)[0]))
            check("personal_import_no_minio_objects", [obj.object_name for obj in minio.list_objects(bucket, recursive=True)] == ["probe.txt"])
        report["passed"] = True
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        print("验证失败，错误类型：" + type(exc).__name__, flush=True)
    finally:
        for name in created_collections:
            try:
                if milvus.has_collection(name):
                    milvus.drop_collection(name, timeout=30)
                report["cleanup"][name] = True
            except Exception:
                report["cleanup"][name] = False
        if bucket_created:
            try:
                for obj in minio.list_objects(bucket, recursive=True):
                    minio.remove_object(bucket, obj.object_name)
                minio.remove_bucket(bucket)
                report["cleanup"]["minio"] = True
            except Exception:
                report["cleanup"]["minio"] = False
        if mongo_collection is not None:
            try:
                mongo_collection.drop()
                report["cleanup"]["mongodb"] = True
            except Exception:
                report["cleanup"]["mongodb"] = False
        if mongo is not None:
            mongo.close()
        if milvus is not None:
            milvus.close()
        report["passed"] = report["passed"] and all(report["cleanup"].values())
        output = ROOT / ".sandcastle/logs" / (run_id + ".json")
        output.parent.mkdir(parents=True, exist_ok=True)
        finish_report(report)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("REPORT", output.name, "PASS" if report["passed"] else "FAIL", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

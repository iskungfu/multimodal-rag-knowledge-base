"""策略 API/真实图节点的单元验证；模型与外部存储为受控替身。"""

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

from fastapi.testclient import TestClient
import mongomock
from pymilvus import DataType
import pytest

from knowledge.api.import_api import create_app
from knowledge.api.strategy_api import get_strategy_service
from knowledge.processor.import_processor.config import ImportConfig
from knowledge.processor.import_processor.nodes.pdf_to_md_node import PdfToMdNode
from knowledge.strategy.pipeline import build_strategy_graph
from knowledge.strategy.repository import StrategyRepository
from knowledge.strategy.scope import IsolationError, retrieval_filter
from knowledge.strategy.vectors import StrategyVectors
from knowledge.service.strategy_service import StrategyService
from knowledge.utils.milvus_util import create_hybrid_search_requests, execute_hybrid_search_query

SENSITIVE = "姓名：王小明。手机号 13812345678。身份证 110101199001011234。银行卡 6222021234567890123。持仓金额 523456元。"
SECRETS = ("王小明", "13812345678", "110101199001011234", "6222021234567890123", "523456")


@pytest.fixture
def setup_service(monkeypatch):
    collection = mongomock.MongoClient().test.strategy
    repository = StrategyRepository(collection)
    vectors = Mock()
    captured = []

    def insert(chunks, owner_id, document_id):
        captured.extend(chunks)
        assert owner_id == "default_user"
        assert document_id
        return len(chunks)

    vectors.insert.side_effect = insert
    config = ImportConfig(max_content_length=400, min_content_length=20)
    graph = build_strategy_graph(vectors, config)
    service = StrategyService(repository, vectors, graph)
    from knowledge.processor.import_processor.nodes import bge_embedding_chunks_node as embedding
    monkeypatch.setattr(embedding.AIClients, "get_bge_m3_client", lambda: object())

    def fake_vectors(model, documents):
        assert not any(secret in "".join(documents) for secret in SECRETS)
        return {"dense": [[1.0, 0.0] for _ in documents], "sparse": [{1: 1.0} for _ in documents]}

    monkeypatch.setattr(embedding, "generate_bge_m3_hybrid_vectors", fake_vectors)
    from knowledge.utils.clients.storage_clients import StorageClients
    monkeypatch.setattr(StorageClients, "get_minio", Mock(side_effect=AssertionError("个人原图不应上传")))
    app = create_app()
    app.dependency_overrides[get_strategy_service] = lambda: service
    with TestClient(app) as client:
        yield SimpleNamespace(service=service, client=client, captured=captured, vectors=vectors, collection=collection)


@pytest.mark.parametrize("kind", ["text", "md", "markdown", "txt", "pdf"])
def test_formats_share_split_embed_store_and_redact(setup_service, monkeypatch, kind):
    env = setup_service
    content = "# " + SENSITIVE + "\n\n只在复盘后交易。![账户](images/raw.png)\n" + "不要追高。" * 100
    if kind == "text":
        response = env.client.post("/strategies/text", json={"title": SENSITIVE, "text": content})
    else:
        data = content.encode()
        if kind == "pdf":
            data = b"%PDF-fake-unit-fixture"

            def parse(self, state):
                directory = Path(state["file_dir"])
                (directory / "raw.png").write_bytes(b"private-original-image")
                path = directory / "parsed.md"
                path.write_text(content, encoding="utf-8")
                return {**state, "md_path": str(path)}

            monkeypatch.setattr(PdfToMdNode, "process", parse)
        response = env.client.post("/strategies/upload", files={"file": (f"../../13812345678.{kind}", data)},
                                   data={"owner_id": "attacker", "doc_type": "knowledge"})
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    status = env.client.get("/strategies/" + task_id).json()
    assert status["status"] == "completed", status
    assert status["owner_id"] == "default_user" and status["doc_type"] == "personal"
    assert status["chunk_count"] > 1
    assert "策略脱敏" in status["done_list"] and "主体名称识别" not in status["done_list"]
    assert "Markdown图片处理" not in status["done_list"]
    encoded = json.dumps([env.captured, status, list(env.collection.find())], default=str, ensure_ascii=False)
    assert not any(secret in encoded for secret in SECRETS)
    assert "images/raw.png" not in encoded
    assert env.client.get("/status/" + task_id).status_code == 404
    assert env.client.get("/strategies").json()["items"][0]["task_id"] == task_id


def test_private_temp_names_and_cleanup_on_success(setup_service):
    env = setup_service
    state = env.service.stage(b"# test\nstrategy", "../../same.md", "../../escape")
    directory = Path(state["file_dir"])
    assert directory.parent == Path(__import__('tempfile').gettempdir())
    assert Path(state["import_file_path"]).name == "input.md"
    if os.name == "posix":
        assert directory.stat().st_mode & 0o077 == 0
    second = env.service.stage(b"another strategy", "same.md")
    assert second["file_dir"] != state["file_dir"]
    env.service.run(state)
    env.service.run(second)
    assert not directory.exists() and not Path(second["file_dir"]).exists()
    assert env.service.get(state["task_id"])["status"] == "completed"


@pytest.mark.parametrize("failure", ["schema", "pdf", "embedding", "insert"])
def test_failure_is_persistent_private_and_cleans_temp(setup_service, monkeypatch, failure):
    env = setup_service
    data, filename = b"test strategy", "test.md"
    if failure == "schema":
        env.vectors.prepare.side_effect = IsolationError("collection 缺少隔离字段")
    elif failure == "pdf":
        data, filename = b"%PDF-test", "test.pdf"
        monkeypatch.setattr(PdfToMdNode, "process", Mock(side_effect=RuntimeError("private-token")))
    elif failure == "embedding":
        from knowledge.processor.import_processor.nodes.bge_embedding_chunks_node import BgeEmbeddingChunksNode
        monkeypatch.setattr(BgeEmbeddingChunksNode, "process", Mock(side_effect=RuntimeError("private-token")))
    else:
        env.vectors.insert.side_effect = RuntimeError("private-token")
    state = env.service.stage(data, filename)
    env.service.run(state)
    assert not Path(state["file_dir"]).exists()
    result = env.service.get(state["task_id"])
    assert result["status"] == "failed" and result["error"]
    assert "private-token" not in str(result)
    assert env.collection.find_one()["status"] == "failed"
    if failure == "schema":
        env.vectors.insert.assert_not_called()
        env.vectors.delete_document.assert_not_called()
    else:
        env.vectors.delete_document.assert_called_once_with("default_user", state["task_id"])


def test_user_isolation_and_response_redaction(setup_service):
    env = setup_service
    env.service.repository.create("other", "other_user", "private title", "private.md")
    assert env.client.get("/strategies/other").status_code == 404
    assert env.client.get("/strategies").json()["items"] == []
    env.service.repository.finish("other", "default_user", "completed")
    assert env.collection.find_one()["status"] == "processing"
    # 模拟旧数据，返回端再次脱敏。
    env.collection.insert_one({"_id": "old", "owner_id": "default_user", "doc_type": "personal", "title": SENSITIVE})
    result = env.client.get("/strategies/old").json()
    assert not any(secret in str(result) for secret in SECRETS)
    assert env.client.post("/strategies/text", json={"text": "test", "owner_id": "other"}).status_code == 422
    assert env.client.post("/strategies/text", json={"text": "test", "doc_type": "knowledge"}).status_code == 422


@pytest.mark.parametrize("filename,data", [("bad.exe", b"test"), ("a.md", b""), ("a.md", b" \n"),
                                            ("a.pdf", b"not pdf"), ("a.md", b"\xff\xff")])
def test_invalid_input_never_creates_task(setup_service, filename, data):
    env = setup_service
    assert env.client.post("/strategies/upload", files={"file": (filename, data)}).status_code == 422
    assert env.collection.count_documents({}) == 0
    env.vectors.prepare.assert_not_called()


def description(scoped=True, dynamic=False):
    names = ["item_name"] + (["owner_id", "doc_type", "document_id"] if scoped else [])
    return {"fields": [{"name": name, "type": DataType.VARCHAR} for name in names], "enable_dynamic_field": dynamic}


@pytest.mark.parametrize("owner,doc_type", [(None, "knowledge"), ('user" OR true', "personal")])
def test_every_dense_sparse_request_gets_scope_and_response_redaction(owner, doc_type):
    client = Mock()
    client.describe_collection.return_value = description()
    client.hybrid_search.return_value = [[{"id": 1, "entity": {"content": SENSITIVE}}]]
    requests = create_hybrid_search_requests([1., 0.], {1: 1.}, expr="item_name in {names}", expr_params={"names": ["x"]})
    result = execute_hybrid_search_query(client, "test", requests, owner_id=owner, doc_type=doc_type)
    for request in client.hybrid_search.call_args.kwargs["reqs"]:
        assert request.expr.startswith('(' + retrieval_filter(client, "test", owner, doc_type) + ') and (')
        assert request.expr_params == {"names": ["x"]}
    assert not any(secret in str(result) for secret in SECRETS)
    assert requests[0].expr == "item_name in {names}"  # 不污染调用方复用的请求


def test_legacy_schema_is_explicit_read_only_and_personal_never_falls_back(monkeypatch):
    client = Mock()
    client.describe_collection.return_value = description(False)
    monkeypatch.delenv("LEGACY_PUBLIC_COLLECTIONS", raising=False)
    with pytest.raises(IsolationError):
        retrieval_filter(client, "legacy")
    monkeypatch.setenv("LEGACY_PUBLIC_COLLECTIONS", "legacy")
    assert retrieval_filter(client, "legacy") == 'item_name != ""'
    with pytest.raises(IsolationError):
        retrieval_filter(client, "legacy", "a", "personal")
    client.has_collection.return_value = True
    with pytest.raises(IsolationError):
        StrategyVectors(client, "legacy").prepare()
    client.insert.assert_not_called()
    client.drop_collection.assert_not_called()
    for desc in [description(False, True), {"fields": [{"name": "owner_id", "type": DataType.VARCHAR}]}]:
        client.describe_collection.return_value = desc
        with pytest.raises(IsolationError):
            retrieval_filter(client, "legacy")


def test_missing_owner_cannot_search_personal():
    client = Mock()
    client.describe_collection.return_value = description()
    with pytest.raises(RuntimeError):
        execute_hybrid_search_query(client, "test", create_hybrid_search_requests([1.], {1: 1.}), doc_type="personal")
    client.hybrid_search.assert_not_called()


@pytest.mark.parametrize("filename", ["../escape.md", "/escape.md", "..\\escape.md"])
def test_pdf_archive_cannot_escape_task_directory(tmp_path, monkeypatch, filename):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(filename, "secret")
        archive.writestr("full.md", "valid")
    response = Mock()
    response.iter_content.return_value = [buffer.getvalue()]
    node = PdfToMdNode()
    monkeypatch.setattr(node, "_request", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError):
        node._extract_md("unused", tmp_path, Path("input.pdf"))


def test_pdf_archive_extracts_normal_markdown(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("full.md", "# strategy")
        archive.writestr("images/a.png", b"fixture")
    response = Mock()
    response.iter_content.return_value = [buffer.getvalue()]
    node = PdfToMdNode()
    monkeypatch.setattr(node, "_request", lambda *args, **kwargs: response)
    path = node._extract_md("unused", tmp_path, Path("input.pdf"))
    assert Path(path).read_text() == "# strategy"


def test_query_service_status_cannot_bypass_personal_endpoint(setup_service):
    from knowledge.api.query_api import create_app as create_query_app
    env = setup_service
    task_id = env.client.post("/strategies/text", json={"text": "test strategy"}).json()["task_id"]
    with TestClient(create_query_app()) as query_client:
        assert query_client.get("/status/" + task_id).status_code == 404


def test_metadata_write_failure_does_not_leave_temp_file_or_start_graph(setup_service, monkeypatch):
    env = setup_service
    directories = []
    import tempfile
    original = tempfile.mkdtemp

    def make_dir(**kwargs):
        directory = original(**kwargs)
        directories.append(Path(directory))
        return directory

    monkeypatch.setattr(tempfile, "mkdtemp", make_dir)
    monkeypatch.setattr(env.service.repository, "create", Mock(side_effect=RuntimeError("private-token")))
    response = env.client.post("/strategies/text", json={"text": "strategy"})
    assert response.status_code == 503 and "private-token" not in response.text
    assert directories and all(not directory.exists() for directory in directories)
    env.vectors.prepare.assert_not_called()


def test_finish_storage_failure_is_visible(setup_service, monkeypatch):
    env = setup_service
    monkeypatch.setattr(env.service.repository, "finish", Mock(side_effect=RuntimeError("private-token")))
    response = env.client.post("/strategies/text", json={"text": "strategy"})
    result = env.client.get("/strategies/" + response.json()["task_id"]).json()
    assert result["status"] == "failed"
    assert result["error"] == "导入状态写入失败，请稍后核对"


def test_vector_rows_and_public_import_preserve_scope():
    from knowledge.processor.import_processor.nodes.milvus_import_node import MilvusImportNode
    from knowledge.utils.clients.storage_clients import StorageClients
    from unittest.mock import patch
    client = Mock()
    client.has_collection.return_value = True
    client.describe_collection.return_value = description()
    client.insert.return_value = {"ids": [1], "insert_count": 1}
    node = MilvusImportNode(ImportConfig(chunks_collection="scoped"))
    chunk = {"text": "public content", "item_name": "test", "dense_vector": [1., 0.], "sparse_vector": {1: 1.}}
    with patch.object(StorageClients, "get_milvus_client", return_value=client):
        node.process({"chunks": [chunk]})
    row = client.insert.call_args.kwargs["data"][0]
    assert row["owner_id"] == "public" and row["doc_type"] == "knowledge"
    private = StrategyVectors(client, "scoped", dim=2)
    private.insert([{**chunk, "title": SENSITIVE, "text": SENSITIVE}], "user_a", "doc_a")
    row = client.insert.call_args.args[1][0]
    assert row["owner_id"] == "user_a" and row["doc_type"] == "personal"
    assert not any(secret in str(row) for secret in SECRETS)


def test_generic_search_checks_schema_before_retrieval(monkeypatch):
    client = Mock()
    client.describe_collection.return_value = description(False)
    monkeypatch.delenv("LEGACY_PUBLIC_COLLECTIONS", raising=False)
    with pytest.raises(IsolationError):
        execute_hybrid_search_query(client, "unknown", create_hybrid_search_requests([1.], {1: 1.}))
    client.hybrid_search.assert_not_called()


def test_validation_response_never_echoes_private_input(setup_service):
    response = setup_service.client.post("/strategies/text", json={"text": "strategy", "unexpected": SENSITIVE})
    assert response.status_code == 422
    assert not any(secret in response.text for secret in SECRETS)


def test_labeled_filename_name_is_redacted(setup_service):
    response = setup_service.client.post("/strategies/upload", files={"file": ("姓名：王小明_策略.md", b"test")})
    result = setup_service.client.get("/strategies/" + response.json()["task_id"]).json()
    assert "王小明" not in str(result) and "王**" in result["filename"]


def test_internal_identifiers_with_phone_digits_stay_queryable(setup_service, monkeypatch):
    from uuid import UUID
    identifier = UUID("abcdefab-cdef-4a13-8123-45678abcdef0")
    monkeypatch.setattr("knowledge.service.strategy_service.uuid4", lambda: identifier)
    env = setup_service
    response = env.client.post("/strategies/text", json={"text": "手机号 13812345678。复盘后才交易。"})
    assert response.json()["task_id"] == identifier.hex
    status = env.client.get("/strategies/" + identifier.hex)
    assert status.status_code == 200
    assert status.json()["task_id"] == identifier.hex
    assert status.json()["status"] == "completed"
    assert env.collection.find_one()["_id"] == identifier.hex
    client = Mock()
    client.describe_collection.return_value = description()
    client.hybrid_search.return_value = [[{"id": 1, "entity": {
        "document_id": identifier.hex, "owner_id": identifier.hex, "content": SENSITIVE,
    }}]]
    result = StrategyVectors(client, "scoped").search([1.], {1: 1.}, identifier.hex)
    assert result[0][0]["entity"]["document_id"] == identifier.hex
    assert result[0][0]["entity"]["owner_id"] == identifier.hex
    assert "13812345678" not in result[0][0]["entity"]["content"]


@pytest.mark.parametrize("content", [
    "姓名：\n王小明。持仓金额：\n523456元。买入单价 12.5元。",
    "**姓名**：**王小明**。**持仓金额**：**523456元**。买入单价 12.5 元。",
    "| 字段 | 内容 |\n| --- | --- |\n| 姓名 | 王小明 |\n| 持仓金额 | 523456元 |\n| 买入单价 | 12.5元 |",
    "| **姓名** | **持仓金额** | 买入单价 |\n| --- | ---: | --- |\n| 王小明 | 523456元 | 12.5元 |",
    "| 手机号 13812345678 | 姓名：王小明 | 持仓金额：523456元 |\n| --- | --- | --- |\n| 策略 | 复盘 | 12.5元 |",
    "| 姓名 | 持仓金额 | 买入单价 |\n| --- | --- | --- |\n王小明 | 523456元 | 12.5元",
    "| 姓名 | 持仓金额 | 买入单价 | 备注 |\n| --- | --- | --- | --- |\n王小明 | 523456元 | 12.5元",
    "<table><tr><td>姓名</td><td>王小明</td></tr><tr><td>持仓金额</td><td>523456元</td></tr><tr><td>买入单价</td><td>12.5元</td></tr></table>",
    "<table><tr><th>姓名</th><th>持仓金额</th><th>买入单价</th></tr><tr><td>王小明</td><td>523456元</td><td>12.5元</td></tr></table>",
    "<td>姓名</td><td>王小明</td><td>持仓金额</td><td>523456元</td>买入单价12.5元。",
    "【姓名】：王小明。 【持仓金额】：523456元。 买入单价12.5元。",
])
def test_markdown_labels_and_tables_redact_before_embedding_and_on_read(setup_service, content):
    from knowledge.review.privacy import redact_text
    env = setup_service
    response = env.client.post("/strategies/upload", files={"file": ("test.md", content.encode("utf-8"))})
    assert response.status_code == 202
    assert env.client.get("/strategies/" + response.json()["task_id"]).json()["status"] == "completed"
    stored = json.dumps(env.captured, ensure_ascii=False)
    assert "王小明" not in stored and "523456" not in stored
    assert "12.5" in stored
    safe = redact_text(content)
    assert redact_text(safe) == safe
    assert "持仓金额" in safe
    env.collection.insert_one({"_id": "markdown_old", "owner_id": "default_user", "doc_type": "personal", "title": content})
    returned = env.client.get("/strategies/markdown_old").text
    assert "王小明" not in returned and "523456" not in returned


def test_configured_legacy_public_schema_keeps_import_and_retrieval(monkeypatch):
    from knowledge.strategy.scope import public_rows_for_collection
    client = Mock()
    fields = {"id", "item_name", "title", "parent_title", "file_title", "content", "dense_vector", "sparse_vector"}
    client.describe_collection.return_value = {
        "fields": [{"name": name, "type": DataType.VARCHAR} for name in fields],
        "enable_dynamic_field": False,
    }
    monkeypatch.setenv("CHUNKS_COLLECTION", "configured_legacy")
    monkeypatch.delenv("LEGACY_PUBLIC_COLLECTIONS", raising=False)
    assert retrieval_filter(client, "configured_legacy") == 'item_name != ""'
    row = {"item_name": "test", "content": "public", "owner_id": "public", "doc_type": "knowledge", "document_id": ""}
    assert public_rows_for_collection(client, "configured_legacy", [row]) == [{"item_name": "test", "content": "public"}]
    with pytest.raises(IsolationError):
        public_rows_for_collection(client, "configured_legacy", [{**row, "doc_type": "personal"}])
    with pytest.raises(IsolationError):
        retrieval_filter(client, "configured_legacy", "default_user", "personal")
    with pytest.raises(IsolationError):
        public_rows_for_collection(client, "unknown", [row])


def test_same_pdf_can_be_extracted_twice_on_windows(tmp_path, monkeypatch):
    node = PdfToMdNode()
    for content in ["# first import", "# repeated import"]:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("full.md", content)
        response = Mock()
        response.iter_content.return_value = [buffer.getvalue()]
        monkeypatch.setattr(node, "_request", lambda *args, **kwargs: response)
        path = node._extract_md("unused", tmp_path, Path("same.pdf"))
        assert Path(path).read_text(encoding="utf-8") == content

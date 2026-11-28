import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import UploadFile
from fastapi.testclient import TestClient
from pymilvus import DataType
import pytest

from knowledge.api.import_api import create_app
from knowledge.processor.import_processor.nodes.pdf_to_md_node import PdfToMdNode
from knowledge.service.file_process_service import FileProcessService
from knowledge.utils.clients.storage_clients import StorageClients
from knowledge.utils.task_util import get_task_info, get_task_result


PRIVATE = "姓名：王小明。手机号：13812345678。身份证：110101199001011234。银行卡：6222021234567890123。持仓金额：523456元。"
SECRETS = ("王小明", "13812345678", "110101199001011234", "6222021234567890123", "523456")
CONTENT = "# 公共书籍\n\n" + PRIVATE + "\n买入价12.5元，复盘后才考虑交易。\n![账户截图](images/private.png)\n" + "保留策略的适用前提。" * 100


@pytest.fixture
def public_import(monkeypatch):
    from knowledge.processor.import_processor import config
    monkeypatch.setattr(config, "_config", config.ImportConfig(
        chunks_collection="offline_public_chunks", item_name_collection="offline_public_items"))
    from knowledge.processor.import_processor.main_graph import import_graph
    monkeypatch.setattr("knowledge.service.file_process_service.import_app", import_graph())
    from knowledge.processor.import_processor.nodes import bge_embedding_chunks_node as embedding
    from knowledge.processor.import_processor.nodes import item_name_recognition_node as names
    stored, embedded, directories = [], [], []
    storage = Mock()
    storage.has_collection.return_value = True
    storage.describe_collection.return_value = {"fields": [
        {"name": name, "type": DataType.VARCHAR} for name in ("owner_id", "doc_type", "document_id")
    ]}

    def insert(*args, **kwargs):
        rows = kwargs["data"]
        rows = [rows] if isinstance(rows, dict) else rows
        stored.extend(rows)
        return {"ids": list(range(1, len(rows) + 1))}

    def vectors(model, documents):
        embedded.extend(documents)
        return {"dense": [[1.0, 0.0] for _ in documents], "sparse": [{1: 1.0} for _ in documents]}

    storage.insert.side_effect = insert
    monkeypatch.setattr(StorageClients, "get_milvus_client", lambda: storage)
    minio = Mock(side_effect=AssertionError("不得备份原件或原图"))
    monkeypatch.setattr(StorageClients, "get_minio", minio)
    monkeypatch.setattr(embedding.AIClients, "get_bge_m3_client", lambda: object())
    monkeypatch.setattr(embedding, "generate_bge_m3_hybrid_vectors", vectors)
    monkeypatch.setattr(names, "generate_bge_m3_hybrid_vectors", vectors)
    model = Mock()
    model.invoke.return_value = SimpleNamespace(content="投资书籍 " + PRIVATE)
    monkeypatch.setattr(names.AIClients, "get_llm_client", lambda *args: model)

    def parse(self, state):
        directory = Path(state["file_dir"])
        directories.append(directory)
        (directory / "private.png").write_bytes(b"private-pixels")
        parsed = directory / "parsed.md"
        parsed.write_text(CONTENT, encoding="utf-8")
        return {**state, "md_path": str(parsed)}

    monkeypatch.setattr(PdfToMdNode, "process", parse)
    with TestClient(create_app()) as client:
        yield SimpleNamespace(client=client, stored=stored, embedded=embedded, directories=directories,
                              storage=storage, minio=minio, model=model)


@pytest.mark.parametrize("suffix", ["md", "markdown", "txt", "pdf"])
def test_public_api_redacts_before_models_and_storage_and_removes_originals(public_import, suffix, caplog):
    env = public_import
    data = b"%PDF-fixture" if suffix == "pdf" else CONTENT.encode()
    response = env.client.post("/upload", files={"file": (f"../../姓名_王小明_13812345678.{suffix}", data)})
    assert response.status_code == 200
    status = env.client.get("/status/" + response.json()["task_id"]).json()
    assert status["status"] == "completed", status
    assert "公共文档脱敏" in status["done_list"]
    assert "Markdown图片处理" not in status["done_list"]
    encoded = json.dumps([env.stored, env.embedded, status], ensure_ascii=False) + caplog.text
    assert env.stored and env.embedded
    assert not any(secret in encoded for secret in SECRETS)
    assert "images/private.png" not in encoded
    assert any("12.5" in item for item in env.embedded)
    assert all(row["owner_id"] == "public" and row["doc_type"] == "knowledge" for row in env.stored)
    assert all(not directory.exists() for directory in env.directories)
    env.minio.assert_not_called()
    prompt = str(env.model.invoke.call_args)
    assert not any(secret in prompt for secret in SECRETS)


def test_public_text_is_safe_before_first_write_and_filename_is_not_a_path(public_import):
    service = FileProcessService()
    path, directory, task_id = service.process_upload_file(UploadFile(file=io.BytesIO(CONTENT.encode()), filename="../../13812345678.md"))
    try:
        assert Path(path).name == "input.md"
        assert Path(path).parent == Path(directory)
        assert not any(secret in Path(path).read_text(encoding="utf-8") for secret in SECRETS)
        assert "13812345678" not in get_task_result(task_id, "document_title")
        service.run_main_graph(path, directory, task_id)
        assert get_task_info(task_id)["status"] == "completed"
        assert not Path(directory).exists()
    finally:
        if Path(directory).exists():
            service._cleanup(directory, task_id)


@pytest.mark.parametrize("failure", ["parse", "embedding", "storage"])
def test_public_failure_cleans_temp_and_does_not_echo_sensitive_exception(public_import, monkeypatch, caplog, failure):
    env = public_import
    from knowledge.processor.import_processor.nodes import bge_embedding_chunks_node as embedding
    secret_error = RuntimeError(PRIVATE)
    if failure == "parse":
        monkeypatch.setattr(PdfToMdNode, "process", Mock(side_effect=secret_error))
    elif failure == "embedding":
        monkeypatch.setattr(embedding, "generate_bge_m3_hybrid_vectors", Mock(side_effect=secret_error))
    else:
        env.storage.insert.side_effect = secret_error
    service = FileProcessService()
    path, directory, task_id = service.process_upload_file(UploadFile(file=io.BytesIO(b"%PDF-fixture"), filename="test.pdf"))
    service.run_main_graph(path, directory, task_id)
    assert not Path(directory).exists()
    status = env.client.get("/status/" + task_id).json()
    assert status["status"] == "failed" and status["error"]
    assert not any(secret in json.dumps(status) + caplog.text for secret in SECRETS)


@pytest.mark.parametrize("filename,data", [("empty.md", b""), ("fake.pdf", b"not a pdf"), ("file.exe", b"text"), ("bad.md", b"\xff")])
def test_public_invalid_input_never_reports_accepted(public_import, filename, data):
    response = public_import.client.post("/upload", files={"file": (filename, data)})
    assert response.status_code == 422
    assert not public_import.stored


def test_public_cleanup_refuses_an_unrelated_directory(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError):
        FileProcessService._cleanup(tmp_path, "not-a-task")
    assert marker.exists()


def test_public_invalid_multipart_does_not_echo_input(public_import):
    response = public_import.client.post("/upload", data={"file": PRIVATE})
    assert response.status_code == 422
    assert not any(secret in response.text for secret in SECRETS)

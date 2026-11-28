"""查询运行的装配、应用隔离与资源归属；全部使用内存存储。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
import asyncio
import json
import socket
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import mongomock
import pytest
from fastapi.testclient import TestClient

from knowledge.runtime import QueryRuntime, RuntimeConfig


class MemoryMongo:
    def __init__(self, backend=None, events=None):
        self.backend = backend if backend is not None else mongomock.MongoClient(tz_aware=True)
        self.events = events if events is not None else []
        self.closed = 0

    def __getitem__(self, database):
        assert not self.closed, "关闭后仍在使用 MongoDB 客户端"
        return MemoryDatabase(self.backend[database])

    def close(self):
        self.closed += 1
        self.events.append("mongo_close")


class MemoryDatabase:
    def __init__(self, database):
        self.database = database

    def __getitem__(self, name):
        return self.database[name]

    def list_collection_names(self, filter=None):
        # mongomock 4.3 的 name 过滤会返回已删除的 collection；先读取真实存在项。
        names = self.database.list_collection_names()
        return [name for name in names if filter is None or name == filter["name"]]


class MemoryMilvus:
    def __init__(self, names=(), events=None):
        self.names = set(names)
        self.events = events if events is not None else []
        self.dropped = []
        self.closed = 0

    def has_collection(self, name, **options):
        assert not self.closed, "关闭后仍在使用 Milvus 客户端"
        return name in self.names

    def drop_collection(self, name, **options):
        self.names.discard(name)
        self.dropped.append(name)
        self.events.append("drop:" + name)

    def close(self):
        self.closed += 1
        self.events.append("milvus_close")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://runtime-secret@invalid.example/business")
    monkeypatch.setenv("MONGO_DB_NAME", "business")
    monkeypatch.setenv("AFK_TEST_DB_NAME", "runtime_tests")
    monkeypatch.setenv("MILVUS_URL", "http://runtime-secret@invalid.example:19530")
    monkeypatch.setenv("CHUNKS_COLLECTION", "shared_knowledge")
    monkeypatch.setenv("ITEM_NAME_COLLECTION", "shared_items")
    monkeypatch.setenv("DAILY_SUMMARY_ENABLED", "0")
    monkeypatch.setenv("MARKET_COLLECTION_ENABLED", "0")

    original_pair = socket.socketpair
    pair_state = threading.local()

    def socketpair(*args, **kwargs):
        # Windows asyncio 用本机 socketpair 唤醒事件循环，允许这次内部连接。
        pair_state.building = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            pair_state.building = False

    def guard_connect(original):
        def connect(*args, **kwargs):
            if getattr(pair_state, "building", False):
                return original(*args, **kwargs)
            raise AssertionError("运行时回归测试禁止访问网络")
        return connect

    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", guard_connect(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect(socket.socket.connect_ex))


@pytest.fixture
def runtime_factory():
    runtimes = []

    def build(profile="investment", prefix=None, *, backend=None, shared_public=False):
        config = RuntimeConfig.acceptance(profile, prefix or "runtime_" + str(len(runtimes)),
                                          shared_public=shared_public, vector_dimension=2)
        mongo, milvus = MemoryMongo(backend), MemoryMilvus()
        runtime = QueryRuntime(config, mongo_factory=Mock(return_value=mongo),
                               milvus_factory=Mock(return_value=milvus))
        runtimes.append(runtime)
        return runtime, mongo, milvus

    yield build
    for runtime in reversed(runtimes):
        runtime.close()


def empty_retrieval(runtime):
    """保留真实检索、投资图和档案装配，仅替换离线向量计算。"""
    runtime.retriever.embed = lambda texts: {"dense": [[1.0, 0.0] for _ in texts],
                                            "sparse": [{1: 1.0} for _ in texts]}


def test_construction_manifest_and_unused_close_do_not_connect():
    mongo_factory, milvus_factory = Mock(), Mock()
    runtime = QueryRuntime(RuntimeConfig.acceptance("stock", "lazy"),
                           mongo_factory=mongo_factory, milvus_factory=milvus_factory)
    manifest = runtime.manifest()
    assert manifest["mongo"] == manifest["vectors"] == []
    assert "runtime-secret" not in json.dumps(manifest)
    assert all(runtime.close().values())
    mongo_factory.assert_not_called()
    milvus_factory.assert_not_called()


def test_closing_one_runtime_preserves_other_milvus_connections(monkeypatch):
    connections = {}

    class SharedClient:
        def __init__(self, uri, *, alias=None, **kwargs):
            self.alias = alias or uri
            connections[self.alias] = True

        def has_collection(self, name, **kwargs):
            assert connections[self.alias], "本轮关闭了其他客户端的底层连接"
            return True

        def close(self):
            connections[self.alias] = False

    monkeypatch.setattr("pymilvus.MilvusClient", SharedClient)
    config = RuntimeConfig.from_env()
    unrelated = SharedClient(config.milvus_url)
    left, right = QueryRuntime(config), QueryRuntime(config)
    try:
        assert left.milvus.has_collection("public")
        assert right.milvus.has_collection("public")
        assert all(left.close().values())
        assert right.milvus.has_collection("public")
        assert unrelated.has_collection("public")
    finally:
        left.close()
        right.close()
        unrelated.close()


@pytest.mark.parametrize(("profile", "requests"), [
    ("stock", [("GET", "/daily-summaries"), ("POST", "/daily-summaries/generate"),
               ("GET", "/reviews"), ("POST", "/reviews"), ("GET", "/strategies"),
               ("POST", "/strategies/text"), ("GET", "/investor-settings"),
               ("PUT", "/investor-settings"), ("GET", "/market/collection/latest"),
               ("POST", "/market/collection/run")]),
    ("summary", [("PUT", "/investor-settings"), ("GET", "/investor-settings/documents"),
                 ("GET", "/market/collection/latest"), ("POST", "/market/collection/run")]),
    ("investment", [("GET", "/market/collection/latest"), ("POST", "/market/collection/run")]),
])
def test_unavailable_routes_are_404_without_storage(profile, requests):
    from knowledge.api.query_api import create_app

    mongo_factory, milvus_factory = Mock(), Mock()
    runtime = QueryRuntime(RuntimeConfig.acceptance(profile, "disabled_routes"),
                           mongo_factory=mongo_factory, milvus_factory=milvus_factory)
    try:
        with TestClient(create_app(runtime=runtime, summary_enabled=False)) as api:
            for method, path in requests:
                response = api.request(method, path, json={})
                assert response.status_code == 404, (method, path, response.text)
        mongo_factory.assert_not_called()
        milvus_factory.assert_not_called()
    finally:
        runtime.close()


def test_two_apps_isolate_same_session_settings_and_task_streams(runtime_factory):
    from knowledge.api.query_api import create_app
    from knowledge.utils.task_util import get_task_result

    backend = mongomock.MongoClient(tz_aware=True)
    left, left_mongo, _ = runtime_factory(prefix="left", backend=backend)
    right, right_mongo, _ = runtime_factory(prefix="right", backend=backend)
    empty_retrieval(left)
    empty_retrieval(right)
    left_app, right_app = create_app(runtime=left), create_app(runtime=right)
    with TestClient(left_app) as one, TestClient(right_app) as two:
        saved = one.put("/investor-settings", json={"revision": 0, "profile": {"notes": "青鹭偏好"}})
        assert saved.status_code == 200
        assert two.get("/investor-settings").json()["revision"] == 0
        assert two.put("/investor-settings", json={"revision": 0, "profile": {"notes": "白鹭偏好"}}).status_code == 200
        assert one.get("/investor-settings").json()["profile"]["notes"] == "青鹭偏好"

        first = one.post("/query", json={"query": "我的策略", "session_id": "same-session", "is_stream": True})
        assert first.status_code == 200
        task_id = first.json()["task_id"]
        assert left.owns_task(task_id) and not right.owns_task(task_id)
        assert two.get("/status/" + task_id).status_code == 404
        assert two.get("/stream/" + task_id).status_code == 404
        assert two.get("/history/same-session").json()["items"] == []
        stream = one.get("/stream/" + task_id)
        assert stream.status_code == 200 and "event: final" in stream.text
        answer = one.get("/status/" + task_id).json()["answer"]
        assert "青鹭偏好" in answer and "白鹭偏好" not in answer

        second = two.post("/query", json={"query": "我的策略", "session_id": "same-session", "is_stream": True})
        second_task = second.json()["task_id"]
        assert one.get("/status/" + second_task).status_code == 404
        assert one.get("/stream/" + second_task).status_code == 404
        second_answer = two.get("/status/" + second_task).json()["answer"]
        assert "白鹭偏好" in second_answer and "青鹭偏好" not in second_answer
        assert two.delete("/history/same-session").json()["deleted_count"] == 2
        assert len(one.get("/history/same-session").json()["items"]) == 2
        assert two.get("/history/same-session").json()["items"] == []

    assert left_mongo.closed == right_mongo.closed == 0
    with TestClient(left_app) as api:
        assert len(api.get("/history/same-session").json()["items"]) == 2
    assert all(left.close().values())
    assert get_task_result(task_id, "answer", None) is None
    assert get_task_result(second_task, "answer") == second_answer
    assert right.settings.read()["profile"]["notes"] == "白鹭偏好"


def test_index_chat_uses_runtime_indices_without_default_summary(runtime_factory, monkeypatch):
    from knowledge.api import summary_api
    from knowledge.api.query_api import create_app
    from knowledge.summary.indices import INDICES, IndexQuote

    runtime, mongo, _ = runtime_factory("stock", "index_chat")
    empty_retrieval(runtime)
    fallback = Mock(side_effect=AssertionError("不应触碰默认摘要运行"))
    monkeypatch.setattr(summary_api, "get_summary_service", fallback)

    def quotes(symbol, start, end, now):
        return [IndexQuote(symbol=symbol, name=INDICES[symbol][1], date=end,
                           close=3200, change_pct=0.1, volume=1000, amount=10000,
                           source="eastmoney", updated_at=now, raw={})]

    adapter = SimpleNamespace(name="eastmoney", fetch_daily=Mock(side_effect=quotes))
    runtime.indices.adapters = [adapter]
    with TestClient(create_app(runtime=runtime)) as api:
        response = api.post("/query", json={"query": "大盘风险如何", "is_stream": False})
        assert response.status_code == 200 and not response.json()["error"]
    assert adapter.fetch_daily.call_count == 2
    assert {row["symbol"] for row in runtime.collection("indices").find()} == set(INDICES)
    assert "index_quotes" not in mongo.backend[runtime.config.database].list_collection_names()
    fallback.assert_not_called()


def test_cleanup_deletes_only_owned_resources_and_preserves_borrowed_public(runtime_factory):
    runtime, mongo, milvus = runtime_factory(prefix="owned", shared_public=True)
    database = mongo.backend[runtime.config.database]
    database.foreign_sentinel.insert_one({"value": "keep"})
    sibling = "owned_someone_else"
    database[sibling].insert_one({"value": "keep"})
    milvus.names.update({"shared_knowledge", "shared_items", "foreign_vectors", sibling})
    runtime.history.insert_one({"session_id": "test"})
    assert runtime.vector_name("knowledge") == "shared_knowledge"
    assert runtime.vector_name("items") == "shared_items"
    own_vectors = runtime.vector_name("personal")
    milvus.names.add(own_vectors)
    manifest = runtime.manifest()
    assert manifest["vectors"] == [own_vectors]
    assert set(manifest["borrowed_vectors"]) == {"shared_knowledge", "shared_items"}
    result = runtime.close()
    assert all(result.values())
    assert set(database.list_collection_names()) == {"foreign_sentinel", sibling}
    assert milvus.names == {"shared_knowledge", "shared_items", "foreign_vectors", sibling}
    assert milvus.dropped == [own_vectors]
    assert mongo.closed == milvus.closed == 1
    assert runtime.close() == result
    assert mongo.closed == milvus.closed == 1


def test_legacy_graph_uses_runtime_history_and_vector_client(runtime_factory, monkeypatch):
    import numpy as np
    from scipy.sparse import csr_array
    from pymilvus import DataType
    from knowledge.utils.clients.ai_clients import AIClients
    from knowledge.utils.clients.storage_clients import StorageClients

    fallback = Mock(side_effect=AssertionError("旧图不得借用全局存储连接"))
    monkeypatch.setattr(StorageClients, "get_mongo_client", fallback)
    monkeypatch.setattr(StorageClients, "get_milvus_client", fallback)
    embedding = SimpleNamespace(encode_documents=lambda texts: {
        "dense": np.array([[1.0, 0.0] for _ in texts]),
        "sparse": csr_array([[1.0, 0.0] for _ in texts]),
    })
    monkeypatch.setattr(AIClients, "get_bge_m3_client", lambda: embedding)
    monkeypatch.setattr(AIClients, "get_bge_m3_rerank_client", lambda: SimpleNamespace(compute_score=lambda pairs: [2.0] * len(pairs)))
    monkeypatch.setattr(AIClients, "get_llm_client", lambda **options: SimpleNamespace(invoke=lambda prompt: SimpleNamespace(content="模型输出")))

    for prefix, message in (("graph_left", "青鹭历史"), ("graph_right", "白鹭历史")):
        runtime, _, milvus = runtime_factory(prefix=prefix)
        runtime.history.insert_one({"session_id": "same-session", "role": "user", "text": message, "ts": 1})
        milvus.describe_collection = Mock(return_value={"fields": [
            {"name": name, "type": DataType.VARCHAR} for name in ("owner_id", "doc_type", "document_id")
        ]})
        milvus.hybrid_search = Mock(return_value=[[{"id": 731, "entity": {"title": "自建知识", "content": prefix}}]])
        task_id = prefix + "_query"
        runtime.register_task(task_id)
        result = runtime.query.graph_factory().invoke({"task_id": task_id, "session_id": "same-session",
            "original_query": "知识说明", "knowledge_query": True, "is_stream": False})
        assert result["answer"] == "模型输出"
        assert [row["text"] for row in result["history"]] == [message]
        assert result["reranked_docs"][0]["content"] == prefix
        assert milvus.hybrid_search.call_count == 2  # 向量与 HyDE 两条真实图路径。
        for call in milvus.hybrid_search.call_args_list:
            assert call.kwargs["collection_name"] == runtime.config.vector_names["knowledge"]
            assert all('owner_id == "public"' in request.expr for request in call.kwargs["reqs"])
    fallback.assert_not_called()


def test_slow_storage_initialization_does_not_block_disabled_routes(runtime_factory):
    from knowledge.api.query_api import create_app

    runtime, mongo, _ = runtime_factory("summary", "slow_storage")
    entered, release = threading.Event(), threading.Event()

    def connect():
        entered.set()
        assert release.wait(5)
        return mongo

    runtime._mongo_factory.side_effect = connect
    with TestClient(create_app(runtime=runtime)) as api, ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(api.get, "/watchlist")
        try:
            assert entered.wait(5)
            second = executor.submit(api.get, "/market/collection/latest")
            assert second.result(timeout=2).status_code == 404
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200


def test_query_initialization_error_is_private_and_retryable(runtime_factory):
    from knowledge.api.query_api import create_app

    runtime, mongo, _ = runtime_factory("stock", "query_retry")
    runtime._mongo_factory.side_effect = [RuntimeError("runtime-secret"), mongo]
    with TestClient(create_app(runtime=runtime)) as api:
        failed = api.get("/history/session")
        assert failed.status_code == 503 and "runtime-secret" not in failed.text
        assert api.get("/history/session").json()["items"] == []


def test_resource_name_collisions_never_claim_or_delete_foreign_data(runtime_factory):
    runtime, mongo, milvus = runtime_factory(prefix="collision")
    database = mongo.backend[runtime.config.database]
    history_name = runtime.config.mongo_names["history"]
    vector_name = runtime.config.vector_names["personal"]
    database[history_name].insert_one({"sentinel": "another run"})
    milvus.names.add(vector_name)
    with pytest.raises(ValueError):
        runtime.collection("history")
    with pytest.raises(ValueError):
        runtime.vector_name("personal")
    assert runtime.manifest()["mongo"] == runtime.manifest()["vectors"] == []
    assert all(runtime.close().values())
    assert database[history_name].find_one()["sentinel"] == "another run"
    assert vector_name in milvus.names and not milvus.dropped


def test_production_close_never_deletes_existing_data():
    config = RuntimeConfig.from_env()
    mongo, milvus = MemoryMongo(), MemoryMilvus([config.vector_names["personal"]])
    runtime = QueryRuntime(config, mongo_factory=lambda: mongo, milvus_factory=lambda: milvus)
    database = mongo.backend[config.database]
    runtime.history.insert_one({"sentinel": "production"})
    runtime.vectors("personal")
    assert all(runtime.close().values())
    assert database[config.mongo_names["history"]].find_one()["sentinel"] == "production"
    assert config.vector_names["personal"] in milvus.names and not milvus.dropped
    assert mongo.closed == milvus.closed == 1


def test_app_created_runtime_is_closed_at_lifespan_exit(monkeypatch):
    from knowledge import runtime as runtime_module
    from knowledge.api.query_api import create_app

    mongo = MemoryMongo()
    runtime = QueryRuntime(RuntimeConfig.from_env(), mongo_factory=lambda: mongo)
    factory = Mock(return_value=runtime)
    monkeypatch.setattr(runtime_module, "QueryRuntime", factory)
    with TestClient(create_app(summary_enabled=False)) as api:
        response = api.get("/investor-settings")
        assert response.status_code == 200 and response.json()["revision"] == 0
        assert mongo.closed == 0
    factory.assert_called_once()
    assert mongo.closed == 1
    with pytest.raises(RuntimeError, match="关闭"):
        runtime.settings


def test_failed_connection_can_retry_and_close_without_leaking_resources():
    mongo = MemoryMongo()
    factory = Mock(side_effect=[RuntimeError("private-connection-error"), mongo])
    runtime = QueryRuntime(RuntimeConfig.acceptance("stock", "retry"), mongo_factory=factory)
    try:
        with pytest.raises(RuntimeError):
            runtime.history
        assert runtime.manifest()["mongo"] == []
        runtime.history.insert_one({"recovered": True})
        assert factory.call_count == 2
        assert all(runtime.close().values())
        assert not mongo.backend[runtime.config.database].list_collection_names()
        assert mongo.closed == 1
    finally:
        runtime.close()


def test_partial_summary_initialization_retries_and_cleans_claimed_resources(runtime_factory, monkeypatch):
    from knowledge.service import summary_service

    runtime, mongo, milvus = runtime_factory("summary", "partial")
    constructor = summary_service.SummaryService
    attempts = Mock(side_effect=[RuntimeError("partial-build"), None])

    def fail_once(*args, **kwargs):
        attempts()
        return constructor(*args, **kwargs)

    monkeypatch.setattr(summary_service, "SummaryService", fail_once)
    with pytest.raises(RuntimeError, match="partial-build"):
        runtime.summary
    assert runtime.manifest()["mongo"]
    assert runtime.summary is runtime.summary
    assert attempts.call_count == 2
    assert milvus.closed == 0
    assert all(runtime.close().values())
    assert not mongo.backend[runtime.config.database].list_collection_names()


def test_live_worker_prevents_deletion_and_client_close_until_join_succeeds(runtime_factory):
    runtime, mongo, milvus = runtime_factory(prefix="worker")
    runtime.history.insert_one({"retained": True})
    vectors = runtime.vector_name("personal")
    milvus.names.add(vectors)
    job = runtime.collection_job
    events = []
    mongo.events = milvus.events = events
    worker = SimpleNamespace(alive=True)
    worker.is_alive = lambda: worker.alive

    def join(timeout=None):
        assert job.stop.is_set()
        events.append("worker_join")

    worker.join = join
    job.worker = worker
    assert runtime.close() == {"workers_stopped": False}
    assert mongo.closed == milvus.closed == 0
    assert runtime.history.find_one()["retained"] is True
    assert milvus.names == {vectors} and not milvus.dropped
    worker.alive = False
    assert all(runtime.close().values())
    assert events.index("worker_join") < events.index("drop:" + vectors)
    assert events.index("drop:" + vectors) < events.index("milvus_close")


def test_scheduler_finishes_before_worker_join_and_resource_cleanup(runtime_factory):
    runtime, mongo, milvus = runtime_factory(prefix="shutdown_order")
    runtime.history.insert_one({"retained": True})
    vector_name = runtime.vector_name("personal")
    milvus.names.add(vector_name)
    events = []
    mongo.events = milvus.events = events
    job = runtime.collection_job
    scheduler = SimpleNamespace(running=True)
    worker = SimpleNamespace(alive=True)
    worker.is_alive = lambda: worker.alive

    def shutdown(wait):
        assert wait is True and job.stop.is_set()
        assert runtime.history.find_one()["retained"] is True
        assert mongo.closed == milvus.closed == 0
        events.append("scheduler_finished")
        scheduler.running = False

    def join(timeout=None):
        assert scheduler.running is False
        assert runtime.history.find_one()["retained"] is True
        events.append("worker_finished")
        worker.alive = False

    scheduler.shutdown = shutdown
    worker.join = join
    job.worker = worker
    runtime.track_scheduler(scheduler)
    assert all(runtime.close().values())
    assert events[:3] == ["scheduler_finished", "worker_finished", "drop:" + vector_name]
    assert mongo.closed == milvus.closed == 1


def test_active_request_preserves_resources_and_can_resume_after_timeout(runtime_factory, monkeypatch):
    from knowledge.api.query_api import create_app

    runtime, mongo, milvus = runtime_factory(prefix="active_request")
    empty_retrieval(runtime)
    runtime.history.insert_one({"session_id": "keep", "text": "existing"})
    entered, release = threading.Event(), threading.Event()

    def blocked_answer(query, market_context=None):
        entered.set()
        assert release.wait(5)
        return {"answer": "工作线程完成后保存", "reranked_docs": []}

    monkeypatch.setattr(runtime.investment, "answer", blocked_answer)
    original_stop = runtime.stop
    monkeypatch.setattr(runtime, "stop", lambda: original_stop(timeout=0.02))
    app = create_app(runtime=runtime)
    with TestClient(app) as api, ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(api.post, "/query", json={"query": "我的策略",
                                  "session_id": "active", "is_stream": False})
        try:
            assert entered.wait(5)
            assert runtime.close() == {"workers_stopped": False}
            assert mongo.closed == milvus.closed == 0
            assert runtime.history.find_one({"session_id": "keep"})["text"] == "existing"
            assert api.get("/history/active").status_code == 503
        finally:
            release.set()
        response = pending.result(timeout=5)
        assert response.status_code == 200 and not response.json()["error"]
    with TestClient(app) as api:
        messages = api.get("/history/active").json()["items"]
        assert len(messages) == 2 and "工作线程完成后保存" in messages[-1]["text"]
    assert all(runtime.close().values())
    assert not mongo.backend[runtime.config.database].list_collection_names()


def test_cancelled_await_does_not_release_live_executor_task(runtime_factory, monkeypatch):
    runtime, mongo, _ = runtime_factory(prefix="cancelled_await")
    entered, release = threading.Event(), threading.Event()
    runtime.history.insert_one({"sentinel": "keep while writing"})
    original_stop = runtime.stop
    monkeypatch.setattr(runtime, "stop", lambda: original_stop(timeout=0.02))

    def write_after_release():
        entered.set()
        assert release.wait(5)
        runtime.history.insert_one({"finished": True})

    with ThreadPoolExecutor(max_workers=1) as executor:
        async def cancelled_request():
            future = asyncio.get_running_loop().run_in_executor(executor, runtime.run_task, write_after_release)
            assert entered.wait(5)
            future.cancel()
            with pytest.raises(asyncio.CancelledError):
                await future
            assert runtime.close() == {"workers_stopped": False}
            assert mongo.closed == 0 and runtime.history.find_one({"sentinel": {"$exists": True}})

        try:
            asyncio.run(cancelled_request())
        finally:
            release.set()
    assert runtime.history.find_one({"finished": True})
    assert all(runtime.close().values())
    assert mongo.closed == 1


def test_second_scheduler_start_failure_stops_first_scheduler(monkeypatch):
    from knowledge.api.query_api import create_app
    from knowledge.summary import scheduler as scheduler_module

    config = replace(RuntimeConfig.from_env(), summary_enabled=True, collection_enabled=True)
    mongo_factory, milvus_factory = Mock(), Mock()
    runtime = QueryRuntime(config, mongo_factory=mongo_factory, milvus_factory=milvus_factory)
    first = SimpleNamespace(running=False)
    first.start = Mock(side_effect=lambda: setattr(first, "running", True))
    first.shutdown = Mock(side_effect=lambda wait: setattr(first, "running", False))
    second = Mock()
    second.start.side_effect = RuntimeError("second scheduler failed")
    monkeypatch.setattr(scheduler_module, "build_scheduler", Mock(return_value=first))
    monkeypatch.setattr(scheduler_module, "BackgroundScheduler", Mock(return_value=second))
    try:
        with pytest.raises(RuntimeError, match="second scheduler failed"):
            with TestClient(create_app(runtime=runtime)):
                pass
        first.start.assert_called_once_with()
        first.shutdown.assert_called_once_with(wait=True)
        assert first.running is False
        mongo_factory.assert_not_called()
        milvus_factory.assert_not_called()
    finally:
        runtime.close()


def test_concurrent_first_summary_access_shares_service_and_generation_slot(runtime_factory, monkeypatch):
    from knowledge.api.query_api import create_app
    from knowledge.api.summary_api import get_summary_service
    from knowledge.market.models import SHANGHAI
    from knowledge.service import summary_service

    runtime, mongo, _ = runtime_factory("summary", "concurrent")
    app = create_app(runtime=runtime)
    constructor = summary_service.SummaryService
    entered, release = threading.Event(), threading.Event()
    attempts = []

    def blocked_constructor(*args, **kwargs):
        attempts.append(True)
        entered.set()
        assert release.wait(5)
        return constructor(*args, **kwargs)

    monkeypatch.setattr(summary_service, "SummaryService", blocked_constructor)
    barrier = threading.Barrier(9)

    def obtain():
        barrier.wait(timeout=5)
        return get_summary_service(SimpleNamespace(app=app))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(obtain) for _ in range(8)]
        try:
            barrier.wait(timeout=5)
            assert entered.wait(5)
        finally:
            release.set()
        services = [future.result(timeout=5) for future in futures]
    assert len(attempts) == 1
    assert all(service is services[0] for service in services)
    service = services[0]
    day = date(2026, 11, 27)
    service.now = lambda: datetime(2026, 11, 27, 17, tzinfo=SHANGHAI)
    claim, record = service.begin(day)
    assert claim and record["status"] == "processing"
    try:
        assert services[-1].begin(day)[0] is None
        with pytest.raises(summary_service.SummaryBusy):
            services[-1].begin(day - timedelta(days=1))
    finally:
        service.slot.release()
    assert mongo.closed == 0


@pytest.mark.parametrize("database", ["business", "BUSINESS", "kb001", "admin", "local", "config"])
def test_acceptance_rejects_business_and_reserved_databases(database, monkeypatch):
    monkeypatch.setenv("AFK_TEST_DB_NAME", database)
    with pytest.raises(ValueError, match="AFK_TEST_DB_NAME"):
        RuntimeConfig.acceptance("stock", "safe_prefix")

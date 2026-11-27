"""#7 独立验收：默认真实存储探针；--host 使用已有模型完成全链路。

必须显式设置 AFK_TEST_DB_NAME，且不能与业务库相同。不安装依赖或下载模型。
--serve 在全链路检查通过后提供浏览器入口；创建 --stop-file 指定文件可安全退出。
默认只向 stdout 输出脱敏报告；--report 可另存至一个尚不存在的 JSON 文件。
"""

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
KINDS = ("personal", "knowledge", "review")
OWNER = "default_user"
FOREIGN_OWNER = "investment_accept_foreign"
PHONE = "13900001234"
FOREIGN_MARKER = "紫鹭8642"
FAILURE_MARKER = "acceptance-injected-private-error"
SAMPLES = {
    "personal": (
        "若股价连续上涨且没有完成回踩确认，先执行青鹭7391核对法：回看17个交易日，"
        "再核对自己写下的仓位上限。熊市轻仓只适用于已经人工核实市场条件的情况，"
        "当前阶段不能由此推断。"
    ),
    "knowledge": (
        "自建投资知识：松柏6284练习要求在估值前核对现金流、债务、盈利质量三类依据。"
        "历史收益不能证明未来收益。本段为验收材料，不代表真实投资建议。"
    ),
    "review": (
        "今天没有成交。我计划在12.5元买入000001，但尚未提交订单。复盘口令是白鹭9527，"
        "我的教训是先等待回踩，不因连续上涨而追价。姓名：张小甲，手机号13900001234。"
    ),
}
FACTS = {
    "personal": ("青鹭7391", "17个交易日", "回踩", "仓位上限"),
    "knowledge": ("松柏6284", "现金流", "债务", "盈利质量"),
    "review": ("白鹭9527", "计划", "尚未提交订单", "没有成交"),
}
LABELS = {"personal": "个人策略", "knowledge": "公共知识", "review": "复盘记录"}
CHECK_HINTS = {"personal": "核对个人策略", "knowledge": "公共知识核对", "review": "对照过往复盘"}
COMBINED_QUERY = (
    "结合我的个人策略、公共知识和复盘，列出追高前需要核对的条件、估值依据与交易状态。"
    "请逐项保留三类资料中的独特口令、回看窗口、完整前提和未成交信息。"
)


class AcceptanceFailure(RuntimeError):
    pass


def source_key(document):
    return (document["source"], document["collection"],
            document.get("review_id") or document["document_id"], str(document["chunk_id"]))


def selection_key(document):
    key = source_key(document)
    return key[:3] if document["source"] == "review" else key


def fail_dependency(*args, **kwargs):
    raise RuntimeError(FAILURE_MARKER + ": mongodb://synthetic:secret@invalid/ " + PHONE)


class FailedSelector:
    def select(self, query, documents, market):
        return fail_dependency()


class FailedExtractor:
    def extract(self, text):
        return fail_dependency()


class StorageProbeTokenizer:
    def encode(self, text, **kwargs):
        return list(text)


class ForgedSelector:
    def select(self, query, documents, market):
        return {"selections": [{"reference": "E999999", "quote": "伪造事实" + PHONE,
                                "evidence_ids": ["stock:forged"]}]}


class ObservedSelector:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = []

    def select(self, query, documents, market):
        call = {"documents": deepcopy(documents), "market": deepcopy(market), "result": None}
        self.calls.append(call)
        call["result"] = self.delegate.select(query, documents, market)
        return call["result"]


class ObservedReranker:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = []

    def __call__(self, query, documents):
        result, status = self.delegate(query, documents)
        self.calls.append({"input": deepcopy(documents), "output": deepcopy(result), "status": status})
        return result, status


class ObservedAssistant:
    def __init__(self, delegate):
        self.delegate = delegate
        self.states = []

    def answer(self, query, market_context=None):
        state = self.delegate.answer(query, market_context=market_context)
        self.states.append(state)
        return state


class ObservedRisk:
    def __init__(self, delegate):
        self.delegate = delegate
        self.answers = []

    def answer(self, security):
        answer = self.delegate.answer(security)
        self.answers.append({"code": security.code, "text": answer})
        return answer


class ScopedMilvus:
    METHODS = {
        "has_collection", "describe_collection", "create_collection", "drop_collection",
        "load_collection", "release_collection", "get_collection_stats", "flush", "query",
        "search", "hybrid_search", "insert", "upsert", "delete",
    }

    def __init__(self, client, collections):
        self.client = client
        self.allowed = set(collections)
        self.searches = []
        self.failed = set()
        self.corrupt_owner = set()

    def __getattr__(self, name):
        action = getattr(self.client, name)
        if name not in self.METHODS:
            return action

        def scoped(*args, **kwargs):
            collection = kwargs.get("collection_name") or (args[0] if args else None)
            if collection not in self.allowed:
                raise AcceptanceFailure("unexpected_vector_collection")
            if name == "hybrid_search":
                requests = kwargs.get("reqs", args[1] if len(args) > 1 else [])
                self.searches.append({"collection": collection,
                                      "expressions": [request.expr for request in requests]})
                if collection in self.failed:
                    return fail_dependency()
            result = action(*args, **kwargs)
            if name == "hybrid_search" and collection in self.corrupt_owner:
                result = [[dict(hit) for hit in hits] for hits in result]
                for hits in result:
                    for hit in hits:
                        hit["entity"] = {**hit["entity"], "owner_id": FOREIGN_OWNER}
            return result

        return scoped


def parse_sse(text):
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        event_name, data = None, []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if event_name is not None and data:
            events.append((event_name, json.loads("\n".join(data))))
    return events


def isolated_environment(test_db, host, prefix):
    business_names = {os.getenv("MONGO_DB_NAME", "kb001"), "kb001", "admin", "local", "config"}
    if host:
        from dotenv import dotenv_values, load_dotenv
        config_path = ROOT / "knowledge/.env"
        business_names.add(dotenv_values(config_path).get("MONGO_DB_NAME"))
        load_dotenv(config_path)
        business_names.add(os.getenv("MONGO_DB_NAME"))
    uri_database = unquote(urlsplit(os.getenv("MONGO_URL", "")).path.lstrip("/").split("/")[0])
    business_names.add(uri_database)
    if (not test_db or test_db.casefold() in {name.casefold() for name in business_names if name}
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", test_db)):
        raise AcceptanceFailure("explicit_independent_AFK_TEST_DB_NAME_required")
    if not os.getenv("MONGO_URL") or not os.getenv("MILVUS_URL"):
        raise AcceptanceFailure("storage_configuration_required")
    return {
        "MONGO_DB_NAME": test_db,
        "DAILY_SUMMARY_ENABLED": "0",
        "STRATEGY_COLLECTION": prefix + "_personal",
        "CHUNKS_COLLECTION": prefix + "_knowledge",
        "REVIEW_COLLECTION": prefix + "_review",
        "ITEM_NAME_COLLECTION": prefix + "_items",
        "LEGACY_PUBLIC_COLLECTIONS": "",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "OMP_NUM_THREADS": "2",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


class Acceptance:
    def __init__(self, args, test_db, prefix, report):
        self.args, self.test_db, self.prefix, self.report = args, test_db, prefix, report
        self.mongo = self.raw_milvus = self.milvus = None
        self.mongo_names, self.vector_names = [], []
        self.collections = {kind: prefix + "_" + kind for kind in KINDS}
        self.names = {name: prefix + "_" + name for name in
                      ("documents", "reviews", "history", "stocks", "indices", "summaries", "watches")}
        self.stack = ExitStack()

    def check(self, name, result):
        self.report["checks"][name] = bool(result)
        print(name, "PASS" if result else "FAIL", flush=True)
        if not result:
            raise AcceptanceFailure(name)

    def safe(self, name, value):
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        self.check(name, all(fragment not in encoded for fragment in
                            (PHONE, "张小甲", FOREIGN_MARKER, FAILURE_MARKER,
                             "mongodb://", "synthetic:secret", "伪造事实")))

    def answer_policy(self, name, answer):
        from knowledge.utils.answer_policy import DISCLAIMER
        self.check(name + "_disclaimer", answer.endswith(DISCLAIMER) and answer.count(DISCLAIMER) == 1)
        self.safe(name + "_private_data", answer)

    def prepare(self):
        from pymongo import MongoClient
        from pymilvus import MilvusClient
        from knowledge.review.repository import ReviewRepository
        from knowledge.review.vectors import ReviewVectors
        from knowledge.strategy.repository import StrategyRepository
        from knowledge.strategy.vectors import StrategyVectors
        self.mongo = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000,
                                 connectTimeoutMS=5000, socketTimeoutMS=15000, tz_aware=True)
        self.db = self.mongo[self.test_db]
        self.check("mongodb_ping", self.db.command("ping")["ok"] == 1)
        for name in self.names.values():
            self.check("unique_mongo_" + name.removeprefix(self.prefix + "_"),
                       not self.db.list_collection_names(filter={"name": name}))
            self.mongo_names.append(name)
            self.db.create_collection(name)
        self.documents = self.db[self.names["documents"]]
        self.reviews = self.db[self.names["reviews"]]
        self.history = self.db[self.names["history"]]
        self.document_repo = StrategyRepository(self.documents)
        self.review_repo = ReviewRepository(self.reviews)
        self.raw_milvus = MilvusClient(uri=os.environ["MILVUS_URL"], timeout=15)
        self.milvus = ScopedMilvus(self.raw_milvus, [*self.collections.values(), self.prefix + "_items"])
        self.vectors = {
            "personal": StrategyVectors(self.milvus, self.collections["personal"]),
            "knowledge": StrategyVectors(self.milvus, self.collections["knowledge"]),
            "review": ReviewVectors(self.milvus, self.collections["review"]),
        }
        for kind, vectors in self.vectors.items():
            self.check("unique_vector_" + kind, not self.milvus.has_collection(vectors.collection))
            self.vector_names.append(vectors.collection)
            vectors.prepare()
            fields = {field["name"] for field in self.milvus.describe_collection(vectors.collection)["fields"]}
            self.check("scoped_schema_" + kind, {"owner_id", "doc_type", "document_id"}.issubset(fields))
        self.report["coverage"]["real_storage"] = True

    def storage_failure_probe(self):
        from knowledge.review.indexing import ReviewIndexer
        from knowledge.service.review_service import ReviewService
        identifier = str(uuid.uuid4())
        options = {} if self.args.host else {"tokenizer": StorageProbeTokenizer(), "max_tokens": 512}
        indexer = ReviewIndexer(self.review_repo, self.vectors["review"], embed=fail_dependency, **options)
        service = ReviewService(self.review_repo, extractor=FailedExtractor(), indexer=indexer)
        result = service.save(SAMPLES["review"], identifier)
        persisted = self.review_repo.get(identifier, OWNER)
        self.check("failed_models_preserve_original", result["index_status"] == "failed"
                   and persisted["extraction_status"] == "failed" and "白鹭9527" in persisted["original_text"])
        self.safe("failed_models_no_sensitive_storage", persisted)
        self.storage_probe_id = identifier

    def real_embedding(self, texts):
        from knowledge.review.indexing import embed_texts
        result = embed_texts(texts)
        self.check("real_bge_m3_vectors", len(result["dense"]) == len(texts)
                   and len(result["sparse"]) == len(texts)
                   and all(len(vector) == 1024 and all(math.isfinite(float(value)) for value in vector)
                           for vector in result["dense"]) and all(result["sparse"]))
        self.report["coverage"]["real_bge_m3"] = True
        return result

    def insert_text(self, kind, text, owner, document_id, doc_type=None):
        embedded = self.real_embedding([text])
        row = {"item_name": "", "title": "自建投资验收材料", "parent_title": "", "file_title": "验收材料",
               "content": text, "owner_id": owner, "doc_type": doc_type or kind, "document_id": document_id,
               "dense_vector": embedded["dense"][0], "sparse_vector": embedded["sparse"][0]}
        result = self.milvus.insert(self.collections[kind], [row], timeout=30)
        self.check("insert_" + document_id, result["insert_count"] == 1)

    def seed(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from knowledge.api.strategy_api import router as strategy_router, get_strategy_service
        from knowledge.api.review_api import router as review_router, get_review_service
        from knowledge.review.indexing import ReviewIndexer
        from knowledge.service.review_service import ReviewService
        from knowledge.service.strategy_service import StrategyService
        self.strategy_service = StrategyService(self.document_repo, self.vectors["personal"])
        self.indexer = ReviewIndexer(self.review_repo, self.vectors["review"])
        self.review_service = ReviewService(self.review_repo, indexer=self.indexer)
        fixture_app = FastAPI()
        fixture_app.include_router(strategy_router)
        fixture_app.include_router(review_router)
        fixture_app.dependency_overrides[get_strategy_service] = lambda: self.strategy_service
        fixture_app.dependency_overrides[get_review_service] = lambda: self.review_service
        self.review_id = str(uuid.uuid4())
        with TestClient(fixture_app) as api:
            response = api.post("/strategies/text", json={"title": "自建个人策略", "text": SAMPLES["personal"]})
            self.check("strategy_import_api", response.status_code == 202)
            self.personal_id = response.json()["task_id"]
            self.check("strategy_real_import_graph", api.get("/strategies/" + self.personal_id).json()["status"] == "completed")
            response = api.post("/reviews", json={"request_id": self.review_id, "text": SAMPLES["review"]})
            self.check("review_save_api", response.status_code == 201)
            record = response.json()
            self.check("review_real_extraction_and_index", record["extraction_status"] == "succeeded"
                       and record["index_status"] == "completed")
            events = record["extraction"]["events"]
            self.check("review_plan_not_executed", bool(events) and all(event["status"] != "executed" for event in events)
                       and any(event["status"] == "planned" and event["stock_code"] == "000001"
                               and event["price"] == 12.5 for event in events))
            self.safe("review_ingress_privacy", record)
            manifest = self.review_repo.get(self.review_id, OWNER)["index_manifest"]
            response = api.post("/reviews", json={"request_id": self.review_id, "text": SAMPLES["review"]})
            self.check("review_request_idempotent", response.status_code == 201
                       and self.reviews.count_documents({"_id": self.review_id}) == 1)
            api.post("/reviews/" + self.review_id + "/index")
            self.check("review_index_idempotent", self.review_repo.get(self.review_id, OWNER)["index_manifest"] == manifest)
            published = self.review_repo.get(self.review_id, OWNER)
            rows = self.milvus.query(self.collections["review"], filter=(
                self.vectors["review"].scope(OWNER, self.review_id)
                + " and index_version == " + json.dumps(published["index_version"])),
                output_fields=["id", "content", "content_hash"], limit=100, consistency_level="Strong")
            self.check("review_published_rows_exactly_once", len(rows) == len(manifest)
                       and {str(row["id"]) for row in rows} == set(manifest)
                       and all(hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
                               == row["content_hash"] == manifest[str(row["id"])] for row in rows))
            self.safe("review_vector_ingress_privacy", rows)
            response = api.post("/reviews", json={"request_id": str(uuid.uuid4()), "text": "自建资料",
                                                  "owner_id": FOREIGN_OWNER})
            self.check("client_owner_rejected", response.status_code == 422)
        self.knowledge_id = uuid.uuid4().hex
        self.insert_text("knowledge", SAMPLES["knowledge"], "public", self.knowledge_id)
        for kind, owner in (("personal", FOREIGN_OWNER), ("knowledge", FOREIGN_OWNER)):
            identifier = uuid.uuid4().hex
            if kind == "personal":
                self.document_repo.create(identifier, owner, "其他用户验收策略", "foreign.md")
                self.document_repo.finish(identifier, owner, "completed", chunk_count=1)
            self.insert_text(kind, "投资策略、估值和复盘：" + FOREIGN_MARKER + "。" + SAMPLES[kind], owner, identifier)
        self.insert_text("knowledge", "类型干扰：" + FOREIGN_MARKER, "public", uuid.uuid4().hex, doc_type="personal")
        self.pending_id = uuid.uuid4().hex
        self.document_repo.create(self.pending_id, OWNER, "尚未完成", "pending.md")
        self.insert_text("personal", "尚未发布的策略口令灰鹭保密。" + SAMPLES["personal"], OWNER, self.pending_id)
        self.foreign_review_id = str(uuid.uuid4())
        self.review_repo.create(self.foreign_review_id, FOREIGN_OWNER, "其他用户复盘：" + FOREIGN_MARKER)
        self.indexer.index(self.foreign_review_id, FOREIGN_OWNER)
        self.check("foreign_review_indexed", self.review_repo.get(self.foreign_review_id, FOREIGN_OWNER)["index_status"] == "completed")
        stale_text = "过期复盘版本灰鹭保密。今天没有成交。"
        self.vectors["review"].write([stale_text], self.real_embedding([stale_text]), OWNER,
                                     self.review_id, "unpublished-" + uuid.uuid4().hex)
        for collection in self.collections.values():
            self.milvus.flush(collection, timeout=30)
        self.report["coverage"]["real_review_index"] = True

    def retriever(self, **overrides):
        from knowledge.investment.retrieval import InvestmentRetriever
        return InvestmentRetriever(documents=self.documents, reviews=self.reviews,
                                   client=self.milvus, collections=self.collections, **overrides)

    def assert_routes(self, name, routes):
        self.check(name + "_three_routes", set(routes) == set(KINDS)
                   and all(routes[kind]["status"] == "retrieved" for kind in KINDS))
        expected_ids = {"personal": self.personal_id, "knowledge": self.knowledge_id, "review": self.review_id}
        for kind in KINDS:
            documents = routes[kind]["items"]
            self.check(name + "_" + kind + "_trace", bool(documents) and all(
                document["source"] == kind and document["collection"] == self.collections[kind]
                and document["owner_id"] == ("public" if kind == "knowledge" else OWNER)
                and document["document_id"] == expected_ids[kind] and document["chunk_id"]
                and (kind != "review" or document["review_id"] == self.review_id) for document in documents))
            self.check(name + "_" + kind + "_facts", any(all(fact in document["content"] for fact in FACTS[kind])
                                                        for document in documents))
            if kind == "review":
                record = self.review_repo.get(self.review_id, OWNER)
                self.check(name + "_review_full_original_and_vector_trace", all(
                    record["original_text"] in document["content"] and document.get("vector_excerpt")
                    and hashlib.sha256(document["vector_excerpt"].encode("utf-8")).hexdigest()
                    == record["index_manifest"][str(document["chunk_id"])] for document in documents))
        self.safe(name + "_isolation", routes)
        self.check(name + "_publication_visibility", "灰鹭保密" not in json.dumps(routes, ensure_ascii=False))

    def retrieval_checks(self):
        from knowledge.investment.retrieval import fuse
        self.real_retriever = self.retriever()
        routes = self.real_retriever.retrieve(COMBINED_QUERY)
        self.assert_routes("real_recall", routes)
        for kind in KINDS:
            searches = [search for search in self.milvus.searches if search["collection"] == self.collections[kind]]
            owner = "public" if kind == "knowledge" else OWNER
            self.check("milvus_query_scope_" + kind, bool(searches) and all(
                len(search["expressions"]) == 2 and all(
                    re.search(r'owner_id\s*==\s*"' + owner + '"', expression or "")
                    and re.search(r'doc_type\s*==\s*"' + kind + '"', expression or "")
                    for expression in search["expressions"]) for search in searches))
        self.check_fusion("real_rrf", routes, fuse(routes))
        collisions = {kind: {"items": [{**routes[kind]["items"][0], "document_id": "same-document",
                                       "review_id": "", "chunk_id": "42"}]} for kind in KINDS}
        collisions["personal"]["items"].append(deepcopy(collisions["personal"]["items"][0]))
        self.check("rrf_cross_collection_id_collision", len(fuse(collisions)) == 3)
        self.check_fusion("rrf_duplicate_source_once", collisions, fuse(collisions))
        for kind in KINDS:
            self.milvus.failed = {self.collections[kind]}
            try:
                failed = self.real_retriever.retrieve(COMBINED_QUERY)
                self.check("route_failure_" + kind, failed[kind]["status"] == "failed" and not failed[kind]["items"]
                           and all(failed[other]["status"] == "retrieved" for other in KINDS if other != kind))
                self.safe("route_failure_safe_" + kind, failed)
            finally:
                self.milvus.failed.clear()
            self.milvus.corrupt_owner = {self.collections[kind]}
            try:
                corrupted = self.real_retriever.retrieve(COMBINED_QUERY)
                self.check("returned_owner_rechecked_" + kind,
                           corrupted[kind]["status"] == "failed" and not corrupted[kind]["items"])
            finally:
                self.milvus.corrupt_owner.clear()
        self.report["coverage"]["real_three_route_retrieval"] = True

    def check_fusion(self, name, routes, fused):
        expected = {}
        for kind in KINDS:
            seen = set()
            for rank, document in enumerate(routes[kind]["items"], 1):
                key = source_key(document)
                if key not in seen:
                    expected[key] = expected.get(key, 0) + 1 / (60 + rank)
                    seen.add(key)
        expected_order = sorted(expected, key=lambda key: (-expected[key], key))[:18]
        self.check(name, [source_key(document) for document in fused] == expected_order
                   and all(math.isclose(document["score"], expected[source_key(document)], rel_tol=1e-9)
                           for document in fused))

    def build_app(self):
        from knowledge.api.query_api import create_app
        from knowledge.api.market_api import get_market_service
        from knowledge.api.review_api import get_review_service
        from knowledge.api.strategy_api import router as strategy_router, get_strategy_service
        from knowledge.api import summary_api
        from knowledge.core.deps import get_query_service
        from knowledge.investment.generation import EvidenceSelector, rerank
        from knowledge.market.repository import MarketQuoteRepository
        from knowledge.service.investment_service import InvestmentAssistant
        from knowledge.service.market_service import MarketService
        from knowledge.service.query_service import QueryService
        from knowledge.service.risk_service import RiskService
        from knowledge.service.summary_service import SummaryService
        from knowledge.summary.indices import IndexService, IndexRepository
        from knowledge.summary.repository import SummaryRepository, WatchlistRepository
        from knowledge.summary.strategies import PersonalStrategyRetriever
        from knowledge.utils import mongo_history_util
        from knowledge.utils.clients.storage_clients import StorageClients
        self.market = MarketService(MarketQuoteRepository(self.db[self.names["stocks"]]))
        self.risk = ObservedRisk(RiskService(market_factory=lambda: self.market))
        self.selector = ObservedSelector(EvidenceSelector())
        self.reranker = ObservedReranker(rerank)
        self.assistant = ObservedAssistant(InvestmentAssistant(self.real_retriever, self.selector, self.reranker))
        self.query_service = QueryService(risk=self.risk, investment=self.assistant)
        indices = IndexService(IndexRepository(self.db[self.names["indices"]]))
        summary = SummaryService(SummaryRepository(self.db[self.names["summaries"]]),
                                 WatchlistRepository(self.db[self.names["watches"]]), indices, self.market,
                                 PersonalStrategyRetriever(self.documents, self.vectors["personal"]))
        self.stack.enter_context(patch.object(mongo_history_util, "_get_collection", lambda: self.history))
        self.stack.enter_context(patch.object(StorageClients, "get_mongo_client", lambda: self.mongo))
        self.stack.enter_context(patch.object(StorageClients, "get_milvus_client", lambda: self.milvus))
        self.stack.enter_context(patch.object(summary_api, "_create_summary_service", lambda: summary))
        app = create_app(summary_enabled=False)
        app.include_router(strategy_router)
        app.dependency_overrides[get_query_service] = lambda: self.query_service
        app.dependency_overrides[get_market_service] = lambda: self.market
        app.dependency_overrides[get_review_service] = lambda: self.review_service
        app.dependency_overrides[get_strategy_service] = lambda: self.strategy_service
        app.dependency_overrides[summary_api.get_summary_service] = lambda: summary
        self.app = app

    def selected_checks(self, name, state, require_facts=False):
        if state.get("selection_status") != "completed":
            self.report.setdefault("selection_failures", {})[name] = state.get("selection_error", "unknown")
        self.check(name + "_real_selector_success", state.get("selection_status") == "completed"
                   and bool(self.selector.calls) and self.selector.calls[-1]["result"] is not None)
        self.check(name + "_real_rerank_success", state.get("rerank_status") == "completed"
                   and bool(self.reranker.calls) and self.reranker.calls[-1]["status"] == "completed")
        self.check_fusion(name + "_graph_rrf", state["routes"], state["fused"])
        ranked = self.reranker.calls[-1]
        fused_documents = {source_key(document): document for document in state["fused"]}
        self.check(name + "_rerank_retains_metadata", bool(ranked["output"]) and all(
            source_key(document) in fused_documents and math.isfinite(document["score"])
            and document["content"] == fused_documents[source_key(document)]["content"]
            and document["owner_id"] == fused_documents[source_key(document)]["owner_id"]
            for document in ranked["output"]))
        call = self.selector.calls[-1]
        selected = []
        for selection_index, selection in enumerate(call["result"].get("selections", [])):
            match = re.fullmatch(r"E([1-9][0-9]*)", selection.get("reference", ""))
            self.check(name + f"_selection_reference_{selection_index}", bool(match) and int(match[1]) <= len(call["documents"]))
            document = call["documents"][int(match[1]) - 1]
            self.check(name + f"_continuous_quote_{selection_index}", bool(selection["quote"]) and selection["quote"] in document["content"])
            self.check(name + f"_market_references_{selection_index}", set(selection.get("evidence_ids", [])).issubset(
                {item["id"] for item in call["market"]}))
            selected.append((document, selection["quote"]))
        rendered = state["reranked_docs"]
        selected_keys = {source_key(document) for document, quote in selected}
        self.check(name + "_selected_docs_used", bool(selected) and
                   {selection_key(document) for document, quote in selected} ==
                   {selection_key(document) for document in rendered}
                   and all(source_key(document) in selected_keys for document in rendered))
        self.check(name + "_review_record_rendered_once", len({selection_key(document) for document in rendered}) == len(rendered))
        selected = [(document, next(quote for candidate, quote in selected if source_key(candidate) == source_key(document)))
                    for document in rendered]
        answer = state["answer"]
        blocks = {kind: [] for kind in KINDS}
        for selection_index, (document, quote) in enumerate(selected):
            kind = document["source"]
            marker = "[chunk_id=" + str(document["chunk_id"]) + "]"
            identity = "review_id" if kind == "review" else "document_id"
            self.check(name + "_citation_" + kind, marker in answer and
                       "collection=" + document["collection"] in answer and
                       identity + "=" + document[identity] in answer and "来源：" + kind in answer)
            position = answer.find(marker)
            preceding = answer[:position]
            boundary = preceding.rfind(CHECK_HINTS[kind])
            block = preceding[boundary:] if boundary >= 0 else ""
            blocks[kind].append(block)
            self.check(name + f"_quote_in_check_{kind}_{selection_index}", bool(block) and quote in block
                       and LABELS[kind] + "原文" in block)
        if require_facts:
            self.assert_routes(name, state["routes"])
            for kind in KINDS:
                self.check(name + "_selected_fact_" + kind, any(
                    document["source"] == kind and all(fact in document["content"] for fact in FACTS[kind])
                    for document, quote in selected))
                self.check(name + "_facts_in_check_" + kind,
                           any(all(fact in block for fact in FACTS[kind]) for block in blocks[kind]))
            self.check(name + "_conditional_not_triggered", "当前未确认满足" in answer
                       and "熊市轻仓只适用于已经人工核实市场条件" in answer)
            self.check(name + "_not_current_holdings", "记录不代表当前持仓" in answer and "今天没有成交" in answer)
        citations = set(re.findall(r"\[chunk_id=([^\]]+)\]", answer))
        self.check(name + "_only_retrieved_citations", citations ==
                   {str(document["chunk_id"]) for document in state["reranked_docs"]})
        self.answer_policy(name, answer)
        self.report.setdefault("selections", {})[name] = [{
            "source": document["source"], "collection": document["collection"],
            "document_id": document["document_id"], "review_id": document.get("review_id"),
            "chunk_id": document["chunk_id"], "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        } for document, quote in selected]
        self.report["coverage"].update(real_reranker=True, real_selector=True, real_investment_graph=True)

    def api_query(self, api, name, query, streaming=False, session=None):
        session = session or "investment-accept-" + uuid.uuid4().hex
        previous = self.history.count_documents({"session_id": session})
        response = api.post("/query", json={"query": query, "is_stream": streaming, "session_id": session})
        self.check(name + "_http", response.status_code == 200)
        body = response.json()
        self.check(name + "_session", body["session_id"] == session)
        if streaming:
            task_id = body["task_id"]
            status = api.get("/status/" + task_id)
            self.check(name + "_status_completed", status.status_code == 200 and status.json()["status"] == "completed")
            response = api.get("/stream/" + task_id)
            self.check(name + "_sse_http", response.status_code == 200 and
                       response.headers.get("content-type", "").startswith("text/event-stream"))
            events = parse_sse(response.text)
            finals = [payload for event, payload in events if event == "final"]
            deltas = [payload.get("answer", "") for event, payload in events if event == "delta"]
            self.check(name + "_buffered_single_delta_final", len(finals) == 1 and len(deltas) == 1
                       and events[-1][0] == "final" and not finals[0].get("error"))
            answer = finals[0]["answer"]
            self.check(name + "_sse_status_equal", deltas[0] == answer == status.json()["answer"])
            self.safe(name + "_every_event_safe", events)
            from knowledge.utils.sse_util import get_sse_queue
            self.check(name + "_sse_queue_removed", get_sse_queue(task_id) is None)
            self.report["coverage"]["real_sse"] = True
        else:
            self.check(name + "_no_api_error", not body.get("error"))
            answer = body["answer"]
        history = api.get("/history/" + session)
        self.check(name + "_history_http", history.status_code == 200)
        items = history.json()["items"]
        persisted = list(self.history.find({"session_id": session}).sort([("ts", 1), ("_id", 1)]))
        self.check(name + "_history_equal", len(items) == len(persisted) == previous + 2
                   and items[-1]["role"] == persisted[-1]["role"] == "assistant"
                   and items[-1]["text"] == persisted[-1]["text"] == answer)
        self.safe(name + "_history_privacy", persisted)
        self.answer_policy(name, answer)
        self.report["coverage"].update(real_api=True, real_history=True)
        return answer, session

    def host_checks(self):
        from fastapi.testclient import TestClient
        with TestClient(self.app) as api:
            answer, session = self.api_query(api, "combined", COMBINED_QUERY)
            self.check("query_uses_investment", bool(self.assistant.states) and self.assistant.states[-1]["answer"] == answer)
            self.selected_checks("combined", self.assistant.states[-1], require_facts=True)
            answer, session = self.api_query(api, "stream", COMBINED_QUERY + " 我的手机号" + PHONE,
                                             streaming=True, session=session)
            self.selected_checks("stream", self.assistant.states[-1], require_facts=True)
            stock_start = len(self.assistant.states)
            answer, stock_session = self.api_query(api, "stock", "平安银行000001的风险如何？", streaming=True)
            self.check("stock_does_not_bypass_three_routes", len(self.assistant.states) == stock_start + 1)
            state = self.assistant.states[-1]
            self.stock_market = state["market"]
            self.selected_checks("stock", state)
            risk = self.risk.answers[-1]
            self.check("risk_factory_and_market_context", risk["code"] == "000001" and
                       state["market"] == [{"id": "stock:000001", "text": risk["text"]}] and risk["text"] in answer)
            quotes = list(self.db[self.names["stocks"]].find({"code": "000001"}, {"_id": 0, "raw": 0}))
            self.report["market"] = {"status": "available" if quotes else "unavailable", "rows": len(quotes),
                                     "dates": sorted({str(quote["date"]) for quote in quotes}),
                                     "sources": sorted({quote.get("source", "unknown") for quote in quotes}),
                                     "risk": re.findall(r"个股风险：[^。\n]+", risk["text"])}
            if quotes:
                self.check("real_market_date_and_source", "实际行情日期" in answer and "行情来源" in answer
                           and any(str(quote["date"]) in answer and quote["source"] in answer for quote in quotes))
            else:
                self.check("real_market_failure_is_explicit", any(word in answer for word in ("暂不可用", "无有效收盘行情")))
            self.report["coverage"]["real_market_attempt"] = True
            self.report["coverage"]["real_market_data"] = bool(quotes)
            if not quotes:
                self.report["pending"] = ["真实行情返回数据；本次仅验证了真实调用失败后的降级"]
            answer, followup_session = self.api_query(api, "followup", "那它呢？", session=stock_session)
            self.check("followup_keeps_security", self.assistant.states[-1]["market"]
                       and self.assistant.states[-1]["market"][0]["id"] == "stock:000001")
            before = len(self.assistant.states)
            answer, conflict_session = self.api_query(api, "identity_conflict", "贵州茅台000001风险如何？", session=stock_session)
            self.check("conflict_before_history_or_investment", len(self.assistant.states) == before and "确认" in answer)
            for name, question in (("personal", "按我以前的交易教训，追高前应核对什么？"),
                                   ("concept", "请解释安全边际"),
                                   ("planned", "我的复盘中计划买入的那笔记录实际成交了吗？")):
                before = len(self.assistant.states)
                answer, case_session = self.api_query(api, name, question)
                self.check(name + "_investment_route", len(self.assistant.states) == before + 1)
                self.selected_checks(name, self.assistant.states[-1])
                if name == "planned":
                    self.check("planned_answer_retains_negation", "今天没有成交" in answer and "尚未提交订单" in answer)
            from knowledge.investment.routing import route_query
            product = route_query("青禾QH-731复盘记录器的校验口令和回看窗口是什么？",
                                  self.query_service.resolver, [])
            self.check("product_keeps_existing_route", product["investment"] is False)
            self.fault_checks(api)
            self.check("foreign_review_api_hidden", api.get("/reviews/" + self.foreign_review_id).status_code == 404)
            visible = api.get("/reviews").json()["items"]
            self.check("foreign_review_listing_hidden", all(record["owner_id"] == OWNER for record in visible))
            self.safe("review_api_isolation", visible)
            deleted = api.delete("/history/" + session)
            self.check("history_delete_only_test_session", deleted.status_code == 200
                       and deleted.json()["deleted_count"] == 4 and not api.get("/history/" + session).json()["items"])

    def fault_checks(self, api):
        from knowledge.service.investment_service import InvestmentAssistant
        from knowledge.utils.answer_policy import finalize_answer
        market = self.stock_market
        for name, selector, reranker in (("model_failure", FailedSelector(), None),
                                         ("forged_reference", ForgedSelector(), None),
                                         ("rerank_failure", None, fail_dependency)):
            assistant = InvestmentAssistant(self.real_retriever, selector=selector, reranker=reranker)
            result = assistant.answer(COMBINED_QUERY, market_context=market)
            self.check(name + "_grounded_fallback", bool(result["reranked_docs"])
                       and all(facts[0] in result["answer"] for facts in FACTS.values())
                       and market[0]["text"] in result["answer"])
            self.check(name + "_visible_status", result["rerank_status" if name == "rerank_failure" else "selection_status"] == "failed"
                       and ("重排暂不可用" if name == "rerank_failure" else "模型整理暂不可用") in result["answer"])
            self.answer_policy(name, result["answer"])
        with patch.object(self.assistant.delegate, "selector", FailedSelector()):
            answer, failure_session = self.api_query(api, "model_failure_sse", COMBINED_QUERY, streaming=True)
            self.check("model_failure_sse_is_grounded", "模型整理暂不可用" in answer and "青鹭7391" in answer)
        self.milvus.failed = {self.collections["personal"]}
        try:
            answer, failure_session = self.api_query(api, "personal_failure_stock", "平安银行000001的风险如何？", streaming=True)
            state = self.assistant.states[-1]
            self.check("personal_failure_preserves_other_routes_and_market", state["routes"]["personal"]["status"] == "failed"
                       and all(state["routes"][kind]["status"] == "retrieved" for kind in ("knowledge", "review"))
                       and self.risk.answers[-1]["text"] in answer and "青鹭7391" not in answer)
        finally:
            self.milvus.failed.clear()
        self.milvus.failed = set(self.collections.values())
        try:
            result = self.assistant.answer(COMBINED_QUERY, market_context=market)
            self.check("all_routes_failed_explicit", all(route["status"] == "failed" for route in result["routes"].values())
                       and not result["reranked_docs"] and "无法生成有据的个性化提示" in result["answer"]
                       and market[0]["text"] in result["answer"])
            self.answer_policy("all_routes_failed", result["answer"])
        finally:
            self.milvus.failed.clear()
        empty_collections = {kind: self.prefix + "_empty_" + kind for kind in KINDS}
        self.milvus.allowed.update(empty_collections.values())
        from knowledge.investment.retrieval import InvestmentRetriever
        empty = InvestmentRetriever(self.documents, self.reviews, self.milvus, collections=empty_collections)
        result = InvestmentAssistant(empty).answer(COMBINED_QUERY)
        self.check("all_routes_empty_distinct", all(route["status"] == "empty" for route in result["routes"].values())
                   and result["selection_status"] == "empty" and "暂无可用资料" in result["answer"])
        self.answer_policy("all_routes_empty", result["answer"])
        protected = finalize_answer("未核实引用 [chunk_id=" + PHONE + "]，手机号" + PHONE)
        self.check("untrusted_chunk_not_privacy_whitelisted", PHONE not in protected)
        self.report["coverage"]["failure_injection"] = True

    def review_recovery_checks(self):
        from knowledge.review.indexing import ReviewIndexer
        self.reviews.update_one({"_id": self.storage_probe_id}, {"$set": {
            "index_retry_after": datetime.now(timezone.utc) - timedelta(seconds=1)}})
        recovered = self.review_service.reindex(self.storage_probe_id)
        self.check("explicit_review_id_reindex_recovers", recovered["index_status"] == "completed"
                   and recovered["extraction_status"] == "failed")
        self.reviews.update_one({"_id": self.storage_probe_id}, {"$set": {"index_status": "failed"}})
        long_id = str(uuid.uuid4())
        tail = "尾部复盘暗号丹鹤尾证：只有核实撤单回执后才考虑下一次委托，挂单仍不等于成交。"
        original = ("今天整理自建交易日记，只记录观察过程，不断言持仓或成交。\n\n" * 120) + tail
        self.review_repo.create(long_id, OWNER, original)
        ReviewIndexer(self.review_repo, self.vectors["review"], max_tokens=512).index(long_id, OWNER)
        record = self.review_repo.get(long_id, OWNER)
        self.check("long_review_real_index", record["index_status"] == "completed" and len(record["index_manifest"]) > 2)
        self.milvus.flush(self.collections["review"], timeout=30)
        routes = self.real_retriever.retrieve("复盘尾部的丹鹤尾证暗号要求先核实什么回执？")
        self.check("long_review_tail_real_bge_recall", any(
            document["review_id"] == long_id and tail in document.get("vector_excerpt", "")
            and original in document["content"] for document in routes["review"]["items"]))
        self.reviews.update_one({"_id": long_id}, {"$set": {"index_status": "failed"}})

    def serve(self):
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.args.port,
                                              log_level="warning", access_log=False,
                                              timeout_graceful_shutdown=60))
        finished = threading.Event()

        def monitor():
            while not finished.wait(0.25):
                if self.args.stop_file and self.args.stop_file.exists():
                    server.should_exit = True
                    return

        watcher = threading.Thread(target=monitor, name="investment-accept-stop", daemon=True)
        watcher.start()
        print(f"浏览器入口：http://127.0.0.1:{self.args.port}/chat.html；使用本次 UUID 测试集合，自动摘要关闭。", flush=True)
        if self.args.stop_file:
            print("创建停止文件可安全退出并清理：" + str(self.args.stop_file), flush=True)
        try:
            try:
                server.run()
            except KeyboardInterrupt:
                self.report["browser_stop"] = "keyboard_interrupt"
            self.check("browser_server_started", server.started)
        finally:
            server.should_exit = True
            finished.set()
            watcher.join(timeout=2)

    def cleanup(self):
        try:
            self.stack.close()
        except Exception:
            self.report["cleanup"]["dependency_restore"] = False
        if self.raw_milvus is not None:
            for name in reversed(self.vector_names):
                try:
                    if self.raw_milvus.has_collection(name, timeout=10):
                        self.raw_milvus.drop_collection(name, timeout=30)
                    self.report["cleanup"][name] = not self.raw_milvus.has_collection(name, timeout=10)
                except Exception:
                    self.report["cleanup"][name] = False
            try:
                self.raw_milvus.close()
            except Exception:
                self.report["cleanup"]["milvus_close"] = False
        if self.mongo is not None:
            for name in reversed(self.mongo_names):
                try:
                    self.db[name].drop()
                    self.report["cleanup"][name] = not self.db.list_collection_names(filter={"name": name})
                except Exception:
                    self.report["cleanup"][name] = False
            try:
                self.mongo.close()
            except Exception:
                self.report["cleanup"]["mongo_close"] = False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", action="store_true", help="使用本机已有 BGE/reranker、真实 LLM/行情/API")
    parser.add_argument("--serve", action="store_true", help="全链路检查通过后启动浏览器验收服务")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--stop-file", type=Path, help="检测到该文件后正常停止服务并清理测试资源")
    parser.add_argument("--report", type=Path, help="可选：写入一个尚不存在的 JSON 报告文件")
    args = parser.parse_args(argv)
    if args.serve and not args.host:
        parser.error("--serve 需要 --host")
    if args.stop_file and not args.serve:
        parser.error("--stop-file 需要 --serve")
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    for path in (args.stop_file, args.report):
        if path is not None and path.exists():
            parser.error("停止文件和报告文件必须尚不存在")
        if path is not None and not path.resolve().parent.is_dir():
            parser.error("停止文件和报告文件的父目录必须已经存在")
    if args.stop_file and args.report and args.stop_file.resolve() == args.report.resolve():
        parser.error("停止文件和报告文件不能使用同一路径")
    if args.stop_file:
        args.stop_file = args.stop_file.resolve()
    test_db = os.getenv("AFK_TEST_DB_NAME")
    prefix = "investment_accept_" + uuid.uuid4().hex
    report = {"run_id": prefix, "mode": "host" if args.host else "storage", "checks": {}, "cleanup": {},
              "coverage": {}, "passed": False, "full_acceptance": False}
    started = time.monotonic()
    acceptance = None
    sys.dont_write_bytecode = True
    logging.disable(logging.CRITICAL)
    try:
        environment = isolated_environment(test_db, args.host, prefix)
        with patch.dict(os.environ, environment):
            acceptance = Acceptance(args, test_db, prefix, report)
            try:
                if args.host:
                    for variable in ("BGE_M3_PATH", "BGE_RERANKER_LARGE"):
                        acceptance.check("local_" + variable, bool(os.getenv(variable)) and Path(os.environ[variable]).is_dir())
                    acceptance.check("llm_configuration", all(os.getenv(variable) for variable in
                                                               ("OPEN_API_BASE", "OPEN_API_KEY", "LLM_DEFAULT_MODEL")))
                acceptance.prepare()
                acceptance.storage_failure_probe()
                if args.host:
                    acceptance.seed()
                    acceptance.retrieval_checks()
                    acceptance.build_app()
                    acceptance.host_checks()
                    acceptance.review_recovery_checks()
                    if args.serve:
                        acceptance.serve()
                report["passed"] = True
            finally:
                acceptance.cleanup()
    except KeyboardInterrupt:
        report["error_type"] = "KeyboardInterrupt"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, AcceptanceFailure):
            report["failed_check"] = str(exc)
        print("验收失败；异常详情已隐藏，类型：" + type(exc).__name__, flush=True)
    finally:
        report["passed"] = report["passed"] and bool(report["cleanup"]) and all(report["cleanup"].values())
        report["full_acceptance"] = args.host and report["passed"] and not report.get("pending")
        report["seconds"] = round(time.monotonic() - started, 2)
        if not args.host:
            report["pending"] = ["BGE-M3", "三路召回/RRF", "reranker", "selector/LLM", "API/SSE/history", "真实行情"]
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            try:
                with args.report.open("x", encoding="utf-8") as destination:
                    destination.write(serialized + "\n")
            except Exception:
                report["passed"] = report["full_acceptance"] = False
                report["report_write_failed"] = True
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        print("结果：" + ("PASS" if report["passed"] else "FAIL")
              + ("（主机检查；完整性见 full_acceptance/pending）" if args.host else "（仅存储探针，完整验收需 --host）"), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

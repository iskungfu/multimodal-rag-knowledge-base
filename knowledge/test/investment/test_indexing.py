import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymilvus import DataType
from pymongo.errors import AutoReconnect

from knowledge.api.review_api import get_review_service, router
from knowledge.review.indexing import ReviewIndexer, digest, index_texts
from knowledge.review.privacy import redact_text, redact_value
from knowledge.review.repository import ReviewRepository
from knowledge.review.vectors import ReviewVectors
from knowledge.service.review_service import DEFAULT_OWNER, ReviewService, present


TEXT = "如果明天回踩支撑才考虑买入 000001。\n今天没有成交，也不能据此推断持仓。"
EXTRACTION = {"events": [], "lesson": None, "emotions": []}


class ByteTokenizer:
    def encode(self, text, add_special_tokens=True, truncation=False):
        assert add_special_tokens is True
        assert truncation is False
        return [0, *text.encode("utf-8"), 1]


class MemoryVectors(ReviewVectors):
    def __init__(self):
        self.rows = {}
        client = Mock()
        client.has_collection.return_value = True
        client.describe_collection.return_value = {"fields": [
            {"name": name, "type": DataType.VARCHAR} for name in (
                "id", "owner_id", "doc_type", "document_id", "review_id", "index_version", "content_hash")
        ]}
        client.upsert.side_effect = self.upsert
        client.delete.side_effect = self.delete
        super().__init__(client, "memory_reviews", dim=2)

    def upsert(self, collection, rows, timeout):
        self.rows.update({row["id"]: deepcopy(row) for row in rows})
        return {"upsert_count": len(rows)}

    def delete(self, collection, filter, timeout):
        predicates = {}
        for expression in filter.split(" and "):
            field, value = expression.split(" == ")
            predicates[field] = json.loads(value)
        assert set(predicates) == {"owner_id", "doc_type", "review_id", "index_version"}
        self.rows = {identifier: row for identifier, row in self.rows.items()
                     if not all(row[field] == value for field, value in predicates.items())}


@pytest.fixture
def setup_indexing(monkeypatch):
    monkeypatch.setattr("knowledge.api.review_api.MongoClient", Mock(side_effect=AssertionError("禁止连接真实 MongoDB")))
    collection = mongomock.MongoClient(tz_aware=True).index_tests.trade_reviews
    repository = ReviewRepository(collection)
    vectors = MemoryVectors()
    tokenizer = ByteTokenizer()
    clock = SimpleNamespace(now=datetime.now(timezone.utc))
    embedded_texts = []

    def embed(texts):
        assert all(len(tokenizer.encode(text)) <= 512 for text in texts)
        embedded_texts.extend(texts)
        return {"dense": [[1.0, 0.0] for text in texts], "sparse": [{1: 1.0} for text in texts]}

    indexer = ReviewIndexer(repository, vectors, embed=embed, now=lambda: clock.now, tokenizer=tokenizer)
    return SimpleNamespace(collection=collection, repository=repository, vectors=vectors,
                           tokenizer=tokenizer, clock=clock, indexer=indexer, embedded_texts=embedded_texts)


def create_review(components, text=TEXT, extracted=True):
    identifier = str(uuid4())
    components.repository.create(identifier, DEFAULT_OWNER, text)
    if extracted:
        components.repository.finish(identifier, DEFAULT_OWNER, EXTRACTION, None)
    return identifier


def assert_published(components, identifier):
    document = components.repository.get(identifier, DEFAULT_OWNER)
    assert document["index_status"] == "completed"
    rows = [row for row in components.vectors.rows.values()
            if row["review_id"] == identifier and row["index_version"] == document["index_version"]]
    assert rows
    assert document["index_manifest"] == {row["id"]: digest(row["content"]) for row in rows}
    assert all(row["content_hash"] == digest(row["content"]) for row in rows)
    return document, rows


def test_lost_publish_response_never_deletes_committed_vectors(setup_indexing, monkeypatch):
    components = setup_indexing
    identifier = create_review(components)
    update = components.collection.update_one

    def lose_success_response(scope, changes):
        result = update(scope, changes)
        if changes["$set"].get("index_status") == "completed":
            raise AutoReconnect("response lost after commit")
        return result

    monkeypatch.setattr(components.collection, "update_one", lose_success_response)
    components.indexer.index(identifier, DEFAULT_OWNER)
    document, rows = assert_published(components, identifier)
    assert document["original_text"] == TEXT
    assert not any(document["index_version"] in call.kwargs["filter"]
                   for call in components.vectors.client.delete.call_args_list)


@pytest.mark.parametrize("committed", [False, True])
def test_unknown_publish_and_failed_confirmation_leave_vectors(setup_indexing, monkeypatch, committed):
    components = setup_indexing
    identifier = create_review(components)
    finish = components.repository.finish_index

    def unavailable(claim, now, manifest=None):
        if manifest is not None and committed:
            finish(claim, now, manifest)
        raise AutoReconnect("unknown outcome")

    monkeypatch.setattr(components.repository, "finish_index", unavailable)
    monkeypatch.setattr(components.repository, "index_is_unpublished", Mock(side_effect=AutoReconnect()))
    components.indexer.index(identifier, DEFAULT_OWNER)
    assert components.vectors.rows
    if committed:
        assert_published(components, identifier)
    else:
        assert components.repository.get(identifier, DEFAULT_OWNER)["index_status"] == "processing"


@pytest.mark.parametrize("response_lost", [False, True])
def test_confirmed_failure_cleans_only_its_unpublished_version(setup_indexing, monkeypatch, response_lost):
    components = setup_indexing
    identifier = create_review(components)
    write = components.vectors.write
    finish = components.repository.finish_index

    def fail_after_write(*arguments):
        write(*arguments)
        raise RuntimeError("write response lost")

    def fail_response(claim, now, manifest=None):
        result = finish(claim, now, manifest)
        if response_lost:
            raise AutoReconnect()
        return result

    monkeypatch.setattr(components.vectors, "write", fail_after_write)
    monkeypatch.setattr(components.repository, "finish_index", fail_response)
    components.indexer.index(identifier, DEFAULT_OWNER)
    document = components.repository.get(identifier, DEFAULT_OWNER)
    assert document["index_status"] == "failed"
    assert document["original_text"] == TEXT
    assert not components.vectors.rows
    assert document["index_token"] in components.vectors.client.delete.call_args.kwargs["filter"]


def test_unresolved_processing_version_is_not_safe_to_delete_even_after_expiry(setup_indexing):
    components = setup_indexing
    identifier = create_review(components)
    claim = components.repository.claim_index(identifier, DEFAULT_OWNER, components.clock.now)
    components.clock.now += timedelta(minutes=4)
    assert not components.repository.index_is_unpublished(claim)
    replacement = components.repository.claim_index(identifier, DEFAULT_OWNER, components.clock.now)
    assert replacement["index_token"] != claim["index_token"]
    assert components.repository.index_is_unpublished(claim)


@pytest.mark.parametrize("error", [None, "model_unavailable"])
def test_late_extraction_atomically_invalidates_completed_index(setup_indexing, error):
    components = setup_indexing
    identifier = create_review(components, extracted=False)
    components.indexer.index(identifier, DEFAULT_OWNER)
    before, rows = assert_published(components, identifier)
    changes = components.repository.finish(identifier, DEFAULT_OWNER, EXTRACTION if not error else None, error)
    after = components.repository.get(identifier, DEFAULT_OWNER)
    assert after["index_status"] == "pending"
    assert after["index_token"] != before["index_token"]
    assert after["index_manifest"] == {}
    assert changes["index_status"] == "pending"
    assert not components.collection.find_one({"_id": identifier, "index_status": "completed",
                                               "index_version": before["index_version"]})
    components.indexer.index(identifier, DEFAULT_OWNER)
    rebuilt, rows = assert_published(components, identifier)
    assert rebuilt["index_version"] != before["index_version"]
    assert all(row["index_version"] != before["index_version"] for row in components.vectors.rows.values())
    assert any(f'"extraction_status":"{after["extraction_status"]}"' in row["content"] for row in rows)


def test_extraction_revokes_worker_before_publish(setup_indexing, monkeypatch):
    components = setup_indexing
    identifier = create_review(components, extracted=False)
    write = components.vectors.write

    def finish_extraction_after_write(*arguments):
        manifest = write(*arguments)
        components.repository.finish(identifier, DEFAULT_OWNER, EXTRACTION, None)
        return manifest

    monkeypatch.setattr(components.vectors, "write", finish_extraction_after_write)
    components.indexer.index(identifier, DEFAULT_OWNER)
    assert components.repository.get(identifier, DEFAULT_OWNER)["index_status"] == "pending"
    assert not components.vectors.rows


def test_claim_snapshot_before_extraction_cannot_acquire_stale_content(setup_indexing, monkeypatch):
    components = setup_indexing
    identifier = create_review(components, extracted=False)
    claim = components.collection.find_one_and_update

    def finish_between_read_and_claim(scope, changes, **options):
        components.repository.finish(identifier, DEFAULT_OWNER, EXTRACTION, None)
        return claim(scope, changes, **options)

    monkeypatch.setattr(components.collection, "find_one_and_update", finish_between_read_and_claim)
    assert components.repository.claim_index(identifier, DEFAULT_OWNER, components.clock.now) is None
    assert components.repository.get(identifier, DEFAULT_OWNER)["extraction_status"] == "succeeded"


def test_resumed_old_worker_never_deletes_new_published_version(setup_indexing, monkeypatch):
    components = setup_indexing
    identifier = create_review(components)
    original_embed = components.indexer.embed
    entered = False

    def delayed_embed(texts):
        nonlocal entered
        if not entered:
            entered = True
            components.clock.now += timedelta(seconds=181)
            replacement = ReviewIndexer(components.repository, components.vectors, embed=original_embed,
                                        now=lambda: components.clock.now, tokenizer=components.tokenizer)
            replacement.index(identifier, DEFAULT_OWNER)
            assert_published(components, identifier)
        return original_embed(texts)

    monkeypatch.setattr(components.indexer, "embed", delayed_embed)
    components.indexer.index(identifier, DEFAULT_OWNER)
    document, rows = assert_published(components, identifier)
    assert len(rows) == len(components.vectors.rows)
    assert all(document["index_version"] not in call.kwargs["filter"]
               for call in components.vectors.client.delete.call_args_list)


@pytest.mark.parametrize("separator", ["\n\n", ""])
def test_10000_characters_are_embedded_with_real_budget_and_tail(setup_indexing, separator):
    components = setup_indexing
    paragraph = "如果明天回踩支撑才考虑买入 000001。今天没有成交。" + separator
    tail = "尾部复盘教训：警惕追高，先核对交易条件。"
    original = (paragraph * (10000 // len(paragraph) + 1))[:10000 - len(tail)] + tail
    identifier = create_review(components, original)
    components.indexer.index(identifier, DEFAULT_OWNER)
    document, rows = assert_published(components, identifier)
    originals = [row for row in rows if row["title"] == "复盘原文"]
    assert len(originals) > 2
    assert "".join(row["content"].split("\n", 1)[1] for row in originals) == original
    assert [row["content"] for row in rows] == components.embedded_texts
    assert all(len(components.tokenizer.encode(row["content"])) <= 512 for row in rows)
    assert all(len(row["content"]) <= 12000 for row in rows)
    assert any(tail in text for text in components.embedded_texts)
    for row in originals:
        assert "计划、挂单和未知不等于成交" in row["content"]
        assert "仅片段，须核对全文前提" in row["content"]


def test_oversized_condition_chunks_require_full_source_context(setup_indexing):
    components = setup_indexing
    original = "只有在这些前提满足时：\n" + "等待更多确认。" * 200 + "\n才考虑买入，今天未成交。"
    identifier = create_review(components, original)
    components.indexer.index(identifier, DEFAULT_OWNER)
    document, rows = assert_published(components, identifier)
    assert document["original_text"] == original
    originals = [row["content"] for row in rows if row["title"] == "复盘原文"]
    assert "".join(text.split("\n", 1)[1] for text in originals) == original
    assert all("仅片段，须核对全文前提" in text for text in originals)


def test_summary_preserves_whole_planned_unknown_and_conflicting_events():
    events = [
        {"status": "planned", "stock_code": None, "mentioned_stock_code": "000001",
         "identity_conflict": True, "evidence": "计划买入 000001 上证指数 ETF，尚未成交。"},
        {"status": "unknown", "stock_code": None, "mentioned_stock_code": None,
         "identity_conflict": False, "evidence": "是否成交还没有确认。"},
    ]
    document = {"original_text": TEXT, "extraction_status": "succeeded",
                "extraction": {**EXTRACTION, "events": events}}
    texts = index_texts(document, ByteTokenizer(), 1024)
    summaries = [json.loads(text[text.index("{"):]) for text in texts if text.startswith("复盘抽取摘要")]
    assert [summary["event"] for summary in summaries if "event" in summary] == events
    assert all(summary["extraction_status"] == "succeeded" for summary in summaries)


def test_oversized_event_is_chunked_without_losing_status_conflict_or_tail():
    document = {"original_text": TEXT, "extraction_status": "succeeded",
                "extraction": {**EXTRACTION, "events": [{"status": "planned", "identity_conflict": True,
                                                        "evidence": "前提" * 11000 + "摘要尾部依据"}]}}
    texts = index_texts(document, ByteTokenizer(), 512)
    events = [text for text in texts if text.startswith("复盘抽取摘要") and "事件状态：" in text]
    assert len(events) > 1
    assert all("事件状态：planned；名称代码冲突：True。" in text for text in events)
    assert all("仅片段" in text and "须核对全文" in text for text in events)
    reconstructed = "".join(text.split("\n", 2)[2] for text in events)
    assert json.loads(reconstructed)["event"] == document["extraction"]["events"][0]
    assert any("摘要尾部依据" in text for text in events)
    assert all(len(ByteTokenizer().encode(text)) <= 512 for text in texts)


def test_runtime_budget_comes_from_installed_encoder_settings(setup_indexing, monkeypatch):
    from knowledge.utils.clients.ai_clients import AIClients

    components = setup_indexing
    client = SimpleNamespace(model=SimpleNamespace(tokenizer=components.tokenizer, passage_max_length=512),
                             _encode_config={})
    monkeypatch.setattr(AIClients, "get_bge_m3_client", lambda: client)
    indexer = ReviewIndexer(components.repository, embed=Mock(), max_tokens=8192)
    assert indexer.token_budget() == (components.tokenizer, 512)
    client._encode_config["max_length"] = 256
    assert indexer.token_budget() == (components.tokenizer, 256)


def test_token_count_includes_special_tokens():
    tokenizer = ByteTokenizer()
    document = {"original_text": TEXT, "extraction_status": "pending", "extraction": None}
    texts = index_texts(document, tokenizer, 512)
    longest = max(len(tokenizer.encode(text)) for text in texts)
    assert index_texts(document, tokenizer, longest) == texts
    smaller = index_texts(document, tokenizer, longest - 1)
    assert len(smaller) > len(texts)
    assert all(len(tokenizer.encode(text)) <= longest - 1 for text in smaller)


def test_content_length_cap_also_applies_to_very_compact_tokenization():
    tokenizer = Mock()
    tokenizer.encode.return_value = [0, 1, 2]
    document = {"original_text": "x" * 12000, "extraction_status": "pending", "extraction": None}
    texts = index_texts(document, tokenizer, 512)
    assert all(len(text) <= 12000 for text in texts)
    assert "".join(text.split("\n", 1)[1] for text in texts if text.startswith("复盘原文")) == document["original_text"]


def test_legacy_record_is_indexed_only_by_requested_id(setup_indexing):
    components = setup_indexing
    identifier = create_review(components)
    other = create_review(components)
    for review_id in (identifier, other):
        document = components.repository.get(review_id, DEFAULT_OWNER)
        components.collection.update_one({"_id": review_id}, {"$unset": {
            name: "" for name in document if name.startswith("index_")}})
    components.indexer.index(identifier, DEFAULT_OWNER)
    assert_published(components, identifier)
    assert "index_status" not in components.repository.get(other, DEFAULT_OWNER)
    assert components.repository.claim_index(other, "another_owner", components.clock.now) is None
    before = deepcopy(components.vectors.rows)
    components.indexer.index(identifier, DEFAULT_OWNER)
    assert components.vectors.rows == before


def test_retry_cooldown_and_rate_limit(setup_indexing):
    components = setup_indexing
    identifier = create_review(components)
    for attempt in range(3):
        claim = components.repository.claim_index(identifier, DEFAULT_OWNER, components.clock.now)
        assert claim["index_attempts"] == attempt + 1
        assert components.repository.finish_index(claim, components.clock.now)
        assert components.repository.claim_index(identifier, DEFAULT_OWNER, components.clock.now) is None
        components.clock.now += timedelta(seconds=31)
    assert components.repository.claim_index(identifier, DEFAULT_OWNER, components.clock.now) is None
    components.clock.now += timedelta(minutes=10)
    assert components.repository.claim_index(identifier, DEFAULT_OWNER, components.clock.now)["index_attempts"] == 1


def test_retry_api_retains_original_after_embedding_failure(setup_indexing, monkeypatch):
    components = setup_indexing
    original_embed = components.indexer.embed
    monkeypatch.setattr(components.indexer, "embed", Mock(side_effect=RuntimeError("private model details")))
    extractor = Mock()
    extractor.extract.return_value = EXTRACTION
    service = ReviewService(components.repository, extractor, components.indexer)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_review_service] = lambda: service
    with TestClient(app) as client:
        response = client.post("/reviews", json={"text": TEXT})
        assert response.status_code == 201
        saved = response.json()
        assert saved["original_text"] == TEXT
        assert saved["index_status"] == "failed"
        assert saved["index_can_retry"] is False
        assert "private" not in response.text
        assert "index_manifest" not in saved and "index_token" not in saved
        assert client.get(f'/reviews/{saved["id"]}').json()["original_text"] == TEXT
        components.clock.now += timedelta(seconds=31)
        monkeypatch.setattr(components.indexer, "embed", original_embed)
        retried = client.post(f'/reviews/{saved["id"]}/index')
        assert retried.status_code == 200 and retried.json()["index_status"] == "completed"
        assert retried.json()["original_text"] == TEXT
        assert client.post(f"/reviews/{uuid4()}/index").status_code == 404
    assert extractor.extract.call_count == 1


def test_public_status_handles_expired_lease_and_missing_legacy_fields(setup_indexing):
    components = setup_indexing
    identifier = create_review(components)
    document = components.repository.get(identifier, DEFAULT_OWNER)
    document.update(index_status="processing", index_lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert present(document)["index_status"] == "interrupted"
    assert present(document)["index_can_retry"] is True
    legacy = {name: value for name, value in document.items() if not name.startswith("index_")}
    assert present(legacy)["index_status"] == "pending"
    assert present(legacy)["index_can_retry"] is True


def test_recursive_redaction_preserves_only_fixed_index_identifiers():
    value = [{"entity": {"index_version": "aa13900001234bb", "content_hash": "cc110101199001011234dd",
                          "content": "手机号13900001234，身份证110101199001011234。",
                          "extra": {"content_hash_copy": "13900001234"}}}]
    safe = redact_value(value)
    assert safe[0]["entity"]["index_version"] == value[0]["entity"]["index_version"]
    assert safe[0]["entity"]["content_hash"] == value[0]["entity"]["content_hash"]
    assert "13900001234" not in safe[0]["entity"]["content"]
    assert "110101199001011234" not in safe[0]["entity"]["content"]
    assert safe[0]["entity"]["extra"]["content_hash_copy"] != "13900001234"


def test_milvus_redaction_roundtrip_preserves_content_and_manifest(setup_indexing, monkeypatch):
    components = setup_indexing
    source = "姓名：张小甲，手机号13900001234。\n\n如果回踩才考虑买入，今天未成交。"
    identifier = create_review(components, source)
    components.indexer.index(identifier, DEFAULT_OWNER)
    document, rows = assert_published(components, identifier)
    for row in rows:
        row["index_version"] = "aa13900001234bb"
    components.vectors.client.hybrid_search.return_value = [[
        {"id": row["id"], "distance": 1.0, "entity": row} for row in rows]]
    hits = components.vectors.search([1.0, 0.0], {1: 1.0}, DEFAULT_OWNER)[0]
    for hit in hits:
        entity = hit["entity"]
        assert entity["index_version"] == "aa13900001234bb"
        assert entity["content_hash"] == document["index_manifest"][hit["id"]] == digest(entity["content"])
        assert entity["content"] in components.embedded_texts
        assert redact_text(entity["content"]) == entity["content"]
        assert "13900001234" not in entity["content"]


@pytest.mark.parametrize("texts,embedded", [
    ([], {"dense": [], "sparse": []}),
    ([TEXT], {"dense": [], "sparse": []}),
    ([TEXT], {"dense": [[1.0]], "sparse": [{1: 1.0}]}),
    ([TEXT], {"dense": [[1.0, 0.0]], "sparse": [{}]}),
    (["x" * 12001], {"dense": [[1.0, 0.0]], "sparse": [{1: 1.0}]}),
])
def test_invalid_vectors_or_unretrievable_content_never_reach_upsert(setup_indexing, texts, embedded):
    components = setup_indexing
    with pytest.raises(ValueError):
        components.vectors.write(texts, embedded, DEFAULT_OWNER, "review", "version")
    components.vectors.client.upsert.assert_not_called()

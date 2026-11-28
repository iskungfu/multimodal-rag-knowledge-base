from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import mongomock
import pytest

from knowledge.review.indexing import ReviewIndexer
from knowledge.review.repository import ReviewRepository
from knowledge.service.review_service import ReviewService


OWNER = "default_user"
TEXT = "仅在回踩确认后才考虑买入。今天只做计划，没有成交。"


class MemoryVectors:
    collection = "memory_review_evidence"

    def __init__(self):
        self.hits = []

    def prepare(self):
        pass

    def write(self, texts, embedded, owner, review_id, version):
        manifest = {}
        for position, content in enumerate(texts):
            identifier = f"{review_id}:{version}:{position}"
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            manifest[identifier] = content_hash
            self.hits.append({"id": identifier, "entity": {
                "owner_id": owner, "doc_type": "review", "document_id": review_id,
                "review_id": review_id, "index_version": version, "content_hash": content_hash,
                "content": content, "title": "复盘片段",
            }})
        return manifest

    def delete_version(self, owner, review_id, version):
        self.hits = [hit for hit in self.hits if (
            hit["entity"]["owner_id"], hit["entity"]["review_id"], hit["entity"]["index_version"]
        ) != (owner, review_id, version)]

    def search(self, dense, sparse, owner, limit):
        return [deepcopy(self.hits[:limit])]


@pytest.fixture
def indexed_review():
    collection = mongomock.MongoClient(tz_aware=True).test.reviews
    repository = ReviewRepository(collection)
    identifier = str(uuid4())
    repository.create(identifier, OWNER, TEXT)
    vectors = MemoryVectors()
    clock = SimpleNamespace(now=datetime(2026, 11, 28, tzinfo=timezone.utc))
    embed = Mock(side_effect=lambda texts: {"dense": [[1, 0] for text in texts], "sparse": [{1: 1} for text in texts]})
    index = ReviewIndexer(repository, vectors, embed=embed, now=lambda: clock.now,
                          tokenizer=SimpleNamespace(encode=lambda text, **kwargs: list(text.encode("utf-8"))))
    return SimpleNamespace(index=index, repository=repository, vectors=vectors, clock=clock, embed=embed,
                           identifier=identifier, collection=collection)


def read_state(environment):
    return environment.index.describe(environment.repository.get(environment.identifier, OWNER))


def search(environment):
    return environment.index.search([1, 0], {1: 1}, OWNER)


def test_saved_text_only_becomes_citable_after_publication(indexed_review):
    env = indexed_review
    assert read_state(env)["index_status"] == "pending" and search(env) == []
    env.index.index(env.identifier, OWNER)
    assert read_state(env)["index_status"] == "completed"
    assert read_state(env)["index_can_retry"] is False
    evidence = search(env)
    assert evidence and all(TEXT in item["content"] for item in evidence)
    assert all(item["review_id"] == env.identifier and item["source"] == "review" for item in evidence)
    assert all(item["collection"] == env.vectors.collection for item in evidence)
    assert all("计划" in item["content"] and "没有成交" in item["content"] for item in evidence)
    assert not {"index_token", "index_manifest", "index_window", "index_lease_until"} & read_state(env).keys()


@pytest.mark.parametrize("field,value", [
    ("index_status", "processing"), ("index_version", "stale"), ("index_version", None),
    ("index_manifest", {}), ("owner_id", "another_owner"),
    ("original_text", ""), ("original_text", "x" * 10001),
])
def test_unpublished_or_invalid_records_are_not_citable(indexed_review, field, value):
    env = indexed_review
    env.index.index(env.identifier, OWNER)
    assert search(env)
    env.collection.update_one({"_id": env.identifier}, {"$set": {field: value}})
    assert search(env) == []


@pytest.mark.parametrize("field,value", [
    ("content", "被替换的行动"), ("content_hash", "wrong"), ("index_version", "stale"),
    ("index_version", None), ("review_id", "other-review"), ("document_id", "other-review"),
])
def test_corrupted_vector_hits_are_not_citable(indexed_review, field, value):
    env = indexed_review
    env.index.index(env.identifier, OWNER)
    assert search(env)
    for hit in env.vectors.hits:
        hit["entity"][field] = value
    assert search(env) == []


def test_unknown_chunk_is_not_citable(indexed_review):
    env = indexed_review
    env.index.index(env.identifier, OWNER)
    for hit in env.vectors.hits:
        hit["id"] = "unpublished-chunk"
    assert search(env) == []


@pytest.mark.parametrize("field,value", [("owner_id", "another_owner"), ("doc_type", "personal")])
def test_vector_scope_mismatch_fails_closed(indexed_review, field, value):
    env = indexed_review
    env.index.index(env.identifier, OWNER)
    env.vectors.hits[0]["entity"][field] = value
    with pytest.raises(ValueError):
        search(env)


def test_reextraction_revokes_citations_until_republished(indexed_review):
    env = indexed_review
    env.index.index(env.identifier, OWNER)
    assert search(env)
    env.repository.finish(env.identifier, OWNER, None, "model_unavailable")
    assert search(env) == []
    env.index.index(env.identifier, OWNER)
    assert search(env) and read_state(env)["extraction_status"] == "failed"
    assert all(TEXT in item["content"] for item in search(env))


def test_retry_status_agrees_with_actual_attempts_and_preserves_text(indexed_review):
    env = indexed_review
    env.embed.side_effect = RuntimeError("private model error")
    service = ReviewService(env.repository, indexer=env.index)
    for attempt in range(3):
        assert service.get(env.identifier)["index_can_retry"] is True
        env.index.index(env.identifier, OWNER)
        state = service.get(env.identifier)
        assert state["index_status"] == "failed" and state["original_text"] == TEXT
        assert state["index_can_retry"] is False and search(env) == []
        env.index.index(env.identifier, OWNER)
        assert env.embed.call_count == attempt + 1
        env.clock.now += timedelta(seconds=30)
    assert service.get(env.identifier)["index_can_retry"] is False
    env.clock.now = service.get(env.identifier)["index_retry_after"]
    assert service.get(env.identifier)["index_can_retry"] is True
    env.index.index(env.identifier, OWNER)
    assert env.embed.call_count == 4


def test_interrupted_worker_becomes_retryable_at_lease_expiry(indexed_review, monkeypatch):
    env = indexed_review
    finish = env.repository.finish_index
    confirm = env.repository.index_is_unpublished
    monkeypatch.setattr(env.repository, "finish_index", Mock(side_effect=RuntimeError("unconfirmed")))
    monkeypatch.setattr(env.repository, "index_is_unpublished", Mock(side_effect=RuntimeError("unconfirmed")))
    env.index.index(env.identifier, OWNER)
    assert read_state(env)["index_status"] == "processing"
    assert read_state(env)["index_can_retry"] is False and search(env) == []
    assert env.vectors.hits
    env.clock.now += timedelta(seconds=179)
    env.index.index(env.identifier, OWNER)
    assert env.embed.call_count == 1
    env.clock.now += timedelta(seconds=1)
    assert read_state(env)["index_status"] == "interrupted" and read_state(env)["index_can_retry"] is True
    monkeypatch.setattr(env.repository, "finish_index", finish)
    monkeypatch.setattr(env.repository, "index_is_unpublished", confirm)
    env.index.index(env.identifier, OWNER)
    assert env.embed.call_count == 2 and search(env)

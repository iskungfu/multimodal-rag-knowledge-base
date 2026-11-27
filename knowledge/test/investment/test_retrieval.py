from types import SimpleNamespace

import mongomock
import pytest
from pymilvus import DataType

from knowledge.investment.retrieval import InvestmentRetriever
from knowledge.review.indexing import digest


@pytest.fixture
def setup_retriever(monkeypatch):
    from knowledge.utils import milvus_util
    fields = [{"name": name, "type": DataType.VARCHAR} for name in ("owner_id", "doc_type", "document_id")]
    client = SimpleNamespace(has_collection=lambda *args, **kwargs: True,
                             describe_collection=lambda *args, **kwargs: {"fields": fields})
    db = mongomock.MongoClient().test
    hits = []
    calls = []
    def search(*args, **kwargs):
        calls.append(kwargs)
        return [hits]
    monkeypatch.setattr(milvus_util, "execute_hybrid_search_query", search)
    retriever = InvestmentRetriever(db.documents, db.reviews, client,
        embed=lambda texts: {"dense": [[1, 0]], "sparse": [{1: 1}]},
        collections={"personal": "personal", "knowledge": "knowledge", "review": "review"})
    return retriever, db, hits, calls


def hit(kind, owner=None):
    content = "若条件成立才执行，当前不确认触发。"
    return {"id": "123", "entity": {"content": content, "title": "自建测试", "document_id": "doc",
        "owner_id": owner or ("public" if kind == "knowledge" else "default_user"), "doc_type": kind,
        "review_id": "doc", "index_version": "version", "content_hash": digest(content)}}


@pytest.mark.parametrize("kind", ["personal", "knowledge", "review"])
def test_owner_boundary_is_checked_after_query(setup_retriever, kind):
    retriever, database, hits, calls = setup_retriever
    hits.append(hit(kind, "other_owner"))
    with pytest.raises(ValueError):
        retriever.search(kind, [1, 0], {1: 1})
    assert calls[0]["owner_id"] == ("public" if kind == "knowledge" else "default_user")
    assert calls[0]["doc_type"] == kind


def test_personal_must_match_completed_document(setup_retriever):
    retriever, database, hits, calls = setup_retriever
    hits.append(hit("personal"))
    assert not retriever.search("personal", [1, 0], {1: 1})
    database.documents.insert_one({"_id": "doc", "owner_id": "default_user", "doc_type": "personal", "status": "processing"})
    assert not retriever.search("personal", [1, 0], {1: 1})
    database.documents.update_one({"_id": "doc"}, {"$set": {"status": "completed"}})
    assert retriever.search("personal", [1, 0], {1: 1})[0]["document_id"] == "doc"


@pytest.mark.parametrize("corruption", ["status", "version", "id", "hash", "content", "owner"])
def test_review_requires_published_manifest(setup_retriever, corruption):
    retriever, database, hits, calls = setup_retriever
    row = hit("review")
    hits.append(row)
    record = {"_id": "doc", "owner_id": "default_user", "doc_type": "review", "index_status": "completed",
              "original_text": "计划买入，但未成交。",
              "index_version": "version", "index_manifest": {"123": digest(row["entity"]["content"])}}
    database.reviews.insert_one(record)
    assert retriever.search("review", [1, 0], {1: 1})
    if corruption in {"status", "version", "owner"}:
        key, value = {"status": ("index_status", "processing"), "version": ("index_version", "new"), "owner": ("owner_id", "other")}[corruption]
        database.reviews.update_one({"_id": "doc"}, {"$set": {key: value}})
    elif corruption == "id":
        row["id"] = "456"
    elif corruption == "hash":
        row["entity"]["content_hash"] = "invalid"
    else:
        row["entity"]["content"] = "篡改内容"
    assert not retriever.search("review", [1, 0], {1: 1})


def test_legacy_private_collection_never_falls_back(setup_retriever):
    retriever, database, hits, calls = setup_retriever
    retriever.client.describe_collection = lambda *args, **kwargs: {"fields": [{"name": "item_name"}]}
    with pytest.raises(ValueError):
        retriever.search("personal", [1, 0], {1: 1})
    assert not calls

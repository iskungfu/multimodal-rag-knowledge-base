from types import SimpleNamespace
from unittest.mock import Mock

import mongomock
import pytest
from pymilvus import DataType

from knowledge.investment.retrieval import InvestmentRetriever
from knowledge.review.indexing import ReviewIndexer


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
    retriever = InvestmentRetriever(db.documents, Mock(spec=ReviewIndexer), client,
        embed=lambda texts: {"dense": [[1, 0]], "sparse": [{1: 1}]},
        collections={"personal": "personal", "knowledge": "knowledge", "review": "review"})
    return retriever, db, hits, calls


def hit(kind, owner=None):
    content = "若条件成立才执行，当前不确认触发。"
    return {"id": "123", "entity": {"content": content, "title": "自建测试", "document_id": "doc",
        "owner_id": owner or ("public" if kind == "knowledge" else "default_user"), "doc_type": kind}}


@pytest.mark.parametrize("kind", ["personal", "knowledge"])
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


def test_review_route_uses_index_interface_without_reading_publication_fields(setup_retriever):
    retriever, database, hits, calls = setup_retriever
    evidence = {"source": "review", "content": "复盘完整原文：计划买入但未成交。", "review_id": "review"}
    retriever.review_index.search.return_value = [evidence]
    assert retriever.search("review", [1, 0], {1: 1}) == [evidence]
    retriever.review_index.search.assert_called_once_with([1, 0], {1: 1}, "default_user")
    assert not calls and not database.list_collection_names()


def test_review_failure_preserves_other_routes(setup_retriever):
    retriever, database, hits, calls = setup_retriever
    retriever.review_index.search.side_effect = RuntimeError("private storage failure")
    routes = retriever.retrieve("我的复盘")
    assert routes["review"] == {"status": "failed", "items": []}
    assert all(routes[kind] == {"status": "empty", "items": []} for kind in ("personal", "knowledge"))


def test_legacy_private_collection_never_falls_back(setup_retriever):
    retriever, database, hits, calls = setup_retriever
    retriever.client.describe_collection = lambda *args, **kwargs: {"fields": [{"name": "item_name"}]}
    with pytest.raises(ValueError):
        retriever.search("personal", [1, 0], {1: 1})
    assert not calls


def test_mode_filters_before_search_and_rejects_out_of_mode_hit(setup_retriever, monkeypatch):
    retriever, database, hits, calls = setup_retriever
    from knowledge.utils import milvus_util
    requests = []
    original = milvus_util.create_hybrid_search_requests
    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        requests.extend(result)
        return result
    monkeypatch.setattr(milvus_util, 'create_hybrid_search_requests', capture)
    hits.append(hit('personal'))
    database.documents.insert_one({'_id': 'doc', 'owner_id': 'default_user', 'doc_type': 'personal', 'status': 'completed'})
    assert retriever.search('personal', [1, 0], {1: 1}, document_ids=['doc'])
    assert len(requests) == 2 and all('document_id in ["doc"]' in request.expr for request in requests)
    with pytest.raises(ValueError):
        retriever.search('personal', [1, 0], {1: 1}, document_ids=['different'])
    assert retriever.search('personal', [1, 0], {1: 1}, document_ids=[]) == []


def test_public_summary_search_does_not_query_private_routes(setup_retriever):
    retriever, database, hits, calls = setup_retriever
    hits.append(hit('knowledge'))
    routes = retriever.retrieve('投资知识', kinds=('knowledge',))
    assert list(routes) == ['knowledge'] and routes['knowledge']['items'][0]['owner_id'] == 'public'
    assert len(calls) == 1 and calls[0]['doc_type'] == 'knowledge'
    retriever.review_index.search.assert_not_called()

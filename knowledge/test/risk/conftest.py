"""单元/API 测试默认隔离历史存储；真实集成显式替换为测试库独立集合。"""
import mongomock
import pytest


@pytest.fixture(autouse=True)
def history_collection(monkeypatch):
    monkeypatch.setattr('knowledge.personalization.settings.current_settings', lambda: {})
    from knowledge.utils import mongo_history_util
    collection = mongomock.MongoClient().test.chat_history
    monkeypatch.setattr(mongo_history_util, "_get_collection", lambda: collection)
    from knowledge.investment.retrieval import InvestmentRetriever
    monkeypatch.setattr(InvestmentRetriever, "retrieve", lambda self, query: {
        kind: {"status": "empty", "items": []} for kind in ("personal", "knowledge", "review")
    })
    return collection

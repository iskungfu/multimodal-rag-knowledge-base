"""每路独立失败；Milvus范围过滤后再次核验来源和Mongo可见状态。"""
import os

from knowledge.review.indexing import digest, embed_texts
from knowledge.review.privacy import redact_value

OWNER = "default_user"
KINDS = ("personal", "knowledge", "review")


def source_key(document):
    return (document.get("source"), document.get("collection"),
            document.get("review_id") or document.get("document_id"), str(document.get("chunk_id")))


def fuse(routes, rank_constant=60, limit=18):
    scores, documents = {}, {}
    for kind in KINDS:
        seen = set()
        for rank, document in enumerate(routes.get(kind, {}).get("items", []), 1):
            key = source_key(document)
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0) + 1 / (rank_constant + rank)
            documents[key] = {**document, "score": scores[key]}
    return sorted(documents.values(), key=lambda doc: (-doc["score"], source_key(doc)))[:limit]


class InvestmentRetriever:
    def __init__(self, documents=None, reviews=None, client=None, embed=None, collections=None):
        self.documents, self.reviews, self.client = documents, reviews, client
        self.embed = embed or embed_texts
        self.collections = collections or {
            "personal": os.getenv("STRATEGY_COLLECTION", "strategy_chunks_v1"),
            "knowledge": os.getenv("CHUNKS_COLLECTION", ""),
            "review": os.getenv("REVIEW_COLLECTION", "review_chunks_v1"),
        }

    def retrieve(self, query):
        routes = {}
        try:
            embedded = self.embed([query])
            dense, sparse = embedded["dense"][0], embedded["sparse"][0]
        except Exception:
            return {kind: {"status": "failed", "items": []} for kind in KINDS}
        for kind in KINDS:
            try:
                items = self.search(kind, dense, sparse)
                routes[kind] = {"status": "retrieved" if items else "empty", "items": items}
            except Exception:
                routes[kind] = {"status": "failed", "items": []}
        return routes

    def search(self, kind, dense, sparse):
        from knowledge.utils.clients.storage_clients import StorageClients
        from knowledge.utils.milvus_util import create_hybrid_search_requests, execute_hybrid_search_query
        from knowledge.strategy.scope import scoped_fields, legacy_public_collection
        client = self.client if self.client is not None else StorageClients.get_milvus_client()
        collection = self.collections[kind]
        if not collection:
            raise ValueError("未配置公共知识库")
        if not client.has_collection(collection, timeout=10):
            return []
        description = client.describe_collection(collection, timeout=10)
        scoped = scoped_fields(description)
        legacy = kind == "knowledge" and legacy_public_collection(description, collection)
        if not scoped and not legacy:
            raise ValueError("检索库未建立隔离")
        repository = None
        if kind != "knowledge":
            repository = self.documents if kind == "personal" else self.reviews
            if repository is None:
                db = StorageClients.get_mongo_client()[os.getenv("MONGO_DB_NAME", "kb001")]
                repository = db.strategy_documents if kind == "personal" else db.trade_reviews
        fields = ["title", "content"]
        if scoped:
            fields += ["owner_id", "doc_type", "document_id"]
        if kind == "review":
            fields += ["review_id", "index_version", "content_hash"]
        hits = execute_hybrid_search_query(client, collection,
            create_hybrid_search_requests(dense, sparse, limit=16), limit=16,
            owner_id=OWNER if kind != "knowledge" else "public", doc_type=kind, output_fields=fields)
        items, seen = [], set()
        for hit in (hits[0] if hits else []):
            entity = hit.get("entity", {})
            if scoped and (entity.get("doc_type") != kind or entity.get("owner_id") != ("public" if kind == "knowledge" else OWNER)):
                raise ValueError("返回来源隔离校验失败")
            identifier = entity.get("document_id") or ""
            content = entity.get("content")
            if hit.get("id") is None or not isinstance(content, str) or not content.strip() or len(content) > 12000:
                continue
            if kind == "personal":
                if not identifier or not repository.find_one({"_id": identifier, "owner_id": OWNER,
                        "doc_type": "personal", "status": "completed"}, {"_id": 1}):
                    continue
            if kind == "review":
                if not identifier or entity.get("review_id") != identifier:
                    continue
                record = repository.find_one({"_id": identifier, "owner_id": OWNER, "doc_type": "review",
                    "index_status": "completed", "index_version": entity.get("index_version")}, {"index_manifest": 1, "original_text": 1})
                if not record or record.get("index_manifest", {}).get(str(hit["id"])) != digest(content) or entity.get("content_hash") != digest(content):
                    continue
                original = record.get("original_text")
                if not isinstance(original, str) or not original.strip() or len(original) > 10000:
                    continue
                content = "复盘完整原文（计划、挂单和未知不代表成交，请核对所有前提）：\n" + original
            item = redact_value({"source": kind, "collection": collection, "document_id": identifier,
                "review_id": identifier if kind == "review" else "", "chunk_id": str(hit["id"]),
                "owner_id": "public" if kind == "knowledge" else OWNER,
                "title": entity.get("title") or kind, "content": content})
            if kind == "review":
                item["vector_excerpt"] = redact_value(entity["content"])
            if source_key(item) not in seen:
                items.append(item)
                seen.add(source_key(item))
        return items[:6]

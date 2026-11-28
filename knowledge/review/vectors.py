"""review独立schema：确定性主键upsert，版本与owner/type双重隔离。"""
import json

from pymilvus import DataType

from knowledge.review.indexing import digest
from knowledge.strategy.scope import require_scoped_collection, IsolationError
from knowledge.utils.milvus_util import create_hybrid_search_requests, execute_hybrid_search_query


class ReviewVectors:
    def __init__(self, client, collection, dim=1024):
        self.client, self.collection, self.dim = client, collection, dim

    def validate(self):
        require_scoped_collection(self.client, self.collection)
        fields = {field["name"]: field for field in self.client.describe_collection(self.collection, timeout=10)["fields"]}
        if any(name not in fields or fields[name]["type"] != DataType.VARCHAR
               for name in ("id", "review_id", "index_version", "content_hash")):
            raise IsolationError("复盘collection版本schema不兼容")

    def prepare(self):
        if not self.client.has_collection(self.collection, timeout=10):
            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("id", DataType.VARCHAR, max_length=64, is_primary=True)
            for name in ("owner_id", "doc_type", "document_id", "review_id", "index_version", "content_hash"):
                schema.add_field(name, DataType.VARCHAR, max_length=256)
            for name in ("content", "title"):
                schema.add_field(name, DataType.VARCHAR, max_length=65535)
            schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=self.dim)
            schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
            indexes = self.client.prepare_index_params()
            indexes.add_index("dense_vector", index_type="AUTOINDEX", metric_type="COSINE")
            indexes.add_index("sparse_vector", index_type="SPARSE_INVERTED_INDEX", metric_type="IP")
            try:
                self.client.create_collection(self.collection, schema=schema, index_params=indexes, timeout=30)
            except Exception:
                if not self.client.has_collection(self.collection, timeout=10):
                    raise
        self.validate()

    def write(self, texts, embedded, owner, review_id, version):
        self.validate()
        if not owner or not review_id or not version or not texts or len(texts) != len(embedded["dense"]) or len(texts) != len(embedded["sparse"]):
            raise ValueError("复盘向量不完整")
        rows, manifest = [], {}
        for index, text in enumerate(texts):
            if not isinstance(text, str) or not text.strip() or len(text) > 12000 or len(text.encode("utf-8")) > 65535:
                raise ValueError("复盘片段超过检索内容预算")
            if len(embedded["dense"][index]) != self.dim or not embedded["sparse"][index]:
                raise ValueError("复盘向量不完整")
            identifier = digest(json.dumps([owner, review_id, version, index]))
            manifest[identifier] = digest(text)
            rows.append({"id": identifier, "owner_id": owner, "doc_type": "review", "document_id": review_id,
                         "review_id": review_id, "index_version": version, "content_hash": digest(text),
                         "title": "复盘原文" if text.startswith("复盘原文") else "复盘抽取摘要", "content": text,
                         "dense_vector": embedded["dense"][index], "sparse_vector": embedded["sparse"][index]})
        result = self.client.upsert(self.collection, rows, timeout=30)
        if result.get("upsert_count") != len(rows):
            raise RuntimeError("复盘向量未完整写入")
        return manifest

    @staticmethod
    def scope(owner, review_id):
        return f'owner_id == {json.dumps(owner)} and doc_type == "review" and review_id == {json.dumps(review_id)}'

    def delete_version(self, owner, review_id, version):
        self.validate()
        self.client.delete(self.collection, filter=self.scope(owner, review_id) +
                           f' and index_version == {json.dumps(version)}', timeout=15)

    def search(self, dense, sparse, owner, limit=8):
        if not self.client.has_collection(self.collection, timeout=10):
            return []
        self.validate()
        return execute_hybrid_search_query(self.client, self.collection,
            create_hybrid_search_requests(dense, sparse, limit=limit), limit=limit, owner_id=owner, doc_type="review",
            output_fields=["owner_id", "doc_type", "document_id", "review_id", "index_version", "content_hash", "title", "content"])

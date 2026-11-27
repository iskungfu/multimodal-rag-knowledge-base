"""共享策略 collection，复用现有 RAG schema/index/row builder。"""

import json

from knowledge.processor.import_processor.nodes.milvus_import_node import (
    _MilvusSchemaBuilder, _MilvusIndexBuilder, _MilvusInserter,
)
from knowledge.review.privacy import redact_value
from knowledge.strategy.scope import require_scoped_collection
from knowledge.utils.milvus_util import create_hybrid_search_requests, execute_hybrid_search_query


class StrategyVectors:
    def __init__(self, client, collection, dim=1024):
        if not collection:
            raise ValueError("必须配置策略 collection")
        self.client, self.collection, self.dim = client, collection, dim

    def prepare(self):
        if not self.client.has_collection(self.collection, timeout=10):
            schema = _MilvusSchemaBuilder.build_schema(self.client, self.dim)
            self.client.create_collection(
                self.collection, schema=schema,
                index_params=_MilvusIndexBuilder.build_index_params(self.client), timeout=30,
            )
        require_scoped_collection(self.client, self.collection)

    def insert(self, chunks, owner_id, document_id):
        if not owner_id or not document_id or not chunks:
            raise ValueError("个人策略缺少归属或切片")
        require_scoped_collection(self.client, self.collection)
        rows = _MilvusInserter._build_rows(chunks)
        for row in rows:
            if len(row["dense_vector"]) != self.dim or not row["sparse_vector"]:
                raise ValueError("策略向量不完整")
            for key in ("item_name", "title", "parent_title", "file_title", "content"):
                row[key] = redact_value(row[key])
            row.update(owner_id=owner_id, doc_type="personal", document_id=document_id)
        result = self.client.insert(self.collection, rows, timeout=30)
        if result.get("insert_count") != len(rows):
            raise RuntimeError("策略切片未完整入库")
        return len(rows)

    def delete_document(self, owner_id, document_id):
        require_scoped_collection(self.client, self.collection)
        self.client.delete(self.collection, filter=(
            f'owner_id == {json.dumps(owner_id)} and doc_type == "personal" '
            f'and document_id == {json.dumps(document_id)}'
        ), timeout=30)

    def search(self, dense_vector, sparse_vector, owner_id, limit=5):
        requests = create_hybrid_search_requests(dense_vector, sparse_vector, limit=limit)
        result = execute_hybrid_search_query(
            self.client, self.collection, requests, limit=limit,
            output_fields=["content", "title", "file_title", "owner_id", "doc_type", "document_id"],
            owner_id=owner_id, doc_type="personal",
        )
        return redact_value(result)

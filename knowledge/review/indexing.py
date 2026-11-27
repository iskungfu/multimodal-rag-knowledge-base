"""复盘索引的独立生命周期；Mongo成功发布的版本才可被召回。"""
import hashlib
import json
import os
from datetime import datetime, timezone

from knowledge.review.privacy import redact_text, redact_value


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def index_texts(document):
    original = redact_text(document["original_text"])
    # 不生成第二份推测摘要，明确保留抽取状态、计划/未知/冲突等原有语义。
    extraction = document.get("extraction")
    summary = json.dumps({"extraction_status": document.get("extraction_status", "pending"),
                          "extraction": redact_value(extraction)}, ensure_ascii=False, separators=(",", ":"))
    # 摘要过大时保留原文，避免重复evidence撑爆单个Milvus VARCHAR。
    if len(summary.encode("utf-8")) > 60000:
        summary = "抽取摘要过长，请核对完整复盘原文；不得推断已成交或持仓。"
    return ["复盘原文（计划、挂单和未知不等于成交）：\n" + original,
            "复盘抽取摘要（可能有误，请与原文核对）：\n" + summary]


def embed_texts(texts):
    from knowledge.utils.clients.ai_clients import AIClients
    from knowledge.utils.embedding_util import generate_bge_m3_hybrid_vectors
    return generate_bge_m3_hybrid_vectors(AIClients.get_bge_m3_client(), texts)


class ReviewIndexer:
    def __init__(self, repository, vectors=None, embed=None, now=None):
        self.repository, self.vectors = repository, vectors
        self.embed = embed or embed_texts
        self.now = now or (lambda: datetime.now(timezone.utc))

    def index(self, review_id, owner):
        claim = self.repository.claim_index(review_id, owner, self.now())
        if claim is None:
            return
        vectors = self.vectors
        try:
            if vectors is None:
                from knowledge.review.vectors import ReviewVectors
                from knowledge.utils.clients.storage_clients import StorageClients
                vectors = ReviewVectors(StorageClients.get_milvus_client(),
                                        os.getenv("REVIEW_COLLECTION", "review_chunks_v1"))
            texts = index_texts(claim)
            embedded = self.embed(texts)
            vectors.prepare()
            # 仅清理本记录旧的不可见版本；过期worker即使再写入也无法发布。
            if claim.get("previous_index_token"):
                vectors.delete_version(owner, review_id, claim["previous_index_token"])
            manifest = vectors.write(texts, embedded, owner, review_id, claim["index_token"])
            if not self.repository.finish_index(claim, self.now(), manifest):
                vectors.delete_version(owner, review_id, claim["index_token"])
        except Exception:
            # 原文及抽取已独立提交。存储或模型异常不回显、不回滚。
            try:
                self.repository.finish_index(claim, self.now())
            except Exception:
                pass  # 租约到期可恢复，不以数据库故障覆盖已保存文本。
            if vectors is not None:
                try:
                    vectors.delete_version(owner, review_id, claim["index_token"])
                except Exception:
                    pass

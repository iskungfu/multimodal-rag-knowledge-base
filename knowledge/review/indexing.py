"""复盘索引的独立生命周期；Mongo成功发布的版本才可被召回。"""
import hashlib
import json
import os
from datetime import datetime, timezone

from knowledge.review.privacy import redact_text, redact_value
from knowledge.review.publication import index_state


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def index_texts(document, tokenizer, max_tokens):
    original = redact_text(document["original_text"])
    original_prefix = "复盘原文（仅片段，须核对全文前提；计划、挂单和未知不等于成交，不得推断持仓或标的）：\n"
    summary_prefix = "复盘抽取摘要（仅片段，可能有误，须核对全文；计划、未知及冲突不等于成交或持仓）：\n"
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("无效的复盘 token 预算")

    def fits(text):
        return len(text) <= 12000 and len(text.encode("utf-8")) <= 65535 and len(tokenizer.encode(
            text, add_special_tokens=True, truncation=False)) <= max_tokens

    def chunks(content, prefix):
        offset = 0
        while offset < len(content):
            lower, upper = 1, min(len(content) - offset, 12000 - len(prefix))
            length = 0
            while lower <= upper:
                middle = (lower + upper) // 2
                candidate = redact_text(prefix + content[offset:offset + middle])
                if fits(candidate):
                    length = middle
                    lower = middle + 1
                else:
                    upper = middle - 1
            if not length:
                raise ValueError("复盘上下文与单个字符超过向量 token 预算")
            if offset + length < len(content):
                window = content[offset:offset + length]
                boundary = max((window.rfind(separator) + len(separator)
                                for separator in ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ")
                                if separator in window), default=0)
                if boundary >= length // 2 and boundary > 0:
                    candidate = redact_text(prefix + content[offset:offset + boundary])
                    if fits(candidate):
                        length = boundary
            yield redact_text(prefix + content[offset:offset + length])
            offset += length

    texts = list(chunks(original, original_prefix))
    extraction = redact_value(document.get("extraction"))
    status = document.get("extraction_status", "pending")
    summaries = [{"extraction_status": status, "extraction": extraction}]
    if extraction is not None:
        summaries = [{"extraction_status": status, "event": event} for event in extraction.get("events", [])]
        summaries.append({"extraction_status": status, "lesson": extraction.get("lesson"),
                          "emotions": extraction.get("emotions", [])})
    for summary in summaries:
        prefix = summary_prefix
        if "event" in summary:
            event = summary["event"]
            prefix += f'事件状态：{event.get("status", "unknown")}；名称代码冲突：{event.get("identity_conflict", False)}。\n'
        texts.extend(chunks(json.dumps(summary, ensure_ascii=False, separators=(",", ":")), prefix))
    return texts


def embed_texts(texts):
    from knowledge.utils.clients.ai_clients import AIClients
    from knowledge.utils.embedding_util import generate_bge_m3_hybrid_vectors
    return generate_bge_m3_hybrid_vectors(AIClients.get_bge_m3_client(), texts)


class ReviewIndexer:
    def __init__(self, repository=None, vectors=None, embed=None, now=None, tokenizer=None, max_tokens=None,
                 collection=None):
        self.repository, self.vectors = repository, vectors
        self.embed = embed or embed_texts
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.tokenizer, self.max_tokens = tokenizer, max_tokens
        self.collection = collection if collection is not None else os.getenv("REVIEW_COLLECTION", "review_chunks_v1")

    def describe(self, document):
        state = index_state(document, self.now())
        hidden = {"index_token", "index_manifest", "index_window", "index_lease_until"}
        result = {key: value for key, value in document.items() if key not in hidden}
        result.update(index_status=state.status, index_retry_after=state.retry_after, index_can_retry=state.can_retry)
        return result

    def _repository(self):
        if self.repository is None:
            from knowledge.review.repository import ReviewRepository
            from knowledge.utils.clients.storage_clients import StorageClients
            database = StorageClients.get_mongo_client()[os.getenv("MONGO_DB_NAME", "kb001")]
            self.repository = ReviewRepository(database.trade_reviews)
        return self.repository

    def _vectors(self):
        if self.vectors is None:
            from knowledge.review.vectors import ReviewVectors
            from knowledge.utils.clients.storage_clients import StorageClients
            self.vectors = ReviewVectors(StorageClients.get_milvus_client(), self.collection)
        return self.vectors

    def search(self, dense, sparse, owner, limit=6):
        vectors = self._vectors()
        hits = vectors.search(dense, sparse, owner, limit=16)
        items, seen = [], set()
        for hit in (hits[0] if hits else []):
            entity = hit.get("entity", {})
            if entity.get("doc_type") != "review" or entity.get("owner_id") != owner:
                raise ValueError("返回复盘来源隔离校验失败")
            identifier = entity.get("document_id")
            content = entity.get("content")
            if not identifier or entity.get("review_id") != identifier or hit.get("id") is None:
                continue
            if not isinstance(content, str) or not content.strip() or len(content) > 12000:
                continue
            record = self._repository().published(identifier, owner, entity.get("index_version"))
            content_hash = digest(content)
            if (not record or record.get("index_manifest", {}).get(str(hit["id"])) != content_hash
                    or entity.get("content_hash") != content_hash):
                continue
            original = record.get("original_text")
            if not isinstance(original, str) or not original.strip() or len(original) > 10000:
                continue
            key = (identifier, str(hit["id"]))
            if key in seen:
                continue
            items.append(redact_value({
                "source": "review", "collection": vectors.collection, "document_id": identifier,
                "review_id": identifier, "chunk_id": str(hit["id"]), "owner_id": owner,
                "title": entity.get("title") or "review", "vector_excerpt": content,
                "content": "复盘完整原文（计划、挂单和未知不代表成交，请核对所有前提）：\n" + original,
            }))
            seen.add(key)
        return items[:limit]

    def token_budget(self):
        if self.tokenizer is not None:
            return self.tokenizer, self.max_tokens if self.max_tokens is not None else 512
        from knowledge.utils.clients.ai_clients import AIClients
        client = AIClients.get_bge_m3_client()
        limit = client._encode_config.get("max_length") or client.model.passage_max_length
        if self.max_tokens is not None:
            limit = min(limit, self.max_tokens)
        return client.model.tokenizer, limit

    def discard_unpublished(self, claim, vectors):
        can_delete = False
        try:
            can_delete = self.repository.finish_index(claim, self.now())
        except Exception:
            pass
        if not can_delete:
            try:
                can_delete = self.repository.index_is_unpublished(claim)
            except Exception:
                return
        if can_delete and vectors is not None:
            try:
                vectors.delete_version(claim["owner_id"], claim["_id"], claim["index_token"])
            except Exception:
                pass

    def index(self, review_id, owner):
        claim = self._repository().claim_index(review_id, owner, self.now())
        if claim is None:
            return
        vectors = self.vectors
        try:
            vectors = self._vectors()
            tokenizer, max_tokens = self.token_budget()
            texts = index_texts(claim, tokenizer, max_tokens)
            embedded = self.embed(texts)
            vectors.prepare()
            for version in {claim.get("previous_index_token"), claim.get("previous_index_version")} - {None}:
                vectors.delete_version(owner, review_id, version)
            manifest = vectors.write(texts, embedded, owner, review_id, claim["index_token"])
            if not self.repository.finish_index(claim, self.now(), manifest):
                self.discard_unpublished(claim, vectors)
        except Exception:
            self.discard_unpublished(claim, vectors)

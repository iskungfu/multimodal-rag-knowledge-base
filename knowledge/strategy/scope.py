"""Milvus 查询边界：默认公共知识；个人数据必须同时限定 owner 和类型。"""

import json
import os

from pymilvus import DataType


class IsolationError(RuntimeError):
    pass


def scoped_fields(description):
    fields = {field["name"]: field for field in description["fields"]}
    return all(name in fields and fields[name]["type"] == DataType.VARCHAR
               for name in ("owner_id", "doc_type", "document_id"))


def require_scoped_collection(client, collection):
    if not scoped_fields(client.describe_collection(collection, timeout=10)):
        raise IsolationError("collection 缺少隔离字段，请配置新的策略 collection；不会修改旧 collection")


def retrieval_filter(client, collection, owner_id=None, doc_type="knowledge"):
    description = client.describe_collection(collection, timeout=10)
    if scoped_fields(description):
        if doc_type == "knowledge":
            return 'doc_type == "knowledge" and owner_id == "public"'
        if doc_type not in {"personal", "review"} or not owner_id:
            raise IsolationError("个人检索必须指定用户和文档类型")
        return f"doc_type == {json.dumps(doc_type)} and owner_id == {json.dumps(owner_id)}"

    # 旧数据只有在管理员明确声明为公共、且为固定 schema 时才允许检索。
    # 绝不把带有部分隔离字段或动态字段的 collection 猜测成公共数据。
    fields = {field["name"] for field in description["fields"]}
    approved = {name.strip() for name in os.getenv("LEGACY_PUBLIC_COLLECTIONS", "").split(",") if name.strip()}
    if (doc_type == "knowledge" and collection in approved
            and not description.get("enable_dynamic_field", False)
            and not fields.intersection({"owner_id", "doc_type", "document_id"})):
        # 旧商品索引和切片都有 item_name；排除无归属项，禁止无条件搜索。
        if "item_name" in fields:
            return 'item_name != ""'
    raise IsolationError("collection 未建立隔离字段；旧公共数据需显式配置 LEGACY_PUBLIC_COLLECTIONS")

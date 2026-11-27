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


def legacy_public_collection(description, collection):
    fields = {field["name"] for field in description["fields"]}
    if description.get("enable_dynamic_field", False) or fields.intersection({"owner_id", "doc_type", "document_id"}):
        return False
    approved = {name.strip() for name in os.getenv("LEGACY_PUBLIC_COLLECTIONS", "").split(",") if name.strip()}
    if collection in approved and "item_name" in fields:
        return True
    legacy_schemas = {
        os.getenv("CHUNKS_COLLECTION"): {"id", "item_name", "title", "parent_title", "file_title", "content", "dense_vector", "sparse_vector"},
        os.getenv("ITEM_NAME_COLLECTION"): {"pk", "file_title", "item_name", "dense_vector", "sparse_vector"},
    }
    return bool(collection) and fields == legacy_schemas.get(collection)


def public_rows_for_collection(client, collection, rows):
    if any(row.get("owner_id") != "public" or row.get("doc_type") != "knowledge" for row in rows):
        raise IsolationError("公共导入不能写入个人数据")
    description = client.describe_collection(collection, timeout=10)
    if scoped_fields(description):
        return rows
    if legacy_public_collection(description, collection):
        return [{key: value for key, value in row.items() if key not in {"owner_id", "doc_type", "document_id"}} for row in rows]
    raise IsolationError("公共 collection 的 schema 未被识别；不会修改现有数据")


def retrieval_filter(client, collection, owner_id=None, doc_type="knowledge"):
    description = client.describe_collection(collection, timeout=10)
    if scoped_fields(description):
        if doc_type == "knowledge":
            return 'doc_type == "knowledge" and owner_id == "public"'
        if doc_type not in {"personal", "review"} or not owner_id:
            raise IsolationError("个人检索必须指定用户和文档类型")
        return f"doc_type == {json.dumps(doc_type)} and owner_id == {json.dumps(owner_id)}"

    if doc_type == "knowledge" and legacy_public_collection(description, collection):
        return 'item_name != ""'
    raise IsolationError("collection 未建立隔离字段；旧公共数据需显式配置 LEGACY_PUBLIC_COLLECTIONS")

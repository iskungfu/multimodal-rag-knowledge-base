"""
Milvus向量入库节点

业务流程:
    1. 参数校验
       1.1 从state中获取chunks,校验是否为空、类型是否是list
       1.2 遍历chunks,只保留同时带有稠密向量与稀疏向量的chunk(没有向量的chunk跳过,不阻塞其它chunk入库)
       1.3 validated_chunks为空说明所有chunk都没有向量,直接抛EmbeddingError
       1.4 取稠密向量的长度作为dim,创建Collection时用于指定向量维度
    2. 向量入库
       2.1 创建Milvus客户端: 客户端不可用则抛MilvusError(入库是本节点的核心职责,不能静默跳过)
       2.2 创建Collection: Collection不存在时,用_MilvusSchemaBuilder构建Schema、
           用_MilvusIndexBuilder构建索引参数,然后调用create_collection创建
       2.3 插入数据: 用_MilvusInserter把chunks组装成只含Schema字段的数据行写入Milvus,
           并把Milvus生成的自增主键回填到每个chunk的chunk_id上
    3. 回填state: 把带chunk_id的chunks写回state["chunks"],供后续节点使用
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from pymilvus import DataType, MilvusClient

from knowledge.processor.import_processor.base import BaseNode
from knowledge.processor.import_processor.exceptions import (
    ConfigurationError,
    EmbeddingError,
    MilvusError,
    StateFieldError,
)
from knowledge.processor.import_processor.state import ImportGraphState
from knowledge.utils.clients.storage_clients import StorageClients


class _MilvusSchemaBuilder:
    """构建chunks集合的Schema(标量字段 + 稠密/稀疏向量字段)"""

    @classmethod
    def build_schema(cls, milvus_client: MilvusClient, dim: int):
        #1. 创建Schema
        schema = milvus_client.create_schema()
        #2. 添加字段
        #2.1 主键:自增,由Milvus生成,插入后回填到chunk的chunk_id上
        schema.add_field(
            field_name="id",
            datatype=DataType.INT64,
            auto_id=True,
            is_primary=True
        )
        #2.2 标量字段:检索结果展示与过滤使用
        schema.add_field(
            field_name="item_name",
            datatype=DataType.VARCHAR,
            max_length=65535
        )
        schema.add_field(
            field_name="title",
            datatype=DataType.VARCHAR,
            max_length=65535
        )
        schema.add_field(
            field_name="parent_title",
            datatype=DataType.VARCHAR,
            max_length=65535
        )
        schema.add_field(
            field_name="file_title",
            datatype=DataType.VARCHAR,
            max_length=65535
        )
        schema.add_field(
            field_name="content",
            datatype=DataType.VARCHAR,
            max_length=65535
        )
        #2.3 向量字段:稠密向量维度由dim决定,稀疏向量维度由BGE-M3的词表决定,不需要指定
        schema.add_field(
            field_name="dense_vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=dim
        )
        schema.add_field(
            field_name="sparse_vector",
            datatype=DataType.SPARSE_FLOAT_VECTOR
        )
        #3. 返回Schema
        return schema


class _MilvusIndexBuilder:
    """构建稠密/稀疏向量字段的索引参数"""

    @classmethod
    def build_index_params(cls, milvus_client: MilvusClient):
        #1. 创建index_params
        index_params = milvus_client.prepare_index_params()
        #2. 添加稠密向量索引
        index_params.add_index(
            field_name="dense_vector",
            index_name="dense_vector_index",
            index_type="AUTOINDEX",
            metric_type="COSINE"
        )
        #3. 添加稀疏向量索引
        index_params.add_index(
            field_name="sparse_vector",
            index_name="sparse_vector_index",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP"
        )
        #4. 返回索引参数
        return index_params


class _MilvusInserter:
    """把chunks组装成Collection要求的数据行并写入Milvus"""

    @classmethod
    def insert(cls, milvus_client: MilvusClient, collection_name: str,
               chunks: List[Dict[str, Any]]) -> List[int]:
        #1. 组装要插入的数据行
        insert_rows = cls._build_rows(chunks)
        #2. 调用Milvus客户端的方法插入数据
        insert_results = milvus_client.insert(
            collection_name=collection_name,
            data=insert_rows,
            timeout=30
        )
        #3. 从插入结果中获取自增主键
        ids = insert_results.get("ids")
        #4. 把自增主键回填到各个chunk中(后续检索/更新时用于定位数据)
        for index, chunk in enumerate(chunks):
            chunk["chunk_id"] = ids[index]
        # print(chunks)

        return ids

    @classmethod
    def _build_rows(cls, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        组装要写入Milvus的数据行

        1. 只保留Collection Schema中存在的字段:chunk上的text、merged_titles等字段不在Schema中,
           直接整条写入会被Milvus拒绝,所以这里显式组装
        2. 切分节点产出的正文key是text,写入Milvus时映射到content字段(兼容直接带content的chunk)
        """
        insert_rows = []
        for chunk in chunks:
            insert_rows.append({
                "item_name": chunk.get("item_name", ""),
                "title": chunk.get("title", ""),
                "parent_title": chunk.get("parent_title", ""),
                "file_title": chunk.get("file_title", ""),
                "content": chunk.get("text") or chunk.get("content") or "",
                "dense_vector": chunk["dense_vector"],
                "sparse_vector": chunk["sparse_vector"],
            })

        return insert_rows


class MilvusImportNode(BaseNode):
    name = "milvus_import_node"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        #1. 参数校验
        validated_chunks, dim = self._validate_state(state)

        #2. 向量入库
        self.log_step(step_name="Step2", message=f"向量入库,共{len(validated_chunks)}个chunk,向量维度{dim}")
        self._insert_to_milvus(validated_chunks, dim)

        #3. 把带chunk_id的chunks回填到state中
        state["chunks"] = validated_chunks

        return state

    def _validate_state(self, state: ImportGraphState) -> Tuple[List[Dict[str, Any]], int]:
        """
        1. 获取并校验chunks(是否为空、类型是否是list)
        2. 遍历chunks,校验每一个chunk是否是dict,并判断是否包含稠密向量、稀疏向量,不包含则跳过
        3. 校验通过则添加到validated_chunks中
        4. 遍历完毕,validated_chunks为空说明所有chunk都没有向量,抛出异常
        5. 获取dim,就是稠密向量的长度
        6. 返回validated_chunks、dim
        """
        #1. 获取chunks
        chunks = state.get("chunks")
        #2. 校验chunks是否为空、类型是否是list
        if not chunks or not isinstance(chunks, list):
            self.logger.error("chunks为空,或者类型不是list")
            raise StateFieldError(node_name=self.name, field_name="chunks", expected_type=list)
        #3. 遍历chunks,过滤掉没有向量的chunk
        validated_chunks = []
        skipped_count = 0
        for chunk in chunks:
            #3.1 判断chunk的类型
            if not isinstance(chunk, dict):
                skipped_count += 1
                continue
            #3.2 获取稠密向量和稀疏向量,缺失任意一个都跳过该chunk
            dense_vector = chunk.get("dense_vector")
            sparse_vector = chunk.get("sparse_vector")
            if not dense_vector or not sparse_vector:
                skipped_count += 1
                continue
            #3.3 稀疏向量的key是token_id,chunks从json备份读取时会被转成字符串,这里统一还原成int
            chunk["sparse_vector"] = {int(token_id): weight for token_id, weight in sparse_vector.items()}
            #3.4 校验通过,加入validated_chunks
            validated_chunks.append(chunk)
        if skipped_count:
            self.logger.warning(f"有{skipped_count}个chunk没有向量,已跳过")
        #4. 判断validated_chunks是否为空
        if not validated_chunks:
            self.logger.error("所有chunk都没有向量,导入失败")
            raise EmbeddingError(node_name=self.name, message="所有chunk都没有向量,导入失败")
        #5. dim就是稠密向量的维度,其实就是数组的长度,创建Collection的时候需要指定
        dim = len(validated_chunks[0]["dense_vector"])
        #6. 返回validated_chunks、dim
        return validated_chunks, dim

    def _insert_to_milvus(self, validated_chunks: List[Dict[str, Any]], dim: int) -> None:
        #1. 创建Milvus客户端
        try:
            milvus_client = StorageClients.get_milvus_client()
        except Exception as e:
            self.logger.error(f"获取Milvus客户端失败,失败信息:{e}")
            raise MilvusError(node_name=self.name, message=f"获取Milvus客户端失败,{e}")
        #2. 从配置中获取存储chunk的Collection的名称
        collection_name = self.config.chunks_collection
        if not collection_name:
            self.logger.error("chunks_collection配置为空,请检查环境变量CHUNKS_COLLECTION")
            raise ConfigurationError(node_name=self.name, message="chunks_collection配置为空,请检查")
        #3. 判断Collection是否存在,如果不存在则创建Collection
        if not milvus_client.has_collection(collection_name):
            #3.1 创建Schema
            schema = _MilvusSchemaBuilder.build_schema(milvus_client, dim)
            #3.2 创建IndexParams
            index_params = _MilvusIndexBuilder.build_index_params(milvus_client)
            #3.3 创建Collection
            milvus_client.create_collection(
                collection_name=collection_name,
                schema=schema,
                index_params=index_params,
                timeout=30
            )
            self.logger.info(f"Collection:{collection_name}创建成功,稠密向量维度为{dim}")
        #4. 插入数据,并把自增主键回填到chunk中
        chunk_ids = _MilvusInserter.insert(milvus_client, collection_name, validated_chunks)
        self.logger.info(f"向量入库成功,共写入{len(chunk_ids)}条数据,Collection:{collection_name}")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    #1. 指定文档的工作目录(换机器/换文档时只需要改这两行)
    file_dir = r"D:\Project\shopkeeper-brain\knowledge\processor\import_processor\output_dir"
    file_title = "万用表RS-12的使用"
    doc_dir = Path(file_dir) / file_title

    #2. 从向量化节点备份的chunks_vector.json中读取chunks(与上游节点解耦,便于单独调试本节点)
    json_path = doc_dir / "chunks_vector.json"
    with open(json_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    #3. 构建state
    init_state = {
        "chunks": chunks
    }

    #4. 执行节点
    node = MilvusImportNode()
    result_state = node(init_state)
    print(f"向量入库完成,chunks数量={len(result_state.get('chunks', []))}")

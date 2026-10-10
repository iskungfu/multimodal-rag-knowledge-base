"""
文档向量化节点

业务流程:
    1. 参数校验
       1.1 从state中获取chunks
       1.2 校验chunks是否为空、类型是否是list
       1.3 遍历chunks,校验每一个chunk是否是dict类型
       1.4 校验通过后返回chunks
    2. 分批嵌入向量
       2.1 获取BGE-M3嵌入模型客户端(获取失败抛EmbeddingError)
       2.2 从配置中获取embedding_batch_size(一个批次最多嵌入多少条chunk),并校验配置合法性
       2.3 按batch_size把chunks分批,逐批调用_embedding_batch做批量嵌入
           2.3.1 组装待嵌入文本: 每个chunk取"item_name + 切片正文(text,兼容content字段)"
           2.3.2 调用公共工具generate_bge_m3_hybrid_vectors,一次产出稠密向量与稀疏向量
           2.3.3 按顺序把稠密向量、稀疏向量回填到chunk的dense_vector、sparse_vector字段
    3. 回填state: 用向量化后的chunks覆盖state["chunks"],供下游Milvus入库节点使用
    4. 备份: 把带向量的chunks备份成chunks_vector.json,便于后续节点单独测试(备份失败只告警,不影响主流程)
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from pymilvus.model.hybrid import BGEM3EmbeddingFunction

from knowledge.processor.import_processor.base import BaseNode
from knowledge.processor.import_processor.exceptions import (
    ConfigurationError,
    EmbeddingError,
    StateFieldError,
    ValidationError,
)
from knowledge.processor.import_processor.state import ImportGraphState
from knowledge.utils.clients.ai_clients import AIClients
from knowledge.utils.embedding_util import generate_bge_m3_hybrid_vectors


class BgeEmbeddingChunksNode(BaseNode):
    name = "bge_embedding_chunks_node"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        #1. 参数校验
        chunks = self._validate_state(state)
        #2. 分批对chunk做向量化(稠密向量 + 稀疏向量)
        self.log_step(step_name="Step2", message=f"开始向量化,共{len(chunks)}个chunk")
        final_chunks = self._embedding_chunks(chunks)
        #3. 将向量化后的chunks回填到state中
        state["chunks"] = final_chunks
        #4. 备份带向量的chunks,便于后续节点单独测试
        self._backup_chunks(final_chunks, state)

        return state

    def _validate_state(self, state: ImportGraphState) -> List[Dict[str, Any]]:
        """
        1. 从state中获取chunks
        2. 校验chunks是否为空，并且其类型是否是list
        3. 遍历chunks，校验每一个chunk是否是dict类型
        4. 返回chunks
        """
        #1. 从state中获取chunks
        chunks = state.get("chunks")
        #2. 校验chunks是否为空、类型是否为list
        if not chunks or not isinstance(chunks, list):
            self.logger.error("chunks参数错误")
            raise StateFieldError(node_name=self.name, field_name="chunks", expected_type=list)
        #3. 校验每一个chunk的类型
        for chunk in chunks:
            if not isinstance(chunk, dict):
                self.logger.error("chunk的类型不正确")
                raise ValidationError(node_name=self.name, message="chunk的类型不是dict")
        #4. 返回chunks
        return chunks

    def _embedding_chunks(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        #1. 获取嵌入模型对象(检索侧编码用户问题也复用这个客户端)
        try:
            embedding_client = AIClients.get_bge_m3_client()
        except (EnvironmentError, ConnectionError) as e:
            self.logger.error(f"获取BGE-M3嵌入模型对象失败,失败信息:{e}")
            raise EmbeddingError(node_name=self.name, message="获取BGE-M3嵌入模型对象失败")
        #2. 从配置中获取批次大小,也就是一批次最多嵌入多少条chunk
        batch_size = self.config.embedding_batch_size
        if not batch_size or batch_size <= 0:
            self.logger.error(f"embedding_batch_size={batch_size}配置错误,请检查")
            raise ConfigurationError(node_name=self.name, message=f"embedding_batch_size={batch_size}配置错误,请检查")
        #3. 声明final_chunks用于收集向量化后的chunk
        final_chunks = []
        #4. 计算chunks的总长度
        total = len(chunks)
        #5. 按批次遍历chunks,步长就是批次大小
        for index in range(0, total, batch_size):
            #5.1 获取当前批次的chunks
            batch_chunks = chunks[index:index + batch_size]
            #5.2 对当前批次做批量嵌入,并把结果收集到final_chunks中
            final_chunks.extend(self._embedding_batch(batch_chunks, embedding_client))
            self.logger.info(f"向量化进度:{min(index + batch_size, total)}/{total}")

        return final_chunks

    def _embedding_batch(self, batch_chunks: List[Dict[str, Any]],
                         embedding_client: BGEM3EmbeddingFunction) -> List[Dict[str, Any]]:
        #1. 组装待嵌入的文本: item_name + 切片正文(切分节点产出的是text,兼容content字段)
        embedding_documents = [
            f"{chunk.get('item_name', '')}\n{chunk.get('text') or chunk.get('content') or ''}"
            for chunk in batch_chunks
        ]
        #2. 调用公共工具批量生成混合向量(稠密 + 稀疏),检索侧编码用户问题也复用这个函数
        try:
            hybrid_vectors = generate_bge_m3_hybrid_vectors(embedding_client, embedding_documents)
        except (ValueError, RuntimeError) as e:
            self.logger.error(f"嵌入向量失败,失败信息:{e}")
            raise EmbeddingError(node_name=self.name, message="嵌入向量失败")
        #3. 按顺序把向量回填到当前批次的chunk中
        dense_vector_list = hybrid_vectors["dense"]
        sparse_vector_list = hybrid_vectors["sparse"]
        for index, chunk in enumerate(batch_chunks):
            chunk["dense_vector"] = dense_vector_list[index]
            chunk["sparse_vector"] = sparse_vector_list[index]
        #4. 返回当前批次的chunks
        return batch_chunks

    def _backup_chunks(self, final_chunks: List[Dict[str, Any]], state: ImportGraphState) -> None:
        """把带向量的chunks备份成json文件(方便后续节点单独测试)"""
        #1. 指定备份文件的路径
        #1.1 获取文件输出的目录,备份到file_dir/文档名/目录下
        md_path = state.get("md_path")
        file_dir = state.get("file_dir")
        if not md_path or not file_dir:
            self.logger.warning("md_path或file_dir为空,跳过chunks的备份")
            return
        md_path_obj = Path(md_path)
        file_dir_obj = Path(file_dir)
        backup_dir = file_dir_obj / md_path_obj.stem
        #1.2 判断该目录是否真实存在,如果不存在则创建目录
        backup_dir.mkdir(parents=True, exist_ok=True)
        #1.3 指定备份文件的路径
        backup_file_path = backup_dir / "chunks_vector.json"

        #2. 将chunks写入到备份文件中
        try:
            with open(backup_file_path, "w", encoding="utf-8") as f:
                json.dump(final_chunks, f, ensure_ascii=False, indent=4)
        except Exception as e:
            #备份失败只告警,不影响主流程
            self.logger.warning(f"{md_path_obj.stem}文件备份成chunks_vector.json失败,但是不影响主流程,失败信息:{e}")
            return
        self.logger.info(f"chunks备份成功,文件路径是{str(backup_file_path)}")


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

    #2. 从商品名识别节点备份的chunks_item_name.json中读取chunks(与上游节点解耦,便于单独调试本节点)
    json_path = doc_dir / "chunks_item_name.json"
    with open(json_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    #3. 构建state(md_path只用于确定chunks_vector.json的备份目录)
    init_state = {
        "file_dir": file_dir,
        "md_path": str(doc_dir / f"{file_title}.md"),
        "chunks": chunks
    }

    #4. 执行节点
    node = BgeEmbeddingChunksNode()
    result_state = node(init_state)
    print(f"向量化完成,chunks数量={len(result_state.get('chunks', []))}")

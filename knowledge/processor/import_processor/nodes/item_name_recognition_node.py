"""
商品名识别节点

业务流程:
    1. 参数校验: 校验file_title、chunks以及商品名识别相关配置
    2. 构建上下文: 从chunks中取前item_name_chunk_k个切片,拼成受item_name_chunk_size限制的上下文
    3. LLM提取商品名: 任何一步失败都降级使用file_title兜底
    4. 商品名向量化: 使用BGE-M3同时产出稠密向量与稀疏向量
    5. 向量入库: 将(file_title、item_name、稠密向量、稀疏向量)写入Milvus
    6. 回填: 把item_name回填到每个chunk以及state中
    7. 备份: 把带item_name的chunks备份成json,便于后续节点单独测试
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from pymilvus import CollectionSchema, DataType, MilvusClient

from knowledge.processor.import_processor.base import BaseNode
from knowledge.processor.import_processor.exceptions import EmbeddingError, MilvusError, StateFieldError
from knowledge.processor.import_processor.state import ImportGraphState
from knowledge.prompts.import_prompt import ITEM_NAME_SYSTEM_PROMPT, ITEM_NAME_USER_PROMPT_TEMPLATE
from knowledge.utils.clients.ai_clients import AIClients
from knowledge.utils.clients.storage_clients import StorageClients
from knowledge.utils.embedding_util import generate_bge_m3_hybrid_vectors


class ItemNameRecognitionNode(BaseNode):
    name = "item_name_recognition_node"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        #1. 参数校验
        file_title, chunks, item_name_chunk_k, item_name_chunk_size = self._validate_state(state)
        #2. 构建LLM的上下文
        self.log_step(step_name="Step2", message="构建商品名识别的上下文")
        item_name_context = self._build_item_name_context(
            file_title=file_title,
            chunks=chunks,
            item_name_chunk_k=item_name_chunk_k,
            item_name_chunk_size=item_name_chunk_size
        )
        # print(item_name_context)
        #3. 调用LLM提取商品名(失败则降级使用file_title)
        self.log_step(step_name="Step3", message="调用LLM提取商品名")
        item_name = self._recognition_item_name(item_name_context, file_title)
        # print(item_name)
        #4. 商品名向量化(稠密向量 + 稀疏向量)
        self.log_step(step_name="Step4", message="商品名向量化")
        dense_vector, sparse_vector = self._embedding_item_name(item_name)
        # print(dense_vector, sparse_vector)
        #5. 向量入库(Milvus)
        self.log_step(step_name="Step5", message="商品名向量入库")
        hybrid_vectors = {"dense": dense_vector, "sparse": sparse_vector}
        self._insert_to_milvus(file_title, item_name, hybrid_vectors)
        #6. 回填item_name到chunks与state
        self._fill_back(item_name, chunks, state)
        #7. 备份带item_name的chunks,便于后续节点单独测试
        self._backup_chunks(chunks, state)

        return state

    def _validate_state(self, state: ImportGraphState) -> Tuple[str, List[Dict[str, Any]], int, int]:
        #1. 获取file_title，作为兜底使用，当没有提取到商品名的时候，使用file_title作为商品名
        file_title = state.get("file_title")
        #2. 校验file_title是否为空,如果为空,则表示前面出现了问题,应该抛出异常
        if not file_title:
            self.logger.error("file_title is empty")
            raise StateFieldError(node_name=self.name, field_name="file_title", expected_type=str)

        #3. 获取chunks,也就是上一步拆分之后的结果,这个内容可以提供作为LLM的上下文信息,让LLM进行商品名的提取
        chunks = state.get("chunks")
        #4. 判断chunks是否为空，且其类型是否是list，如果不满足则抛出异常
        if not chunks or not isinstance(chunks, list):
            self.logger.error("chunks is empty or not a list")
            raise StateFieldError(node_name=self.name, field_name="chunks", expected_type=list)

        #5. 从config中获取item_name_chunk_k和item_name_chunk_size，分别表示商品名提取能使用的最大chunk数量，以及最大的长度
        item_name_chunk_k = self.config.item_name_chunk_k
        item_name_chunk_size = self.config.item_name_chunk_size
        #6. 判断item_name_chunk_k和item_name_chunk_size是否为空，并且是否大于0，如果不满足则抛出异常
        if not item_name_chunk_k or item_name_chunk_k <= 0:
            self.logger.error(f"item_name_chunk_k={item_name_chunk_k}配置错误,请检查")
            raise StateFieldError(node_name=self.name, field_name="item_name_chunk_k", expected_type=int)

        if not item_name_chunk_size or item_name_chunk_size <= 0:
            self.logger.error(f"item_name_chunk_size={item_name_chunk_size}配置错误,请检查")
            raise StateFieldError(node_name=self.name, field_name="item_name_chunk_size", expected_type=int)

        #7. 返回file_title、chunks、item_name_chunk_k、item_name_chunk_size
        return file_title, chunks, item_name_chunk_k, item_name_chunk_size

    def _build_item_name_context(self, file_title: str, chunks: List[Dict[str, Any]],
                                 item_name_chunk_k: int, item_name_chunk_size: int) -> str:
        """
        构建商品名识别的上下文

        1. 遍历每一个chunk，但是注意，最多只能遍历item_name_chunk_k个
           1. 定义`final_context=[]`用来接收最终的上下文信息
           2. 判断chunk如果不是dict类型，则跳过这个chunk
           3. 获取chunk的文本，并将文本进行组装，组装成`【切片】-index-content`的格式，并加入到final_context中
           4. 判断final_context的总长度是否超过了item_name_chunk_size,如果超过了,则break掉
        2. 使用"\n"拼接final_context中的内容。返回拼接后的内容
        """
        #1. 遍历chunks,最多遍历item_name_chunk_k个
        final_context = []
        total_length = 0
        for index, chunk in enumerate(chunks[:item_name_chunk_k]):
            #1.1 判断chunk的类型是否是dict,如果不是则跳过该chunk
            if not isinstance(chunk, dict):
                continue

            #1.2 获取chunk的文本(切分节点产出的是text,兼容content字段)并组装后存入final_context
            content = chunk.get("text") or chunk.get("content") or ""
            if not content:
                continue
            splice_context = f"【切片】-{index + 1}-{content}"
            final_context.append(splice_context)
            total_length += len(splice_context)
            #1.3 内容总长度超过上限则停止收集(先加入再判断,保证至少有一个切片作为上下文)
            if total_length > item_name_chunk_size:
                break

        context = "\n".join(final_context)
        self.logger.info(f"file_title={file_title},商品名识别上下文共{len(final_context)}个切片,长度{len(context)}")
        #2. 返回拼接后的上下文内容
        return context

    def _recognition_item_name(self, item_name_context: str, file_title: str) -> str:
        #1. 创建LLM客户端
        try:
            llm_client = AIClients.get_llm_client(False)
        except ConnectionError as e:
            self.logger.error(f"获取llm_client失败，失败信息为{e}")
            #兜底:使用file_title作为商品名
            return file_title

        #2. 构建系统消息
        system_message = SystemMessage(content=ITEM_NAME_SYSTEM_PROMPT)

        #3. 构建HumanMessage
        user_prompt = ITEM_NAME_USER_PROMPT_TEMPLATE.format(
            file_title=file_title,
            context=item_name_context
        )
        human_message = HumanMessage(content=user_prompt)

        #4. 执行llm
        try:
            resp = llm_client.invoke([system_message, human_message])
        except Exception as e:
            self.logger.warning(f"执行llm提取item_name失败,失败信息{e}")
            #兜底:使用file_title作为商品名
            return file_title

        #5. 解析响应数据
        llm_result = resp.content.strip()
        if not llm_result or llm_result.upper() in ("UNKNOW", "UNKNOWN"):
            self.logger.warning("执行llm提取item_name失败,大模型未识别出商品名")
            #兜底:使用file_title作为商品名
            return file_title

        self.logger.info(f"提取item_name成功,item_name={llm_result}")
        return llm_result

    def _embedding_item_name(self, item_name: str) -> Tuple[List[float], Dict[int, float]]:
        # 1. 获取嵌入模型对象
        try:
            embedding_client = AIClients.get_bge_m3_client()
        except (EnvironmentError, ConnectionError) as e:
            self.logger.error(f"获取嵌入模型对象失败,错误信息:{e}")
            raise EmbeddingError(node_name=self.name, message="获取嵌入模型对象失败")

        # 2. 调用公共工具生成混合向量(检索侧编码用户问题也复用这个函数)
        try:
            hybrid_vectors = generate_bge_m3_hybrid_vectors(embedding_client, [item_name])
        except (ValueError, RuntimeError) as e:
            self.logger.error(f"向量化失败,失败信息{e}")
            raise EmbeddingError(node_name=self.name, message="向量化失败")
        # 3. 本次只编码了一个文档,取第一个即可
        dense_vector = hybrid_vectors["dense"][0]
        sparse_vector = hybrid_vectors["sparse"][0]
        return dense_vector, sparse_vector

    def _insert_to_milvus(self, file_title: str, item_name: str, hybrid_vectors: dict):
        # 1. 取出待写入的向量
        # 注意:上游_embedding_item_name已经取出单条数据的向量,dense是List[float],sparse是Dict[token_id, weight],不能再取[0]
        dense_vector = hybrid_vectors.get("dense")
        sparse_vector = hybrid_vectors.get("sparse")
        if not dense_vector or not sparse_vector:
            self.logger.warning("稠密向量或者稀疏向量为空,跳过入库")
            return
        # 2. 创建Milvus客户端
        try:
            milvus_client = StorageClients.get_milvus_client()
        except Exception as e:
            self.logger.warning(f"获取Milvus客户端失败,{e}")
            return
        # 3. 判断存储item_name的Collection是否存在，如果不存在则创建Collection
        collection_name = self.config.item_name_collection
        if not milvus_client.has_collection(collection_name):
            # 3.1 创建表
            # 3.1.1 创建Schema
            schema = milvus_client.create_schema()
            schema.add_field(
                field_name="pk",
                datatype=DataType.INT64,
                auto_id=True,
                is_primary=True
            )
            schema.add_field(
                field_name="file_title",
                datatype=DataType.VARCHAR,
                max_length=2048
            )
            schema.add_field(
                field_name="item_name",
                datatype=DataType.VARCHAR,
                max_length=2048,
            )
            schema.add_field(
                field_name="dense_vector",
                datatype=DataType.FLOAT_VECTOR,
                # 维度以实际产出的稠密向量为准,避免与嵌入模型维度不一致(BGE-M3产出1024维)
                dim=len(dense_vector)
            )
            schema.add_field(
                field_name="sparse_vector",
                datatype=DataType.SPARSE_FLOAT_VECTOR
            )
            # 3.1.2 添加索引
            index_params = milvus_client.prepare_index_params()
            index_params.add_index(
                field_name="dense_vector",
                index_name="dense_vector_index",
                index_type="AUTOINDEX",
                metric_type="COSINE"
            )
            index_params.add_index(
                field_name="sparse_vector",
                index_name="sparse_vector_index",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP"
            )

            # 3.1.3 创建Collection
            milvus_client.create_collection(
                collection_name=collection_name,
                schema=schema,
                index_params=index_params
            )
        # 4. 插入数据
        # 4.1 构建要插入的数据行(file_title、item_name、稠密向量、稀疏向量)
        insert_data = {
            "file_title": file_title,
            "item_name": item_name,
            "dense_vector": dense_vector,
            "sparse_vector": sparse_vector
        }
        # 4.2 调用Milvus客户端的方法插入数据
        milvus_client.insert(
            collection_name=collection_name,
            data=insert_data,
            timeout=30
        )

    def _create_index_params(self, milvus_client: MilvusClient) -> Any:
        #返回类型是MilvusClient.prepare_index_params()产出的IndexParams对象
        #IndexParams定义在pymilvus.milvus_client.index子模块中,顶层并未导出,故此处标注为Any
        #1. 准备index_params
        index_params = milvus_client.prepare_index_params()
        #2. 添加稠密向量索引
        index_params.add_index(
            index_name="dense_vector_index",
            field_name="dense_vector",
            index_type="AUTOINDEX",
            metric_type="COSINE"
        )
        #3. 添加稀疏向量索引
        index_params.add_index(
            index_name="sparse_vector_index",
            field_name="sparse_vector",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP"
        )
        return index_params

    def _fill_back(self, item_name: str, chunks: List[Dict[str, Any]], state: ImportGraphState) -> None:
        """
        回填item_name

        目的:
        1. 将item_name回填到chunk中，方便下游其它模型使用
        2. 将item_name回填到state中，方便下游其它节点使用
        """
        #1. 遍历chunks，给每个chunk设置item_name
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            chunk["item_name"] = item_name
        #2. 给state设置item_name
        state["item_name"] = item_name
        state["chunks"]=chunks

    def _backup_chunks(self, chunks: List[Dict[str, Any]], state: ImportGraphState) -> None:
        """把带item_name的chunks备份成json文件(方便后续节点单独测试)"""
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
        #1.2 判断该目录是否真实存在，如果不存在则创建目录
        backup_dir.mkdir(parents=True, exist_ok=True)
        #1.3 指定备份文件的路径
        backup_file_path = backup_dir / "chunks_item_name.json"

        #2. 将chunks写入到备份文件中
        try:
            with open(backup_file_path, "w", encoding="utf-8") as f:
                json.dump(chunks, f, ensure_ascii=False, indent=4)
        except Exception as e:
            #备份失败只告警,不影响主流程
            self.logger.warning(f"{md_path_obj.stem}文件备份成chunks_item_name.json失败,但是不影响主流程,失败信息:{e}")
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

    #2. 从切分节点备份的chunks.json中读取chunks(与上游节点解耦,便于单独调试本节点)
    json_path = doc_dir / "chunks.json"
    with open(json_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    #3. 构建state(md_path只用于确定chunks_item_name.json的备份目录)
    init_state = {
        "file_title": file_title,
        "file_dir": file_dir,
        "md_path": str(doc_dir / f"{file_title}.md"),
        "chunks": chunks
    }

    #4. 执行节点
    node = ItemNameRecognitionNode()
    result_state = node(init_state)
    print(f"商品名识别完成,item_name={result_state.get('item_name')},chunks数量={len(result_state.get('chunks', []))}")

# 多模态RAG智能知识库

基于 RAG（检索增强生成）技术的企业级智能知识库系统，支持 PDF/Markdown 文档智能导入、混合向量检索、多路召回融合与流式问答。

## 项目简介

本项目针对垂直领域（电子产品手册、维修指南、技术文档等）构建了一套完整的智能知识库解决方案。通过 LangGraph 编排文档导入与查询两大工作流，结合 BGE-M3 混合向量检索、HyDE 假设文档检索、RRF 多路召回融合、BGE-Reranker 重排序等前沿技术，实现高精度的文档检索与智能问答。

## 核心功能

| 功能模块 | 描述 |
|---------|------|
| **文档智能导入** | 支持 PDF/Markdown 上传，自动解析、图片理解、文档切分、向量化入库 |
| **多模态理解** | VLM 视觉语言模型自动生成图片摘要，图片内容可被检索 |
| **混合向量检索** | BGE-M3 稠密向量（语义）+ 稀疏向量（关键词 BM25）双路检索 |
| **多路召回融合** | 向量检索 + HyDE 假设文档检索 + Web 搜索，RRF 算法融合排序 |
| **智能重排序** | BGE-Reranker-Large 交叉编码器重排序，断崖检测动态截断 |
| **流式问答** | SSE 服务端推送，逐字输出答案，支持多轮对话上下文 |

## 技术栈

### 后端框架
- **FastAPI** - 高性能异步 Web 框架
- **Uvicorn** - ASGI 服务器
- **LangGraph** - 有状态工作流编排引擎
- **LangChain** - LLM 应用开发框架

### AI / 机器学习
- **BGE-M3** - 混合向量嵌入模型（稠密 1024 维 + 稀疏 BM25）
- **BGE-Reranker-Large** - 交叉编码器重排序模型
- **OpenAI 兼容 API** - 支持 Qwen / GPT 等大语言模型
- **VLM 视觉语言模型** - 图片内容理解与摘要生成
- **MinerU** - PDF 智能解析（支持公式、表格、复杂排版）

### 数据存储
- **Milvus** - 向量数据库（支持混合检索）
- **MongoDB** - 文档数据库（对话历史存储）
- **MinIO** - 对象存储（文件与图片存储）

## 系统架构

### 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                        前端页面                           │
│  （文档导入页 / 智能问答页，HTML + JavaScript）            │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP / SSE
┌────────────────────▼────────────────────────────────────┐
│                     API 层（FastAPI）                     │
│  导入服务（:8000）          查询服务（:8001）              │
│  POST /upload              POST /query                   │
│  GET  /status/{task_id}    GET  /stream/{session_id}     │
│                           GET  /history/{session_id}     │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│                 Processor 层（LangGraph）                 │
│  ┌──────────────────┐      ┌──────────────────┐         │
│  │  文档导入工作流    │      │  智能查询工作流    │         │
│  │  Entry → PDF解析  │      │  商品名确认        │         │
│  │  → 图片处理       │      │  → 多路召回        │         │
│  │  → 文档切分       │      │    (向量/HyDE/Web)│         │
│  │  → 商品名识别     │      │  → RRF融合        │         │
│  │  → 向量化         │      │  → Rerank重排序   │         │
│  │  → Milvus入库     │      │  → 答案生成        │         │
│  └──────────────────┘      └──────────────────┘         │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│                    数据存储层                             │
│  Milvus（向量）  MongoDB（历史）  MinIO（文件/图片）       │
└─────────────────────────────────────────────────────────┘
```

### 文档导入流程

```
PDF/MD 文件
    │
    ▼
入口节点（文件校验、路由判断）
    │
    ├─ PDF → PDF转Markdown节点（MinerU API）
    │         │
    │         ▼
    └─────────→ 图片处理节点（VLM图片摘要 + MinIO上传）
                  │
                  ▼
            文档切分节点（按标题层级切分 + 过短合并）
                  │
                  ▼
            商品名识别节点（LLM提取 + 向量入库）
                  │
                  ▼
            切片向量化节点（BGE-M3 稠密+稀疏）
                  │
                  ▼
            Milvus向量入库节点
```

### 智能查询流程

```
用户问题
    │
    ▼
商品名确认节点（LLM识别 + 向量匹配）
    │
    ├── 已有答案 → 直接输出
    │
    └── 无答案 → 多路并行召回
              ├── 向量检索（BGE-M3 混合检索）
              ├── HyDE 检索（LLM生成假设文档再检索）
              └── Web 搜索（MCP协议）
                    │
                    ▼
              RRF 多路召回融合
                    │
                    ▼
              Rerank 重排序（断崖检测动态截断）
                    │
                    ▼
              答案生成（SSE 流式输出）
```

## 项目结构

```
knowledge/
├── api/                              # API 路由层
│   ├── import_api.py                # 导入服务接口（端口 8000）
│   └── __init__.py
│
├── core/                             # 核心配置
│   ├── deps.py                      # 依赖注入（单例管理）
│   ├── paths.py                     # 路径常量
│   └── __init__.py
│
├── processor/                        # 业务流程（LangGraph）
│   ├── import_processor/            # 文档导入流程
│   │   ├── main_graph.py           # 导入流程图定义
│   │   ├── state.py                # 状态类型定义
│   │   ├── base.py                 # 节点基类
│   │   ├── config.py               # 导入配置
│   │   ├── exceptions.py           # 自定义异常
│   │   └── nodes/                  # 处理节点
│   │       ├── entry_node.py              # 入口节点
│   │       ├── pdf_to_md_node.py          # PDF转Markdown
│   │       ├── md_image_node.py           # 图片处理与MinIO上传
│   │       ├── document_split_node.py     # 文档切分
│   │       ├── item_name_recognition_node.py  # 商品名识别
│   │       ├── bge_embedding_chunks_node.py    # 切片向量化
│   │       └── milvus_import_node.py      # 向量入库
│   │
│   └── query_processor/             # 智能查询流程
│       ├── main_graph.py           # 查询流程图定义
│       ├── state.py                # 状态类型定义
│       ├── base.py                 # 节点基类
│       ├── config.py               # 查询配置
│       ├── exceptions.py           # 自定义异常
│       └── nodes/                  # 处理节点
│           └── item_name_confirmed_node.py  # 商品名确认
│
├── prompts/                          # 提示词模板
│   ├── import_prompt.py            # 导入流程提示词
│   ├── query_prompt.py             # 查询流程提示词
│   └── __init__.py
│
├── schema/                           # 数据模型
│   ├── upload_schema.py            # 上传请求/响应模型
│   └── __init__.py
│
├── service/                          # 业务服务层
│   ├── file_process_service.py     # 文件处理服务
│   └── __init__.py
│
├── utils/                            # 工具函数库
│   ├── clients/                    # 外部服务客户端
│   │   ├── ai_clients.py          # AI模型客户端（LLM/VLM/BGE）
│   │   ├── storage_clients.py     # 存储客户端（Milvus/MinIO）
│   │   └── base.py                # 客户端基类
│   ├── embedding_util.py           # 向量嵌入工具
│   ├── markdown_util.py            # Markdown处理工具
│   ├── milvus_util.py              # Milvus混合检索工具
│   ├── task_util.py                # 任务状态管理
│   └── __init__.py
│
├── front/                            # 前端页面
│   └── import.html                 # 文档导入页面
│
├── test/                             # 测试代码
│
├── .env                              # 环境配置（不提交到仓库）
└── __init__.py
```

## 快速开始

### 环境要求

| 项目 | 要求 |
|------|------|
| Python | >= 3.10 |
| 内存 | >= 16GB（建议 32GB） |
| 显存 | >= 8GB（BGE-M3 + Reranker 本地推理，CPU 也可运行） |
| 存储 | >= 50GB（模型缓存） |

### 依赖服务

- **Milvus** - 向量数据库（>= 2.3）
- **MongoDB** - 文档数据库（>= 6.0）
- **MinIO** - 对象存储（>= RELEASE.2023）

推荐使用 Docker Compose 一键启动依赖服务：

```yaml
# docker-compose.yml 示例
version: '3.8'
services:
  milvus:
    image: milvusdb/milvus:v2.3.4
    ports:
      - "19530:19530"
    environment:
      - ETCD_ENDPOINTS=etcd:2379
  mongodb:
    image: mongo:6.0
    ports:
      - "27017:27017"
    environment:
      - MONGO_INITDB_ROOT_USERNAME=admin
      - MONGO_INITDB_ROOT_PASSWORD=password
  minio:
    image: minio/minio:latest
    ports:
      - "9000:9000"
      - "9001:9001"
    environment:
      - MINIO_ROOT_USER=minioadmin
      - MINIO_ROOT_PASSWORD=minioadmin
    command: server /data --console-address ":9001"
```

### 安装步骤

```bash
# 1. 克隆项目
git clone <repository-url>
cd <project-name>

# 2. 创建虚拟环境
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac
# source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入实际的 API Key、模型路径、服务地址等
```

### 下载模型

```bash
# BGE-M3 嵌入模型（通过 ModelScope）
python -c "from modelscope import snapshot_download; snapshot_download('BAAI/bge-m3')"

# BGE-Reranker-Large 重排序模型
python -c "from modelscope import snapshot_download; snapshot_download('BAAI/bge-reranker-large')"
```

### 运行服务

```bash
# 启动导入服务（端口 8000）
cd knowledge
uvicorn api.import_api:create_app --host 0.0.0.0 --port 8000 --factory --reload

# 启动查询服务（端口 8001）
# uvicorn api.query_api:create_app --host 0.0.0.0 --port 8001 --factory --reload
```

**访问地址：**
- 文档导入页面：http://localhost:8000/front/import.html
- API 文档（Swagger）：http://localhost:8000/docs

## API 接口

### 导入服务（端口 8000）

| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/upload` | 上传文件（支持 PDF/Markdown） |
| GET | `/status/{task_id}` | 查询任务处理状态 |
| GET | `/hello` | 健康检查 |

### 查询服务（端口 8001）

| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/query` | 发起查询（流式/非流式） |
| GET | `/stream/{session_id}` | SSE 流式获取答案 |
| GET | `/history/{session_id}` | 获取会话历史 |
| DELETE | `/history/{session_id}` | 清除会话历史 |

## 核心技术亮点

### 1. BGE-M3 混合向量检索

同时使用稠密向量（语义理解）和稀疏向量（关键词精确匹配），通过 Milvus 的 `WeightedRanker` 进行加权融合，兼顾语义相关性和关键词匹配。

```python
# 混合检索示例
search_requests = create_hybrid_search_requests(
    dense_vector=query_dense,
    sparse_vector=query_sparse,
    dense_params={"metric_type": "COSINE"},
    sparse_params={"metric_type": "IP"},
    limit=10
)
results = execute_hybrid_search_query(
    milvus_client=client,
    collection_name="kb_chunks",
    search_requests=search_requests,
    ranker_weights=(0.5, 0.5)  # 50% 稠密 + 50% 稀疏
)
```

### 2. 多路召回 + RRF 融合

实现向量检索、HyDE 假设文档检索、Web 搜索三路并行召回，使用 RRF（Reciprocal Rank Fusion）算法进行无参数融合，避免人工调权。

### 3. Reranker 重排序 + 断崖检测

使用 BGE-Reranker-Large 交叉编码器对召回结果精排，通过断崖检测算法自动判断截断点，动态选择最相关的文档片段，避免无关上下文干扰答案生成。

### 4. LangGraph 有状态工作流

使用 LangGraph 构建导入和查询两大有状态工作流，支持条件路由、并行执行、状态传递，流程清晰可扩展。

### 5. 多模态文档理解

通过 VLM 视觉语言模型自动为文档中的图片生成中文摘要，并上传至 MinIO 对象存储，使图片内容也能被语义检索到。

## License

MIT

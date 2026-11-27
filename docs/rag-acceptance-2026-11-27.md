# RAG 实际验收（2026-11-27）

本次验收使用 Windows 本机既有 `myenv` Python 环境和 CPU 上的 BGE-M3、bge-reranker-large，连接 VMware 中的 MongoDB、Milvus 2.5.5、MinIO，以及已配置的应用 LLM、MinerU、Web MCP。FastAPI 路由通过 TestClient 执行，后台任务和全部业务节点真实运行，没有替换模型、向量或存储调用。

## 已修复的问题

- 本机 pymilvus 2.6.6 返回的 `Hit` 是 `UserDict`，原 RRF 节点的 `dict` 类型检查把真实检索命中全部丢弃。改为接受 `Mapping`，保留本地证据并正常融合。
- 流式回答未保存到任务结果，导致 `/status` 返回空答案。现与非流式一致保存完整答案，`final` 同时携带完整答案。
- SSE 发出 `final` 后仍无限等待；现关闭生成器并释放队列。
- 查询图异常未返回错误且流式请求不会终止；现返回通用错误消息和终止事件，前端显示错误，不把底层凭据或连接信息返回给用户。

## 验收范围

自建“青禾 QH-731 复盘记录器”虚构说明书，包含只能从文档获得的校验口令“青鹭7391”和回看窗口“17个交易日”。未使用私人交易记录或书籍。

- Markdown 导入、切分、真实 BGE-M3 稠密/稀疏向量、Milvus gRPC 写入及检索。
- 单页中文 PDF 经真实 MinerU 转 Markdown，再进入同一入库流程；PDF 样本已渲染检查。
- MinIO 原文件备份。
- 普通向量召回、HyDE 召回、Web MCP、RRF 融合、真实 reranker 评分和模型回答；断言本地证据存在、评分非空、答案命中两个独有事实。
- 非流式回答、SSE 增量与唯一终止事件、流式轮询答案、HTTP 流关闭和队列清理。
- MongoDB 会话写入、读取、删除。

每次运行都生成唯一的 MongoDB 测试库、两个 Milvus collection 和一个 MinIO bucket，最后清理这些资源与本次上传产生的本地临时文件。原有业务资源不在清理范围。

## 复现

在项目根目录、已安装 `requirements.txt` 及 Milvus model 依赖的 Python 环境执行：

```powershell
python -m unittest discover -s knowledge/test/query -p test_rag_regressions.py
python scripts/verify_rag.py
python scripts/verify_rag.py --stream --pdf
python scripts/verify_rag.py --legacy
```

PDF 验收额外需要 `reportlab`、`pymupdf`；API 验收需要 `httpx`。配置读取 `knowledge/.env`，本地模型应已存在。详细报告保存于被 Git 忽略的 `.sandcastle/logs/rag_accept_*.json`，原始服务日志可能包含服务地址，留在本机。

PDF/SSE 验收记录：`rag_accept_2c41fa2a7d30`，全部通过，耗时 93.56 秒；实际召回数量为向量 1、HyDE 1、Web 3、RRF 1、rerank 3。5 项针对 Mapping 命中、答案保存、SSE 关闭和失败响应的回归测试通过。

Markdown/非流式复测记录：`rag_accept_cf7d1764f8d1`，导入、真实向量、RRF、reranker、本地证据、两个独有事实、会话存取和资源清理全部通过。

## 边界

本次证明既有 RAG 的短文本与单页纯文字 PDF 链路可运行。未验收扫描书籍、图片问答、长书籍批量导入、性能压测或浏览器交互。现有查询仍依赖商品/实体名称确认；金融策略的 personal/knowledge/review 三路隔离、权限、脱敏与免责声明属于后续 Issue，不因此被标记为完成。

Sandcastle 已按最小允许列表获得应用 LLM 配置，并在真实 bwrap 沙箱通过应用模型响应检查。#3 的后续实际验收记录见 `.sandcastle/results/issue-3.md`。

## #4 合入成果分支后的回归

2026-11-27，个人策略导入实现后再次执行完整公共 RAG：

- `rag_accept_89f2ed43c6ae.json`：新 schema，PDF/SSE，21 项检查通过，86.00 秒。
- `rag_accept_399fd7d7b563.json`：独立构造原版固定 schema，Markdown/非流式，16 项检查通过，74.91 秒；导入和检索正常，schema 保持原样。

两次均实际调用 BGE-M3、Milvus、MinIO、RRF、reranker、Web MCP、应用 LLM 与 MongoDB，PDF 另经 MinerU。全部独立测试资源已清理。个人策略的导入、脱敏、隔离及浏览器验收记录见 `.sandcastle/results/issue-4.md`；投资助手三路召回仍属于 #7。

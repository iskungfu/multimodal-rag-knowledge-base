# 个人策略导入（Issue #4）

导入服务 `8000` 的 `/front/import.html` 支持个人策略 PDF、Markdown，以及直接输入文字。服务端固定 `owner_id=default_user`、`doc_type=personal`；没有登录或多用户认证。多用户隔离测试验证的是存储/服务边界，不能把公开部署的这个单用户应用当作认证系统。

## 接口与处理流程

- `POST /strategies/upload`：multipart `file`，支持 `.pdf/.md/.markdown/.txt`，上限 50 MB，文字为 UTF-8。文件名只用于脱敏后的展示，磁盘路径使用随机临时目录和固定 `input.pdf/input.md`。
- `POST /strategies/text`：JSON `{ "title": "个人策略", "text": "只在复盘后考虑新的交易。" }`。正文上限 200000 字符；不接受额外的 `owner_id/doc_type` 字段。
- 返回 HTTP 202 和 `task_id`，表示任务已接收。`GET /strategies/{task_id}` 返回 `processing/completed/failed`、错误提示、切片数、进度；`GET /strategies` 返回当前用户最近 20 个任务。刷新页面后仍能查看持久状态。
- 两个服务的旧 `/status/{task_id}` 都不能绕过个人状态接口。其他用户或其他类型的记录返回 404。
- MongoDB 新集合 `strategy_documents` 保存脱敏标题、文件名、归属和任务状态；正文切片存入 Milvus。
- 个人策略使用独立 LangGraph，复用 `EntryNode → PdfToMdNode（仅 PDF）→ DocumentSplitNode → BgeEmbeddingChunksNode`，在切分前执行 `StrategyPrivacyNode`，最后写入有隔离字段的 collection。不调用商品名识别，也不调用商品名确认。BGE-M3 保留现有稠密/稀疏向量流程。
- 投资问答、RRF三路召回及回答生成已由后续 #7 接入，见 `docs/personalized-investment-assistant.md`。内部 `StrategyVectors.search(..., owner_id)` 为受限召回基础。

## collection 兼容与配置

个人策略默认写入共享 `strategy_chunks_v1`，可用 `STRATEGY_COLLECTION` 指定新名称。所有用户共享这个 collection，由 `owner_id`、`doc_type`、`document_id` 三个显式 VARCHAR 字段隔离，不为每位用户创建 collection。

新建的通用切片/商品索引也包含这些字段，通用导入固定 `owner_id=public/doc_type=knowledge`。所有现有 Milvus 混合检索入口统一经过过滤工具；默认只能召回 `knowledge/public`，个人检索同时要求指定用户与类型。稠密和稀疏请求都附加范围条件，不能借商品名过滤或无过滤请求绕过。读取采用 Strong 一致性。

旧 collection 不删除、不清空、不自动改 schema，也不自动把未知归属的数据视作公共知识：

1. `CHUNKS_COLLECTION`、`ITEM_NAME_COLLECTION` 指定的已有公共库，在完整匹配原版固定 schema 且关闭动态字段时，继续支持通用导入和查询。公共写入自动省略旧 schema 没有的归属字段，不要求重建或迁移。个人策略仍必须使用具备完整隔离字段的 collection，不能回退到旧库。
2. 对额外的、已确认全部为公共资料的旧库，可将名称以逗号分隔加入 `LEGACY_PUBLIC_COLLECTIONS`。仅接受没有任何隔离字段、关闭动态字段、具有 `item_name` 的固定 schema；查询附加 `item_name != ""` 条件，公共写入只接受 `knowledge/public`。
3. 含部分隔离字段、动态字段或未知 schema 的旧库继续拒绝访问。显式允许列表也不能绕过前两项结构检查，个人读取和写入永不回退到旧公共库。

兼容逻辑只识别服务端配置的旧公共集合，不将任意 collection 当作公共知识。实现中没有自动迁移或删除业务数据的代码。`scripts/verify_rag.py --legacy` 会创建独立的旧 schema 测试集合，验证原有导入、真实问答和 schema 保留。

## 脱敏与文件生命周期

复用 `knowledge/review/privacy.py` 的 ADR-0005 规则，处理手机、身份证、银行卡、带标签的姓名、持仓/交易总金额；识别 Markdown 格式化标签、纵横表格及 HTML 表格，覆盖表头含敏感值、数据行省略边缘竖线或缺尾列的情况，保留买卖单价。任务、用户、文档等系统标识不会被内容脱敏规则改写。Markdown/直接文字在写临时文件前脱敏，标题和展示文件名同样处理。解析后的 PDF 正文在切分和向量化前脱敏，Milvus 写入与返回、MongoDB 返回再次脱敏。校验错误不回显原始输入，解析错误不输出响应正文或带签名 URL。

PDF 无法先用文本规则处理原始像素：原件暂存于系统临时目录的随机私有目录（Linux 权限 0700），通过现有 MinerU 接口发送给已配置解析服务。ZIP、解析 Markdown、图片同样仅位于这个任务目录；检查解压路径和大小限制。原图不上传 MinIO、不生成公开 URL，也不作为策略知识片段；Markdown 图片引用被省略。策略图不生成切片/向量 JSON 备份。

成功与失败均在 `finally` 清理整个任务目录，向量写入失败按 owner + document 删除本次切片；清理或状态保存失败显示明确错误。**这里没有声称 PDF 原件/解析中间文件已脱敏**。本期有意不采用 ADR-0006 提议的“MinIO 存策略 PDF”：当前没有私有原件下载和像素脱敏能力，持久备份原件与 ADR-0005 写入前脱敏目标存在冲突，因此本轮只保存脱敏后的文本切片。MinerU 远端保留策略由服务提供方决定，本应用不承诺删除远端文件。

规则脱敏不是任意自然语言姓名识别：没有姓名/户名/我叫等标记的名字可能无法识别，不承诺清除文档中的全部个人信息。进程被强制终止时 `finally` 不执行，可能留下 `shopkeeper-strategy-*` 临时目录和 `processing` 状态；运维应在确认任务已停止后清理这些目录并人工核对状态。本期不新增持久任务队列或后台自动恢复。

## 沙箱验证与主机验收

Linux 沙箱使用 `/tmp/shopkeeper-venv`，没有下载 BGE、Chromium 或 GPU 包。最小测试依赖：

```bash
python3 -m venv /tmp/shopkeeper-venv
/tmp/shopkeeper-venv/bin/python -m pip install pytest mongomock fastapi httpx python-multipart python-dotenv pymongo pymilvus minio langgraph langchain-openai langchain-text-splitters beautifulsoup4 requests uvicorn
/tmp/shopkeeper-venv/bin/python -m pytest -q -p no:cacheprovider knowledge/test/strategy knowledge/test/query/test_rag_regressions.py knowledge/test/market/test_market.py knowledge/test/review/test_review.py
/tmp/shopkeeper-venv/bin/python scripts/verify_strategy_import.py
```

独立探针只向 `AFK_TEST_DB_NAME` 内的随机集合、随机 Milvus collection 和随机 MinIO bucket 写入自建数据，结束后清理；不删除整个 MongoDB 数据库。默认模式的向量为二维固定测试向量，验证的是实际存储与过滤，**不代表运行过 BGE-M3**。MinIO 探针验证独立 bucket 的读写和清理，不是 PDF 原件备份或脱敏验收。

主机需使用已有 Python 环境、本地 BGE-M3 和连接参数；脚本设置 `HF_HUB_OFFLINE=1`，不下载模型。`--host` 加载已存在的 `knowledge/.env`，进程环境优先；`AFK_TEST_DB_NAME` 必须指向独立测试库。

```powershell
python scripts/verify_strategy_import.py --host
python scripts/verify_strategy_import.py --host --pdf
```

`--host` 执行真实 API → LangGraph → 本地 BGE-M3 → Milvus，检查实际向量、召回、用户隔离、脱敏和持久状态。`--pdf` 额外用 reportlab 在内存中构造无私人信息 PDF 并调用真实 MinerU，需要主机已有 MinerU 配置和 `reportlab`。探针只输出 PASS/FAIL、异常类型和不含凭据的 JSON 报告，默认不写原始服务异常。

2026-11-27 已完成主机浏览器补验：三种输入均完成真实导入，刷新保留脱敏任务列表；503 故障后输入保留，重试成功；390px 布局无水平溢出。实际 BGE-M3、MinerU PDF、用户间召回隔离与新旧 schema 的完整 RAG 问答均已补验。详细记录见 `.sandcastle/results/issue-4.md`。

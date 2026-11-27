# Issue #4：用户策略文档导入

状态：完成（completed；实现与沙箱验证完成，完整 RAG 待主机验收）

日期：2026-11-27（Asia/Shanghai）

任务：https://github.com/iskungfu/multimodal-rag-knowledge-base/issues/4

父规格：https://github.com/iskungfu/multimodal-rag-knowledge-base/issues/1

## 选择依据

读取了 AGENTS.md、GLOSSARY.md、docs/agents/、基线审阅、相关 ADR、父规格及 #4 正文。没有采用原型关于 RAG 全流程已经验证的旧断言。

选择前逐一查询 #2–#7 的原生依赖：

```bash
gh api repos/iskungfu/multimodal-rag-knowledge-base/issues/<number>/dependencies/blocked_by
```

#2、#3、#4 返回空列表；当前分支已有 #2、#3 完成记录，因此跳过。#5、#6 的 blocker #2 仍为 OPEN；#7 的 blocker #3、#5 仍为 OPEN，全部跳过。本轮仅实现 #4，不把 #1 当实施任务。#4 正文无 blocker；实际代码已有导入图、MinerU 解析、文档切分、BGE-M3 混合向量和 Milvus 存储，因此可复用，无需依赖尚未实现的投资问答。

## 实现

- 导入页面增加个人策略用途、文字输入、最近任务列表；API 新增 `/strategies/upload`、`/strategies/text`、`/strategies`、`/strategies/{task_id}`。
- 服务端固定 `default_user/personal`，忽略 multipart 中伪造的归属，JSON 额外字段拒绝；存储/列表/状态和召回均过滤归属。旧导入/查询状态接口不能读取个人任务。
- 独立个人策略 LangGraph 复用现有 Entry、PDF 解析、切分、BGE-M3 节点，切分前脱敏，不走商品名识别或确认；本轮不实现 #7 三路召回或投资问答。
- 默认使用共享 `strategy_chunks_v1`，显式保存 `owner_id/doc_type/document_id`。通用新 schema 同样包含隔离字段并固定写为 `public/knowledge`。所有现有混合检索入口统一追加范围条件，覆盖稠密和稀疏请求，返回再次脱敏。
- 旧 collection 不修改、不清空。缺少隔离字段拒绝写入和默认读取；经管理员确认全为公共资料后，可用 `LEGACY_PUBLIC_COLLECTIONS` 显式启用固定旧 schema 的只读兼容，仍附加非空商品名条件。动态或部分隔离 schema 不允许回退。此兼容配置需主机确认，不能把任意旧数据默认为公共。
- MongoDB `strategy_documents` 保存脱敏元数据与持久状态。失败提供明确提示；本任务切片按 owner + document 清理，临时目录在成功/失败后删除。
- 复用 ADR-0005 脱敏规则处理正文、标题、文件名及返回；参数校验错误不回显原输入。PDF 原件和解析 ZIP/图片仅临时保存；没有原件 MinIO 备份、公开图片 URL 或策略切片 JSON 备份。MinerU 的日志移除响应正文/签名 URL，增加超时和 ZIP 路径/大小检查。
- BGE 和 MCP 依赖延迟导入，使策略单元与存储验证可在没有大模型依赖的环境中运行；实际主机 BGE 路径保留。

详细接口、旧库配置、原件边界、主机命令见 `docs/strategy-import.md`。

## 真实执行的验证

按要求使用 Linux 文件系统的 `/tmp/shopkeeper-venv`；未在工作树创建虚拟环境或链接，未安装大型模型、GPU 依赖或浏览器，未修改 `AFK_STORAGE_PYTHON`。

最终单元/API/回归命令：

```bash
/tmp/shopkeeper-venv/bin/python -m pytest -q -p no:cacheprovider knowledge/test/strategy knowledge/test/query/test_rag_regressions.py knowledge/test/market/test_market.py knowledge/test/review/test_review.py
```

实际结果：**102 passed in 5.11s**，没有 skip。涵盖策略格式输入与真实图编排/切分、两用户隔离、归属伪造、正文/标题/文件名/返回脱敏、非法文件/校验错误、解析/嵌入/入库/元数据失败、临时文件清理、旧 schema 拒绝和显式兼容、双向量过滤、公共检索与原有 RAG/SSE 行为，以及已有行情/复盘单元回归。单元中的 BGE、PDF 外部解析和存储使用替身，不将其记为实际模型验收。

真实服务独立探针：

```bash
/tmp/shopkeeper-venv/bin/python scripts/verify_strategy_import.py
```

最终报告：`.sandcastle/logs/strategy_test_2aaf16f43ee947cf.json`，**19/19 checks PASS**，全部清理成功。此前一次相同探针也通过。

- MongoDB：实际连接、按用户读取/更新隔离、失败状态持久化。
- Milvus：真实 gRPC schema 创建、混合向量写入/检索、两个用户隔离、恶意 owner 字符串无越权、写入/返回脱敏、通用搜索排除 personal、正常 knowledge 回归、跨用户删除无效。
- 旧 Milvus collection：个人写入拒绝、默认读取拒绝、显式公共只读兼容、schema 及原有标记数据完整保留。
- MinIO：独立 bucket 的认证创建、对象读写和清理；此检查仅验证存储服务，不表示策略原件归档。
- MongoDB 仅使用 `AFK_TEST_DB_NAME` 内随机集合；Milvus 使用两份随机测试 collection；MinIO 使用随机 bucket。所有资源在 `finally` 清理，没有删除整个测试数据库、改写或清空业务资源。
- 默认存储探针采用固定二维测试向量，**未调用 BGE-M3**。没有用 mock 结果代替上述真实存储检查。
- 前端两个改动 JavaScript 通过 Node.js module 语法检查；`git diff --check` 通过；敏感值扫描与最终 diff 检查通过。

## 待主机验收与边界

- **待主机验收**：实际本地 BGE-M3 向量化、MinerU PDF 解析、浏览器交互、完整原有 RAG 问答回归。沙箱缺少本地模型、MinerU 凭据和浏览器，没有声称已运行这些流程。
- 主机入口：`python scripts/verify_strategy_import.py --host`；额外 PDF：`python scripts/verify_strategy_import.py --host --pdf`。使用主机已有环境和本地模型，保持 `AFK_TEST_DB_NAME` 隔离；脚本强制离线模型加载并清理独立测试资源。
- 未调用 LLM 抽取，个人策略导入无需抽取结构化交易字段；本轮未重跑 #3 的真实模型验收。
- 规则脱敏不能可靠识别无标记人名；PDF 原始像素未脱敏，MinerU 接收原件，其远端保留策略不由本程序控制。本期暂不采用 ADR-0006 中的 PDF 原件 MinIO 归档提议，以避免将未脱敏原件当作已脱敏持久数据。
- 后台任务仍采用现有进程内模型；强制终止可能留下临时文件和 processing 状态，需要主机人工核对清理；没有新增自动恢复队列。
- 旧公共库需要配置显式只读允许列表，或使用新 collection 重新导入。未自动替用户迁移任何业务 collection。

保持启动器创建的独立分支；本次只本地提交。没有 push、PR、GitHub 评论、Issue 关闭或部署。Issue 保持打开，由用户审阅后决定发布和关闭。

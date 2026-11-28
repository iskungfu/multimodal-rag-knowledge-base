# 查询运行架构真实验收（2026-11-28）

实现位于 `knowledge/runtime.py`，架构决策见 ADR-0012。用户选择候选 1，并要求继续完成真实环境验收。

本轮使用独立随机测试库 `afk_runtime_20261128_7nrx7aqh` 和脚本生成的 UUID 集合；未修改业务配置文件。已有本机 BGE-M3、reranker、MongoDB、Milvus、LLM 与行情来源用于主机验收；未安装依赖或下载模型。

日志目录：`.sandcastle/logs/runtime-live-20261128-7nrx7aqh/`。

## 通过项

- 离线回归：679 passed、24 skipped，26.20 秒，含 28 项查询运行测试。
- 真实集成：24 passed，134.34 秒，覆盖行情、复盘、风险查询、摘要的存储与模型流程。日志 `integrations.log`。
- 投资助手存储故障探针通过，`investment-storage.json`；数据库和 3 个向量集合清理成功。
- 风险聊天入口 3 问题 × 流式/非流式均通过，API、SSE、历史、免责声明一致，日志 `stock.log`。
- 真实 MongoDB 应用隔离 14 项通过：关闭入口无集合写入；两个应用同会话和档案互不串用；临时集合清理成功。报告 `live-isolation.json`。
- 档案、模式、复盘纠错及摘要定向复测 111 项通过，`investment-personal-retry.json` 中 `personal_mvp_acceptance=true`。手动摘要及实际 scheduler executor 的当前模式、公共知识、历史快照均通过，资源全部清理。
- 独立每日摘要主机入口复测 17 项通过，`.sandcastle/logs/summary_accept_46554290f0214a1a.json`；真实导入、BGE、手动生成、模型选取、实际调度与资源清理全部通过。
- 旧 schema RAG 17 项通过，`.sandcastle/logs/rag_accept_a7d5c03414f6.json`；公共知识流式 RAG 22 项通过，`.sandcastle/logs/rag_accept_a52e77a6f954.json`。实际 BGE、RRF、reranker、LLM、观察器、SSE、历史和清理通过。

## 真实验收发现与修复

旧 RAG 两次首次运行的所有功能检查通过，但清理公共集合失败。所用 PyMilvus 的默认连接 alias 按地址与身份派生，两个 `MilvusClient` 对象可能共用底层连接，查询运行 `close()` 会使导入侧客户端失效。

修复：查询运行与投资验收的专用客户端显式使用随机 alias；新增多应用连接关闭隔离回归测试。真实检查确认关闭一个运行后，另一运行与既有导入连接均继续可用。仅删除报告明确登记、且 worker 已停止的 4 个残留公共向量集合，报告 `milvus-connection-isolation.json`。旧 RAG 两模式重跑均成功清理。

首次失败报告仍保留：`.sandcastle/logs/rag_accept_28ea963a10b6.json`、`.sandcastle/logs/rag_accept_335a11c929a5.json`；对应输出与退出码另存为 `*-before-fix*`。

## 模型输出波动

首次投资助手完整主机加档案验收在 `mvp_summary_completed` 失败；报告记录 392 项唯一检查成功、1 项失败，策略模型返回 `ValueError`，摘要保持 failed，资源全部清理。独立摘要主机首次运行在同一模型依据校验阶段失败并清理成功。

保留原始失败报告 `investment-host.json` 与 `.sandcastle/logs/summary_accept_91b448c4144445c3.json`。定向复测未修改提示词、校验规则或模型行为；两次摘要均通过原文及行情 ID 校验。诊断仅记录布尔匹配结果、长度和异常类别，见 `summary-diagnostics.json`，未记录模型原文或密钥。首次失败的具体校验分支未被捕获，不能仅凭后续成功推断唯一原因。

定向报告按设计仍为 `full_acceptance=false`，不冒充完整主机单次全绿。真实服务的长期可靠性、跨交易日调度和浏览器视觉交互不在本轮完成声明中。

## 结束核对

`audit.json` 确认本轮独立测试库零集合；Milvus 集合清单相对验收前无新增、无缺失。失败运行的残留已限定名称清理，既有公共集合保持完整。`git diff --check` 通过；改动保留在工作区，未提交或发布。

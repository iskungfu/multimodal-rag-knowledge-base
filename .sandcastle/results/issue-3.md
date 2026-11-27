# Issue #3：复盘自然语言字段抽取

状态：完成（completed）

日期：2026-11-27（Asia/Shanghai）

任务：https://github.com/iskungfu/multimodal-rag-knowledge-base/issues/3

父规格：https://github.com/iskungfu/multimodal-rag-knowledge-base/issues/1

## 选择与范围

已读取 AGENTS.md、GLOSSARY.md、docs/agents/、基线审阅、相关 ADR 和父规格。当前分支已有 #2 完成记录，跳过该任务。选择 #3 前执行：

```bash
gh api repos/iskungfu/multimodal-rag-knowledge-base/issues/3/dependencies/blocked_by
```

实际返回 `[]`；#3 正文无 blocker。代码只有探索脚本，尚无复盘 API、存储或页面输入，本任务不依赖尚未实现的个人策略导入、风险评估或三路检索。本轮仅实施 #3，保留启动器创建的分支。

## 实现内容

- 查询服务新增 `POST /reviews`、`GET /reviews`、`GET /reviews/{id}`，聊天页新增自然语言复盘输入、结果显示和最近记录读取。
- 新建 MongoDB `trade_reviews` repository，先保存文本，再调用模型和更新抽取结果。固定 `owner_id=default_user`，读写均带 owner 过滤；客户端不能覆盖 owner。
- 用 UUID 请求编号和 MongoDB `_id` 唯一索引避免相同请求重试重复入库/抽取，保留创建与更新时间。创建文本失败明确返回 503；抽取失败仍返回文本已保存；抽取结果写入故障保留 `pending` 文本并提示。
- 逐笔抽取 `events`（代码、名称、买卖方向、价格、原因、成交/计划/未知状态、原文依据），另存反思教训和情绪短语。仅名称不猜代码，仅代码不补名称，缺失价格不补值。
- 名称代码冲突时保留 `mentioned_stock_code`，关联字段 `stock_code=null`，页面要求核对；不自动修正或静默选定标的。
- JSON 解析支持围栏/前后说明，拒绝半截 JSON、多个对象、重复字段、非法类型、非有限价格及无原文依据的内容。模型配置/网络/响应失败与解析失败均降级保存文本，不记录原始模型响应或异常中的敏感详情。
- 根据 ADR-0005 在本功能调用模型前、写入前、返回前做基础规则脱敏。无敏感信息时逐字保留原文；有敏感信息时仅保存脱敏后的用户文本。前端复盘结果末尾显示固定免责声明。
- 使用已有 OpenAI/PyMongo/FastAPI 依赖，复盘模块不加载 embedding 模型，不连接其他存储。接口说明及限制见 `docs/trade-reviews.md`。

## 对旧原型断言的更正

不以“JSON 可解析”作为语义验收。真实样本分别检验已成交、未执行计划、已撤挂单、多次买卖及计划混合、名称代码冲突、无代码、无价格/名称/原因/情绪。实际模型使用环境注入的应用配置，不复用原型中的硬编码地址、模型或凭据。

单价与交易数量、仓位百分比、涨跌幅分别处理；原型中的“准备减仓”没有成为已执行交易。未给名称的 `600519` 没有自动补成贵州茅台。文本中的 `000001 上证指数 ETF` 被标记为冲突，保留所写代码但不关联个股。

## 实际验证

使用 Linux 临时文件系统中的 `/tmp/shopkeeper-venv`，通过 `python3 -m venv /tmp/shopkeeper-venv` 创建；所有 Python 安装与测试均由其解释器运行。没有在项目内创建 venv 或链接，没有修改只读 `AFK_STORAGE_PYTHON` 环境。安装按需使用的 pytest、mongomock、FastAPI、httpx、PyMongo、OpenAI、python-dotenv、requests、uvicorn；浏览器验证工具 Playwright 安装在同一临时环境。

### 单元/API 行为及行情回归

最终命令：

```bash
/tmp/shopkeeper-venv/bin/python -m pytest -q -p no:cacheprovider knowledge/test/review/test_review.py knowledge/test/market/test_market.py
```

实际结果：**66 passed in 1.76s**，其中复盘 33 项，现有行情 33 项，无 skip。覆盖保存与读取、原文先落盘、UUID 幂等、owner 过滤、输入校验、解析容错、无依据字段拒绝、冲突代码保留、模型失败保留文本、存储故障、脱敏前后边界和标识符不被误脱敏。

首次运行发现金额在重复脱敏时会从 `50万+` 变成错误文本，已修正规则并通过重复脱敏测试；另补充了 UUID 不应被当作手机号脱敏的回归验证。单元/API 行为中的模型/存储故障使用受控替身，不将其记作外部模型或数据库验收。

### 真实模型与 MongoDB

第一轮命令：

```bash
AFK_RUN_REVIEW_INTEGRATION=1 /tmp/shopkeeper-venv/bin/python -m pytest -q -s --tb=short -p no:cacheprovider knowledge/test/review/test_review_integration.py
```

当时文件包含前 8 个模型样本和 1 个故障注入样本，实际结果：**9 passed in 81.10s**，无 skip。随后新增数量/百分比和仓位调整计划两例，运行：

```bash
AFK_RUN_REVIEW_INTEGRATION=1 /tmp/shopkeeper-venv/bin/python -m pytest -q -s --tb=short -p no:cacheprovider knowledge/test/review/test_review_integration.py -k 'quantity or position_plan'
```

实际结果：**2 passed, 9 deselected in 20.28s**。合计当前集成文件的 **11 项均实际执行通过**：10 项实际模型 + MongoDB，1 项真实 MongoDB + 人工注入坏 JSON（不计作真实模型调用）。未发生实际模型连接失败；没有用 mock 替代这 10 项语义验收。

| 自建样本 | 实际结果 |
| --- | --- |
| 12.5 元买入 000001，含原因、教训和着急情绪 | 已成交买入，价格 12.5，原因/教训/情绪有原文依据 |
| 明天以 185 元买入 300750 的条件计划 | planned，目标价 185，不计作已成交 |
| 185 元卖出宁德时代，未写代码 | 已成交卖出，代码 null，保留后悔和反思 |
| 买入 000001 上证指数 ETF | identity_conflict=true，关联代码 null，原文代码 000001 保留 |
| 仅写买入 600519 | 名称、价格、原因、教训均 null，情绪空列表 |
| 12.5 买入、13 卖出、计划 12 再买 | 分为两笔已成交与一笔计划，价格不混淆 |
| 挂单 12 元但未成交并撤单 | 未标为 executed |
| 纯反思，明确无买卖/计划 | events 空数组，保留教训 |
| 买入 100 股、仓位 20%、跌 3% | 已成交，price=null，未推断情绪 |
| 仓位 80%，准备减仓到 40% | planned sell，代码/单价 null |

每个真实模型样本经 FastAPI router → 实际 LLM → 独立 MongoDB → API 读取，验证文本与抽取字段一致，并重复同 UUID 请求确认没有多写记录。所有文本为自建样本，没有使用私人交易记录。

MongoDB 写入严格使用 `AFK_TEST_DB_NAME` 指定的独立测试库，拒绝业务库及系统库；各例创建 `trade_reviews_issue3_<uuid>` 集合，在 `finally` 中仅删除该集合并关闭连接。没有改写/清空业务数据，没有连接 Milvus/MinIO。

### 静态与提交检查

- 10 个相关 Python 文件通过 `ast.parse`。
- `reviews.js`、`api.js` 通过 `node --input-type=module --check`，没有调用不存在的 npm test/typecheck。
- 已检查 diff 与新增文件；敏感值扫描及 `git diff --check` 通过，仅暂存本任务相关文件与结果记录。

## 未验证项与限制

- Chromium 下载约 10 分钟仍仅完成少量数据，主动停止下载；**没有执行真实浏览器交互/视觉验收**，未将其记为通过。
- 本次实际验收的是新增 router、LLM、MongoDB 流程；没有安装/启动完整 RAG 模型和查询图，没有重测整套原有应用。
- 语义分类和名称代码冲突仍依赖模型及有限样本验证，没有接入权威证券主数据；原文依据校验不能证明所有语义都正确。自动抽取结果需用户核对。
- 脱敏为本入口的基础规则，无法保证覆盖无标签人名、分段号码或所有自由文本金额表达；没有宣称全应用脱敏已完成。
- 父规格/ADR 的复盘向量化及三路检索尚未实现，本期按 #3 验收范围完成 MongoDB 文本与字段保存。后续可基于 `owner_id/doc_type=review` 接入检索，当前不生成 Milvus 向量。
- 没有自动重抽 `pending/failed` 记录的任务或人工编辑接口；文本可读以供核对。其他 AI 回答的全局免责声明仍属于后续功能。

仅本地提交，不 push、不创建 PR、不写 GitHub 评论、不关闭 Issue，交由用户审阅。

# Issue #5：个股风险等级查询

状态：完成（completed；沙箱实施、主机真实验收与双轴审查均完成）

日期：2026-11-27（Asia/Shanghai）

任务：https://github.com/iskungfu/multimodal-rag-knowledge-base/issues/5

父规格：https://github.com/iskungfu/multimodal-rag-knowledge-base/issues/1

## 选择、依赖与范围

依据用户“进入下一轮”的持续执行授权，按当前依赖顺序选择 #5，本轮只实施该工单。已阅读 AGENTS.md、GLOSSARY.md、docs/agents/、docs/afk-baseline-review.md、相关 ADR、#5 与父规格 #1。开始时当前分支没有 #5 完成记录，工作树干净；沙箱未切换/合并分支。

选择前以及收尾均执行：

```bash
gh api repos/iskungfu/multimodal-rag-knowledge-base/issues/5/dependencies/blocked_by
```

**原生依赖仍为 #2，state=open**；#5 正文也依赖 #2。没有把本地完成当作 GitHub 解除阻塞。本轮仅采用用户明确授权给 #5→#2 的分支内例外：

```bash
git merge-base --is-ancestor dcd09f1fedabb4673315b94aa9f65b02e67ccf32 HEAD
git merge-base --is-ancestor 5ff71c8 HEAD
```

两条命令均退出 0。核对 `.sandcastle/results/issue-2.md` 为完成，原记录 33 单元/API + 4 真实集成通过；阅读 `docs/market-data.md` 及实际 adapter、repository、MarketService。正式实施前重新运行行情全套：**37 passed in 4.75s**，含真实东方财富、腾讯、MongoDB 验证。因此确认依赖在本分支可用，未扩大例外到其他 Issue。

## 实现

- 个股风险接入原有 `POST /query` 与聊天页面，自动核实证券、刷新/读取行情并展示风险，不进入商品名称确认；没有只做独立行情/风险 API。
- 东方财富公开证券检索严格过滤 AStock、沪深市场及 QuoteID。名称精确匹配来源，简称/多证券/名称代码冲突要求确认；`000001` 仅作为平安银行，拒绝把上证指数或基金混作个股。保存了两份实际公开响应样本。
- 拉取最近 120 个自然日的收盘行情，N=20。波动率使用源日涨跌幅的样本标准差，不从不复权价格反推；量比基准为此前 20 条不同日期日线，排除当日。
- 明确中/高阈值及取最高档聚合，处理非正 PE、坏数值、日期乱序/重复/冲突、零量与零基准、历史不足、接口和存储失败。完整有效依据才能判低；缺失时仅可给有明确有限依据的中/高，或者拒绝完整评级。
- 展示实际行情日期、来源、估值来源、抓取/源快照时间（含时区）、距查询日自然日数、短时/旧缓存与失败提示。腾讯单快照明确缺失波动率与量比，旧数据不能标成今日。
- 公共知识继续使用现有 LangGraph 检索/RRF/rerank，按公共文档隔离，跳过商品名确认；产品问题保留既有流程。未实现个人策略/复盘三路投资问答。
- QueryService 统一终稿出口：完整文本脱敏、免责声明去重并置于末尾、会话持久化、非流式响应、任务结果、SSE delta/final 使用同一答案。覆盖商品确认、无结果和异常。模型流先完整缓冲再脱敏，避免跨 chunk 绕过；final 后不再发送 progress。
- 会话输入、重写文本、实体和输出复用脱敏；旧历史读取时补齐尾句与脱敏。历史写入失败有明确提示，连接异常原文和配置不向外输出。前端图片解析保留图片块之后的尾句。
- 规则、接口行为与限制见 `docs/stock-risk.md`；主机验收入口 `scripts/verify_stock_risk.py`。

没有新增市场阶段分类、交易执行、实际持仓修改、审计日志、收盘调度、摘要、推送或付费源。没有实施 #6/#7。

## 真实验证

按本轮要求创建 `/tmp/shopkeeper-venv`，安装及测试均使用 `/tmp/shopkeeper-venv/bin/python`。没有在项目中创建虚拟环境或链接，没有使用 Windows 虚拟环境，没有修改 `AFK_STORAGE_PYTHON`。没有下载 BGE、GPU 依赖或浏览器。

最终命令：

```bash
AFK_RUN_MARKET_INTEGRATION=1 AFK_RUN_RISK_INTEGRATION=1 /tmp/shopkeeper-venv/bin/python -m pytest -q -p no:cacheprovider knowledge/test/risk knowledge/test/query/test_rag_regressions.py knowledge/test/market knowledge/test/review/test_review.py knowledge/test/strategy/test_strategy.py
```

最终真实输出：**230 passed in 24.91s**，无 skip。其中 **221 项单元/API/回归，9 项真实集成**（#2 的 4 项 + #5 的 5 项）。

- 规则阈值、缺失/负 PE、NaN/Infinity、无效/重复/乱序/未来日期、零/负成交量、零均量、缺少样本、除权价格阶跃不污染源收益率通过。测试发现并修复标准差在 2%/4% 数学边界因浮点误差降档的问题，比较容忍 1e-12 相对/绝对误差。
- 实际 `/query` router 与 LangGraph 回归验证非流式、完整 delta、唯一 final、status、历史一致；商品确认、无结果、错误、旧历史、逐字符模型分块脱敏、异常结构化输出均覆盖。公共知识检索的图结构回归使用明确的模型/检索测试替身，不冒充真实向量模型验收。
- 真实东方财富 → `/query` → 独立 MongoDB：平安银行 **2026-11-27，81 条日线**；真实腾讯备用 → 同路径：**2026-11-27，1 条快照**。重复查询复用缓存，SSE/status/history 一致；腾讯显示两项历史指标缺失。
- 市场前缀与明确公共概念独立于证券源的回归通过；实际证券检索确认 `000001` 的多类型歧义，以及贵州茅台 `600519` 的名称/代码；冲突不静默纠正。
- 实际配置 LLM 的非流式与流式调用均通过：使用自建、无敏感信息的分散投资知识片段，验证真实 AnswerOutputNode 与 API/存储出口。没有复盘抽取需求，未调用私人交易记录。该项没有真实向量检索，不能算完整 RAG。
- 显式集成仅使用 `AFK_TEST_DB_NAME` 内 `market_quotes_issue2_<uuid>`、`risk_issue5_<uuid>_market/history`；每次结束按集合清理。没有对 Milvus/MinIO 写入或清空业务资源。
- `python scripts/verify_stock_risk.py` 已在沙箱实跑：实际股票、名称代码冲突、指数歧义三类问题 × 非流式/流式共 6 次 API/历史/尾句检查通过。使用指定测试库内 `risk_accept_<uuid>`，结束清理。
- `node --check knowledge/front/assets/js/chat.js` 通过；直接执行实际图片解析函数的 2 项行为检查通过（尾句另起一行及与 URL 同行）。未运行不存在的 npm test/typecheck。
- 25 个改动 Python 文件 AST 解析通过；改动文件未匹配注入凭据值；`git diff --check` 通过。

早期测试曾出现两个旧回归断言失败（答案已从节点发布迁移到服务出口），及两个新增浮点边界失败，均已修复并纳入最终通过结果。首次扩大回归时出现原有导入节点 docstring 的 `invalid escape sequence` SyntaxWarning，本轮未改动该无关文件。

## 测试隔离疏漏与撤销

**本轮有一次违反测试库隔离要求的疏漏，不能记为“从未写入业务库”。** 在新增服务端统一会话保存后，第一次运行旧 `test_failure_is_visible_and_does_not_expose_exception`，旧测试没有替换新写入路径，向配置业务库的 `chat_history` 写入了 4 条自建测试消息（2 条“问题”、2 条固定失败答复）。

发现后立即停止该路径；以 session=`test`、本次 5 分钟内时间窗、固定内容、空重写/实体、角色分布及记录间隔核实恰好 4 条，再仅按这 4 条 `_id` 删除，实际删除数为 4。未读取/输出私人消息，未删除其他历史，未清空集合。已向用户披露此事。

随后为新规则/API 测试设置默认 mongomock 历史集合，旧回归显式替换保存路径；真实集成则显式切到指定测试库 UUID 集合。最终 230 项测试在这些隔离措施下执行。主机验收脚本的聊天、行情同样使用独立集合，并禁用不属于本次验收的复盘路由。

## 未验证项与验收入口

- **完整 BGE RAG 与浏览器交互：待主机验收。** 沙箱没有本机向量/重排模型或浏览器，没有把测试替身或自建固定上下文记为完整 RAG 成功。主机命令及逐项浏览器步骤见 `docs/stock-risk.md`。
- `python scripts/verify_stock_risk.py --rag-query "什么是分散投资？请引用公共知识资料。"`：在主机已有公共文档、模型及服务环境中验证真实完整查询。应选主机库中已有证据的问题。
- `python scripts/verify_stock_risk.py --serve --port 8002`：访问 `http://127.0.0.1:8002/chat.html`，用独立测试行情/历史验证流式开关、风险与日期来源、冲突确认、刷新历史、公共知识和原商品手册图片/尾句。正常退出清理临时集合。
- 免费源长期可用性、全部 A 股名称/更名、行业校准、真实限流上限和跨交易日长期运行未验证。名称切词为保守常用句式规则，不是任意自然语言理解；不能唯一核实时要求确认。
- 初始风险阈值未经回测，不代表预测能力或买卖建议；该限制在每次风险回答中展示。为可靠脱敏，文字在完整生成后发布，未提供逐 token 的可见文本流。

沙箱仅本地提交。GitHub #2/#5 及依赖保持原状；沙箱没有 push、创建 PR、写评论或关闭 Issue。主机验收及后续 push 按用户已有授权执行，见下方记录。

## 主机最终验收与审查（2026-11-27）

固定审查基点 `3667889`，沙箱实施提交 `26053fd`。主机沿用已有模型与独立 Python 测试环境，未升级业务模型依赖。

### Standards

独立审查发现 4 项并全部修复、复核通过：简称与代码冲突、设备追问误走证券流程、脱敏损坏实际引用 ID，以及风险规则与说明参数重复维护。前 3 项加入确定性反例；阈值说明现在从 `THRESHOLDS`/`N` 生成。

### Spec

独立审查发现 3 项并全部修复、复核通过：同一代码混用市场前缀或后缀、口语句式中的名称代码冲突，以及指数基金知识解释被误当成不支持的评级。独立复核的 11 项定向检查全部通过。

### 主机修复与验证

- 实际提问“股票600519现在的位置风险怎么样？”最初被错误要求确认；修复了描述词被当成证券名称的行为，同时保留已核实名称/简称的冲突检查。名称切词仍是有限规则，不声称能理解任意自然语言。
- Windows 快速连续写入会产生相同时间戳，初次主机回归出现 16 项历史顺序失败；增加 `_id` 作为排序次序并以固定相同时间戳覆盖。没有通过延时或放宽断言绕过。
- 仅对检索实际返回的 `chunk_id` 提供脱敏豁免，传递到服务端任务、存储及历史。未核实的数字标识和正文手机号仍被脱敏；流式与非流式均验证。
- 沙箱默认 `MONGO_DB_NAME` 改为本轮独立 `_app` 测试库，显式集成继续使用不同的 `AFK_TEST_DB_NAME`；补做 `npm run sandcastle:check`，bwrap、Linux、GitHub、数据库和模型连通检查通过。业务库中此前 4 条测试消息的精确清理已由主机只读复查，残留为 0。
- **最终主机完整回归：249 passed in 34.89s，无 skip。** 包含 240 项单元/API/回归和 9 项真实集成（行情 4 项、风险/实际 LLM 出口 5 项）。覆盖策略导入、自然语言复盘、旧 RAG、风险规则与查询。
- **真实股票端到端：28/28 检查通过，8.09 秒。** 贵州茅台非流式、平安银行 SSE，经实际名称检索/行情/默认应用存储/API/status/history，五项指标、日期、唯一尾句、历史删除与测试库清理均通过。报告 `.sandcastle/logs/risk_accept_c9eeb058bec1.json`。
- **真实 RAG：** PDF + MinerU + 新 schema + SSE，21/21，94.52 秒，报告 `.sandcastle/logs/rag_accept_63b21afd98f8.json`；旧 schema + Markdown + 非流式，16/16，74.08 秒，报告 `.sandcastle/logs/rag_accept_7ba6ede1e9fa.json`；公共知识路径 + Markdown + SSE，21/21，58.73 秒，报告 `.sandcastle/logs/rag_accept_eba27fb829a3.json`。实际 BGE-M3、Milvus、RRF、reranker、LLM、证据答案与历史存取均通过，各自资源清理通过。公共知识路径没有依赖商品名确认。
- **浏览器：** 实际聊天页面流式/非流式查询、刷新历史、名称代码冲突、503 展示与重试均通过；390px 视口 `scrollWidth=390`，无横向溢出。截图 `.sandcastle/logs/risk-ui-mobile.png`。服务停止后核对并清理仅本次创建的两个 UUID 测试集合，未保留后台服务。

审查结果：Standards 原 4 项与 Spec 原 3 项均已解决，无未解决的本轮实质发现。测试报告保留在本地被忽略的日志目录，此文件保存可随 Git 复核的结论。

### 验收边界

主机待验项已完成。风险阈值仍是未经回测的初始规则；未验证全市场覆盖、复杂名称解析、长期数据源稳定性、行业校准或扫描长书籍。答案先完整缓冲再脱敏，SSE 推送进度与完整终稿，不逐 token 展示。个人策略/复盘三路问答留给 #7，摘要与调度留给 #6；未部署、合并 main 或关闭 Issue。

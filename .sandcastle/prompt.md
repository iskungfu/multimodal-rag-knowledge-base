# 项目与任务

全程使用简体中文输出。代码、命令、文件名和术语保留英文。
项目是 Python/FastAPI + LangGraph 的 A 股个人投资助手，前端为现有 HTML/JavaScript。
先读取 AGENTS.md、GLOSSARY.md、docs/afk-baseline-review.md、docs/agents/ 和相关 ADR。基线审阅记录更正了旧原型与 Issue 中未经验证的断言，不得机械复制旧公式。
任务仓库固定为 iskungfu/multimodal-rag-knowledge-base，父规格为 Issue #1。

## 待办候选

!`gh issue list --repo iskungfu/multimodal-rag-knowledge-base --state open --label ready-for-agent --limit 100 --json number,title,body,labels,comments --jq '[.[] | select(.number != 1) | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}] | sort_by(.number)'`

仅从上述候选中选任务。#1 是规格，不能当作实施任务。
每轮最多处理一个 Issue，跳过当前分支已有 `.sandcastle/results/issue-<编号>.md` 且标记为完成的任务。
选择前使用 `gh api repos/iskungfu/multimodal-rag-knowledge-base/issues/<number>/dependencies/blocked_by` 查询原生依赖，默认跳过有 OPEN blocker 的任务；仅允许下方本轮明确列出的、已在当前分支验收的依赖例外。
同时检查正文依赖和实际代码依赖，不因标签为 ready-for-agent 就假定所有前置功能已实现。

## 本轮任务与分支依赖凭据

本轮只执行 #7（个性化投资助手三路召回）。GitHub #3、#5 仍 OPEN，尚未发布关闭；当前用户已授权沿已验收分支持续执行下一轮，不能把本地完成等同于 GitHub 已解除阻塞。
仅对 #7 的前置 #3、#5 使用以下分支内依赖凭据，不适用于其他候选；#2 行情、#4 策略导入、#6 摘要也在此分支验收，可复用：

- 验收基线 `2bb39697fb2fb3c91a95e58d6a68f6a5541737ac` 必须通过 `git merge-base --is-ancestor <基线> HEAD`；#2 实现 `5ff71c8`、#3 实现 `b6f6dde`、#4 验收 `dcd09f1`、#5 实现 `26053fd` 和验收 `4550158` 同样必须是祖先。
- `.sandcastle/results/issue-2.md` 状态为完成，记录 33 单元/API + 4 真实网络/MongoDB 集成通过；接口与字段约定见 `docs/market-data.md`。
- `.sandcastle/results/issue-4.md`、`.sandcastle/results/issue-5.md` 记录策略导入、RAG、个股风险真实验收。#5 最终回归 249 passed，真实查询 28/28，三种 RAG 路线通过；复用现有隐私与引用保护逻辑，不倒退旧公共库兼容。
- `.sandcastle/results/issue-3.md` 状态完成，66 单元/API、11 实际集成和主机浏览器通过，但明确尚未实现复盘向量化。本轮需补齐此实际前置能力，不能只手工拼接文本冒充三路检索。
- `.sandcastle/results/issue-6.md` 状态完成；329 回归、真实摘要16/16、PDF+SSE RAG22/22、旧schema RAG17/17、公共知识SSE RAG21/21 通过。保留摘要、关注列表和调度行为。
- 在本轮结果记录 GitHub 的 OPEN blocker 状态、采用的基线与复核证据。检查实际行情代码及相关回归；依赖不可用时停止，不扩大例外。
- 不更改 GitHub 依赖、Issue 状态或评论；不实施其他 Issue。
- #7 旧正文错误地把行情作为向量三路之一。按更新后的 GLOSSARY.md、docs/spec-personal-investment-advisor.md、ADR-0003 执行：personal / knowledge / review 三路向量检索、RRF 融合、rerank；行情为独立结构化上下文。结果记录此差异。

## 工作步骤

1. 阅读任务及父规格，核实实现范围和验收条件。
2. 检查现有实现和测试。原型输出仅是探索结果，行情字段单位、数据来源和 LLM 抽取语义必须重新验证。
3. 实现一个最小且完整的任务，避免无关重构。
4. 按 `docs/agents/development.md` 的 WSL/Linux 分支准备检查环境并运行 `python scripts/check.py`。实际集成按下列隔离与主机边界执行。
5. 运行改动对应的测试并记录真实结果。MongoDB、Milvus、MinIO 的连接参数已从本机配置注入环境，不要打印变量值。MongoDB 写入测试必须使用 AFK_TEST_DB_NAME 指定的独立测试库；其他服务使用独立测试 collection/bucket，禁止改写或清空现有业务数据。需要服务或模型的测试必须实测，不能把未执行的集成测试记为通过。
6. 应用 LLM 的 OPEN_API_BASE、OPEN_API_KEY、LLM_DEFAULT_MODEL 已按最小允许列表注入。涉及抽取时，使用自建、无敏感信息的文本实测实际模型；明确区分已执行交易与交易计划、名称代码冲突和未明示字段。不要打印凭据、使用用户私人交易记录或为了仅调用 LLM 安装整个向量模型依赖。网络错误、模型调用失败必须记录，不能用 mock 结果替代真实验收。
   沙箱没有本机 BGE 模型、MinerU 凭据和浏览器。涉及完整 RAG 时，完成可运行的单元测试和独立存储测试，并提供主机验收入口；主协作者会使用 Windows 已有模型与已连接浏览器补验。不要下载大型模型、Chromium 或安装 GPU 依赖来绕过这一边界。真实流程未跑时，在结果中明确标记“待主机验收”。
7. 将实现内容、验证结果和未验证项写入本地 `.sandcastle/results/issue-<编号>.md`，明确标记为完成或阻塞。
8. 检查 diff、敏感信息和 `git diff --check`，提交本次任务相关的文件及结果记录。commit 使用 `RALPH: <任务说明> (#编号)`。保持 Issue 打开，由用户审阅结果后决定发布和关闭。

## 执行边界

- 使用启动器创建的独立分支，不切回或合并到主分支，不 push，不创建 PR，不写 GitHub 评论、不关闭 Issue。
- 不自动连接券商或交易，不修改实际持仓，不部署，不接入付费数据源。
- 复盘输入是自然语言，不要求用户填结构化交易表；不做牛熊/震荡市场阶段分类。
- 不打印、提交或复制密钥。`.sandcastle/.env` 与本机 Codex 配置仅用于运行环境。
- 不使用交互式命令。如果外部依赖或需求缺失导致阻塞，记录原因并结束当前运行。
- 所有可执行候选均完成、为空或阻塞时，输出 `<promise>COMPLETE</promise>`。

## 个性化投资助手验收关注点（#7）

- 先检查现有 QueryService、查询 LangGraph、milvus_util、RerankNode、StrategyVectors、ReviewService、answer_policy。保持商品与旧公共知识 RAG 兼容，不要求用户选择技术模式。
- 自然语言复盘先保存脱敏原文及抽取结果，再索引原文和摘要。向量故障不能回滚原文；索引状态独立于 extraction_status，提供用户可操作的有限重试/补索引入口。旧记录应能按明确记录ID补索引。并发重试、部分写入、进程中断后不能产生重复可见切片或永久处理中。固定 default_user，禁止客户端覆盖 owner；不得自动扫描私人记录作为测试。
- 实际 BGE-M3 三路召回：personal/default_user、knowledge/public、review/default_user；Milvus 查询阶段和返回阶段均校验隔离。个人策略需核对 Mongo 完成状态，复盘需核对可见记录与索引版本。公共旧 schema 仅沿用现有服务端白名单；personal/review 永不能回退旧无隔离库。
- 三路 RRF 用包含来源类型、collection、document/review_id、chunk_id 的稳定键去重，防止不同库的相同数字ID串源。后续 rerank/断崖截断保留这些元数据。行情不参与 RRF 排名。
- 查询路由覆盖明确个股、投资概念、个人交易方法/过往复盘，以及有明确上下文的简短追问；#5 个股风险不能提前返回而跳过三路检索。证券身份冲突优先澄清，不凭历史强行选股；纯商品问题仍走既有 RAG。避免无界历史和把历史AI回答当作证据。
- 复用 RiskService 的确定性风险、日期、来源、缺失提示，作为独立行情依据；LLM 不得改写其等级或臆造实时价格/持仓。大盘只用现有指数能力表达当前位置风险，不增加牛熊/震荡阶段分类。没有行情时说明缺失。
- 生成结果必须可追溯：实际检索的类型、原文片段、document/review_id、chunk_id 和行情来源。拒绝模型伪造引用；没有检索证据时不能生成貌似个性化的结论。个人条件规则必须保留前提，不能声称触发；复盘计划/挂单/未知不得变为已执行交易。使用自建三类独特事实证明回答确实使用检索，不能只是列来源附录。
- 单路为空/故障与全部不可用分别呈现；rerank/模型失败提供有据降级，不能让个人库故障拖垮可用行情。异常不泄露连接串、原文或模型响应。
- 服务端最终文本统一脱敏并只追加一次免责声明；SSE 的 delta/final、非流式/status/history一致。保留当前完整答案缓冲，不逐片泄露号码。仅允许实际检索引用的 chunk_id 绕过数字脱敏，任意自报标识不能成为白名单。
- 单元/API 覆盖上述边界；用真实 MongoDB/Milvus 验证索引幂等、隔离与三路融合接口。提供 scripts/verify_investment_assistant.py 一类可重复主机验收入口，真实 BGE-M3、reranker、LLM、行情与 API 全链路，包含自建 personal/knowledge/review 三种不同事实和其他 owner 干扰数据、SSE/history一致、模型失败/单路失败。遵循现有 verify_* 的 --host 和隔离清理模式，主机模型只在主机验收。
- 复盘索引状态/重试如需要 UI，最小改动现有自然语言复盘面板，中文说明，安全文本渲染及390px可用；不要重做设计系统。保留 #2/#3/#4/#5/#6 与三种旧 RAG 回归。
- 沙箱默认应用 MongoDB 是独立 `_app` 库，但显式集成用 AFK_TEST_DB_NAME；结束时核对清理两种测试库。不要向业务库写测试消息。

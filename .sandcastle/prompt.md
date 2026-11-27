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

本轮只执行 #5。GitHub #2 仍 OPEN，尚未发布关闭；当前用户已授权沿已验收分支持续执行下一轮，不能把本地完成等同于 GitHub 已解除阻塞。
仅对 #5 的前置 #2 使用以下分支内依赖凭据，不适用于其他候选：

- 验收基线 `dcd09f1fedabb4673315b94aa9f65b02e67ccf32` 必须通过 `git merge-base --is-ancestor <基线> HEAD`；#2 实现提交 `5ff71c8` 同样必须是祖先。
- `.sandcastle/results/issue-2.md` 状态为完成，记录 33 单元/API + 4 真实网络/MongoDB 集成通过；接口与字段约定见 `docs/market-data.md`。
- 在本轮结果记录 GitHub 的 OPEN blocker 状态、采用的基线与复核证据。检查实际行情代码及相关回归；依赖不可用时停止，不扩大例外。
- 不更改 GitHub 依赖、Issue 状态或评论；不实施 #6/#7。

## 工作步骤

1. 阅读任务及父规格，核实实现范围和验收条件。
2. 检查现有实现和测试。原型输出仅是探索结果，行情字段单位、数据来源和 LLM 抽取语义必须重新验证。
3. 实现一个最小且完整的任务，避免无关重构。
4. 使用沙箱 Linux 文件系统中的 Python 虚拟环境：`python3 -m venv /tmp/shopkeeper-venv`，之后通过 `/tmp/shopkeeper-venv/bin/python` 运行安装和测试。项目位于 WSL 的 Windows 挂载盘，不要在项目内创建虚拟环境或链接，避免 DrvFS 权限问题和临时文件入库。按实际测试需求安装依赖；完整应用依赖见 requirements.txt。已设置可用的 PyPI 镜像；`AFK_STORAGE_PYTHON` 指向只读存储客户端环境，可运行独立集成探针，不要向其中安装包。不要调用不存在的 npm test/typecheck，不复用 Windows 虚拟环境。
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

## 个股风险任务的验收关注点（#5）

- 必须接入用户现有聊天按需查询，自动读取/更新行情并返回风险，不能只做一个孤立 API。不要把股票问题送入商品名确认流程；保留公共知识库 RAG 查询。三路投资问答属于 #7。
- 涨跌幅、换手率、近 N 日波动率、动态 PE、成交量异常全部由行情取得。明确 N、阈值、聚合方式和指标单位；阈值是可解释的初始规则，不能声称经过回测、预测涨跌或构成买卖建议。
- 无有效数据不能冒充低风险。备用源只有最新快照时，显示缺失历史指标；任何降级结论必须说明有限依据。显示实际行情日期、来源、延迟与失败提示；旧缓存不能标成今日。
- 正确处理缺失/负 PE、NaN/Infinity、重复或乱序日期、零成交量、除零、历史样本不足和接口失败。不将未经复权的跨日收盘价变化与源提供的日涨跌幅混淆；量比基准不应包含当日本身。
- 股票名称/代码的解析必须有可核实来源；不猜代码或把所有六位数字当股票。000001 只代表平安银行，不能把上证指数当股票。无法唯一识别时说明需要确认的证券，不让用户填行情指标。
- 每条 AI 回答结尾强制附加 `以上信息不构成投资建议，投资有风险，入市需谨慎。`。由服务端保证，不能仅依靠模型 prompt 或前端拼接。流式 delta/final、非流式、任务状态和历史回放应一致；不重复附加或让 final 被后续事件覆盖。已有商品确认/无结果回答也需覆盖。
- 复用脱敏，返回与会话持久化均需注意，避免流式分块绕过脱敏。异常不暴露配置或原始凭据。不做牛熊/震荡分类、交易执行、审计日志、收盘调度或独立风险推送。
- 测试包括规则边界、坏数据、实际 /query API 与 SSE/历史一致性、原有 RAG 回归。可在沙箱执行真实行情与独立 MongoDB 验证；提供主机完整查询与浏览器验收入口，不下载模型。

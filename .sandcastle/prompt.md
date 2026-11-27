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

本轮只执行 #6（每日投资摘要）。GitHub #2 仍 OPEN，尚未发布关闭；当前用户已授权沿已验收分支持续执行下一轮，不能把本地完成等同于 GitHub 已解除阻塞。
仅对 #6 的前置 #2 使用以下分支内依赖凭据，不适用于其他候选；#4 策略导入、#5 风险也在此分支验收，可复用：

- 验收基线 `45501584fab1bc180d2b08108f16c95d642f1b21` 必须通过 `git merge-base --is-ancestor <基线> HEAD`；#2 实现提交 `5ff71c8`、#4 验收 `dcd09f1`、#5 实现 `26053fd` 同样必须是祖先。
- `.sandcastle/results/issue-2.md` 状态为完成，记录 33 单元/API + 4 真实网络/MongoDB 集成通过；接口与字段约定见 `docs/market-data.md`。
- `.sandcastle/results/issue-4.md`、`.sandcastle/results/issue-5.md` 记录策略导入、RAG、个股风险真实验收。#5 最终回归 249 passed，真实查询 28/28，三种 RAG 路线通过；复用现有隐私与引用保护逻辑，不倒退旧公共库兼容。
- 在本轮结果记录 GitHub 的 OPEN blocker 状态、采用的基线与复核证据。检查实际行情代码及相关回归；依赖不可用时停止，不扩大例外。
- 不更改 GitHub 依赖、Issue 状态或评论；不实施 #7。

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

## 每日投资摘要验收关注点（#6）

- 查询服务内嵌 APScheduler 3.x，Asia/Shanghai 收盘后运行；通过 FastAPI lifespan 启停，避免 import 时启动后台任务。默认 15:30，提供开关，测试必须关闭自动调度并使用独立库。参考已核实官方 3.x userguide：max_instances=1、coalesce=True、显式 timezone；跨进程幂等不能只靠 max_instances。
- 定时生成、手动补生成/重试、拉取最新/历史摘要构成完整闭环。用收盘日和 default_user 唯一键，不得重复插入；并发请求、失败重试、进程中断后恢复要有有界处理，不允许永久 processing。最小方案即可，避免搭建通用任务平台。
- 工作日不等于交易日。只有核实的指数当日收盘行情可生成该日正式摘要；周末/节假日、盘中、未来日期和行情延迟不能伪造当日数据。服务离线不能保证定时生成，文档明确手动补生成能力；无需自动补全多年历史。
- 大盘使用上证指数 sh000001、深证成指 sz399001，独立指数标识及存储，不能与股票平安银行 000001 冲突。免费数据源需真实验证身份、日期、单位、日线涨跌幅与成交量；新增指数不能改变现有股票 API 语义。不把仅一日快照当历史；不足时明确有限依据/无法评级。
- 大盘近 N 日涨跌幅、成交量变化用于当前位置风险，不输出牛市/熊市/震荡阶段判断。规则明确 N、单位和未经回测的初始阈值；无数据不得低风险。复用 #5 个股评估，不复制实现或让 LLM 推翻确定性风险。
- 当前无关注列表。增加最小持久化关注股票能力（default_user，最多 20 个，使用已核实身份解析，去重），现有 chat.html 可添加/删除与显示，无需用户手输行情。初始为空时说明未配置，不能暗中预置用户持仓。
- 摘要真实检索本用户已导入的 personal 策略向量，可复用 StrategyVectors + BGE；保留来源/document_id/chunk_id，限定 owner 与类型；策略为空、检索失败和模型失败分别可解释，不能编造策略。不得把“熊市轻仓”等条件当作当前已满足；操作建议以条件式检查清单表达，不能声称自动成交或持仓已知。#7 的聊天三路召回仍在范围外。
- 摘要包括大盘位置简评、关注个股风险等级、个人策略操作提示和数据依据。每日记录存 MongoDB daily_summaries，包含实际数据日期、来源、警告、状态与生成时间；失败不覆盖上次成功摘要。当前无记录、生成中、失败、旧摘要与重试状态在前端清楚显示，手机宽度可用；保持旧聊天/策略/复盘功能。
- 复用服务端 answer_policy 与脱敏：写入前、返回前均保护；免责声明只追加一次，实际检索引用不损坏。个人内容不得写原始调试日志，异常不回显凭据；前端使用安全文本渲染。不做独立推送、审计日志、自动交易、券商接入。
- 单元/API 覆盖调度启停与时区、盘中/休市/缺失/旧数据、去重并发/失败重试、关注列表身份冲突、owner 隔离、策略不足/失败、脱敏/免责声明；真实免费行情 + 独立 MongoDB + 实际 LLM 要执行。输出可重复的主机验收入口，用真实 BGE 策略召回、API、调度和浏览器补验，完整 RAG 回归。
- 沙箱默认应用 MongoDB 是独立 `_app` 库，但显式集成用 AFK_TEST_DB_NAME；结束时核对清理两种测试库。不要向业务库写测试消息。

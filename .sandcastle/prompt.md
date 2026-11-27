# 项目与任务

全程使用简体中文输出。代码、命令、文件名和术语保留英文。
项目是 Python/FastAPI + LangGraph 的 A 股个人投资助手，前端为现有 HTML/JavaScript。
先读取 AGENTS.md、GLOSSARY.md、docs/afk-baseline-review.md、docs/agents/ 和相关 ADR。基线审阅记录更正了旧原型与 Issue 中未经验证的断言，不得机械复制旧公式。
任务仓库固定为 iskungfu/multimodal-rag-knowledge-base，父规格为 Issue #1。

## 待办候选

!`gh issue list --repo iskungfu/multimodal-rag-knowledge-base --state open --label ready-for-agent --limit 100 --json number,title,body,labels,comments --jq '[.[] | select(.number != 1) | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}] | sort_by(.number)'`

仅从上述候选中选任务。#1 是规格，不能当作实施任务。
每轮最多处理一个 Issue，跳过当前分支已有 `.sandcastle/results/issue-<编号>.md` 且标记为完成的任务。
选择前使用 `gh api repos/iskungfu/multimodal-rag-knowledge-base/issues/<number>/dependencies/blocked_by` 查询原生依赖，跳过有 OPEN blocker 的任务。
同时检查正文依赖和实际代码依赖，不因标签为 ready-for-agent 就假定所有前置功能已实现。

## 工作步骤

1. 阅读任务及父规格，核实实现范围和验收条件。
2. 检查现有实现和测试。原型输出仅是探索结果，行情字段单位、数据来源和 LLM 抽取语义必须重新验证。
3. 实现一个最小且完整的任务，避免无关重构。
4. 使用沙箱 Linux 文件系统中的 Python 虚拟环境：`python3 -m venv /tmp/shopkeeper-venv`，之后通过 `/tmp/shopkeeper-venv/bin/python` 运行安装和测试。项目位于 WSL 的 Windows 挂载盘，不要在项目内创建虚拟环境或链接，避免 DrvFS 权限问题和临时文件入库。按实际测试需求安装依赖；完整应用依赖见 requirements.txt。已设置可用的 PyPI 镜像；`AFK_STORAGE_PYTHON` 指向只读存储客户端环境，可运行独立集成探针，不要向其中安装包。不要调用不存在的 npm test/typecheck，不复用 Windows 虚拟环境。
5. 运行改动对应的测试并记录真实结果。MongoDB、Milvus、MinIO 的连接参数已从本机配置注入环境，不要打印变量值。MongoDB 写入测试必须使用 AFK_TEST_DB_NAME 指定的独立测试库；其他服务使用独立测试 collection/bucket，禁止改写或清空现有业务数据。需要服务或模型的测试必须实测，不能把未执行的集成测试记为通过。
6. 将实现内容、验证结果和未验证项写入本地 `.sandcastle/results/issue-<编号>.md`，明确标记为完成或阻塞。
7. 检查 diff、敏感信息和 `git diff --check`，提交本次任务相关的文件及结果记录。commit 使用 `RALPH: <任务说明> (#编号)`。保持 Issue 打开，由用户审阅结果后决定发布和关闭。

## 执行边界

- 使用启动器创建的独立分支，不切回或合并到主分支，不 push，不创建 PR，不写 GitHub 评论、不关闭 Issue。
- 不自动连接券商或交易，不修改实际持仓，不部署，不接入付费数据源。
- 复盘输入是自然语言，不要求用户填结构化交易表；不做牛熊/震荡市场阶段分类。
- 不打印、提交或复制密钥。`.sandcastle/.env` 与本机 Codex 配置仅用于运行环境。
- 不使用交互式命令。如果外部依赖或需求缺失导致阻塞，记录原因并结束当前运行。
- 所有可执行候选均完成、为空或阻塞时，输出 `<promise>COMPLETE</promise>`。

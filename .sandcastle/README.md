# Sandcastle 本机运行

使用 Ubuntu-24.04 / WSL2、bwrap 和 Codex。Windows 项目仍位于 `D:\Project\shopkeeper-brain`。
Linux 的 npm 依赖存放在 `~/.local/share/shopkeeper-brain/sandcastle/`，不复用项目根目录的 Windows node_modules。
业务任务的 Python 虚拟环境创建在沙箱 `/tmp/shopkeeper-venv`，避免 WSL Windows 挂载盘上的权限问题；沙箱结束后自动清理。

## 检查环境

在 Windows 项目目录执行：

```powershell
npm run sandcastle:check
```

检查会创建临时本地副本，在真实 bwrap 沙箱中验证 Linux 工具、GitHub 读取权限、三种存储服务的认证读取和一次 Codex 模型调用，然后清理副本。模型调用使用当前配置的模型与账户额度。不会处理业务 Issue。

## 启动一次 AFK

先审阅并提交当前项目改动，使 Git 工作区干净，再执行：

```powershell
npm run sandcastle
```

每次默认最多执行 1 轮。读取 ready-for-agent 标签，排除规格 #1，检查原生 blocker 后选择任务。
结果保留在新的 codex/sandcastle-* 分支，记录在 `.sandcastle/results/`；不自动合并、push、部署或关闭 Issue。
日志位于 `.sandcastle/logs/`。运行期间按 Ctrl+C 停止。

## 本机配置

- `.env`：GitHub token、模型凭据、模型名称和 Git 身份；已被 Git 忽略。
- `codex.local.toml`：本机模型网关配置；已被 Git 忽略，通过 env_key 读取凭据。
- `.env.example`：可以提交的占位示例。
- `SANDCASTLE_MAX_ITERATIONS`：1 到 20，默认 1。
- `SANDCASTLE_WSL_DISTRO`：Windows 启动器读取的可选环境变量，默认 Ubuntu-24.04。
- `PIP_INDEX_URL`：默认阿里云 PyPI 镜像，可在本机 `.env` 覆盖。

存储连接从 `knowledge/.env` 按允许列表加载；应用 LLM 仅注入 `OPEN_API_BASE`、`OPEN_API_KEY`、`LLM_DEFAULT_MODEL`，用于无敏感文本的真实抽取验收。不会注入 MinerU 或 Web MCP 密钥。沙箱中的 MongoDB 写入测试使用每次启动生成的 `AFK_TEST_DB_NAME` 独立测试库。

在 Windows 更新本机凭据：

```powershell
python .sandcastle/refresh-auth.py
```

该脚本复用当前 gh 登录和 CODEX_HOME 下的自定义 provider 配置，不会打印 token。它不会改变 Windows Codex 的配置。

## Linux 依赖与验证范围

需要 Linux 原生的 Node.js 22、npm、Codex CLI、git、gh、bubblewrap、python3、python3-venv 和编译工具。
Sandcastle 固定为 0.14.0；安装的 Codex CLI 与本机 Windows 版本一致，为 0.169.0。
本机 WSL 的 Git 已仅信任此项目目录及其 `.git` 目录，以兼容 Windows 挂载盘的文件归属。

存储检查客户端位于 `~/.local/share/shopkeeper-brain/storage-probe`，在沙箱中只读挂载，不挂载整个用户目录。

MongoDB 使用客户端 `ping` 与集合列表，MinIO 使用认证后的 bucket 存在检查，Milvus 2.5.5 使用 REST v2 的集合列表接口。检查仅做读取；Milvus gRPC/向量检索留给相关任务做集成验收。

初始化命令（WSL 项目目录）：

```bash
python3 -m venv ~/.local/share/shopkeeper-brain/storage-probe
~/.local/share/shopkeeper-brain/storage-probe/bin/python -m pip install --index-url https://mirrors.aliyun.com/pypi/simple -r .sandcastle/storage-requirements.txt
```

2026-11-27 已执行 `npm run sandcastle:check` 并通过：Linux Node.js 22.23.5、Codex CLI 0.169.0、bwrap 沙箱内 GitHub 读取、三种存储的协议级读取，以及模型返回 `SANDCASTLE_OK`。每轮业务执行状态以 `.sandcastle/results/` 和本地日志为准。

VMware/CentOS 的 MongoDB、Milvus REST、MinIO 已分别从 WSL 和 bwrap 沙箱验证读取成功。此检查不等于业务验收；完整 Python 应用依赖、模型文件、Milvus gRPC/向量检索和业务写入仍由相应任务验证。
不能把 Windows 的 localhost 地址直接当作 WSL 或虚拟机中的服务地址。

## 主机补验

沙箱不包含本机 BGE 模型、MinerU 凭据或浏览器。不要在每轮中重复下载模型或 Chromium；先完成小依赖的单元测试和独立存储验证，再由主协作者使用 Windows 既有 Python 环境与浏览器执行完整 RAG、PDF 与界面验收。结果须区分“沙箱通过”和“待主机验收”，补验完成后再记录最终结论和 push。

2026-11-27 的 #3 曾因沙箱浏览器下载耗时而改用主机浏览器完成验收。后续轮次沿用此分工，避免相同环境准备重复消耗时间。

# 复盘改进落实记录

状态：五项已完成本地实现与验证。以下为提交前的验证快照，GitHub CI 的后续运行结果另行核对。

## 1. 统一离线检查与 CI

- `scripts/check.py` 与 `npm run check` 共用入口，依次执行 Ruff、Git 空白检查、9 个前端脚本语法检查和离线回归，任一步失败即停止。
- `requirements-check.txt` 固定检查环境直接依赖，不安装 Torch、FlagEmbedding 或下载模型。自动任务、dotenv 与真实集成开关由检查入口关闭。
- `.github/workflows/check.yml` 在 push/PR 时运行同一入口，Python 3.12、Node.js 22、只读仓库权限，Actions 固定到已核实的提交 SHA。
- 新环境暴露的缺失导入和两个测试环境依赖已修复：演示入口补 `get_recent_messages` 导入；公共导入测试显式配置测试集合；查询状态隔离测试注入 mongomock 运行资源。

## 2. 验收报告绑定代码版本

- 新增 `scripts/acceptance_report.py`，全部 9 个 `verify_*.py` 接入，在应用模块导入前保存入口快照，在验收及清理结束后再次核对。
- 记录 HEAD、Python 版本、逐文件与整体 SHA256、起止时间、`source_unchanged` 和变更文件。代码变化或 Git 元数据不可读时撤销整体验收通过标志，保留各项实测结果。
- 未提交但稳定的代码可以验收；日志和结果文档不影响散列。报告不保存源码、环境凭据或模型原文。起止比较不覆盖中途改动后恢复、环境配置或外部数据变化。
- 个股风险验收新增 JSON 报告；机械测试检查各验收入口持续接入来源记录。

## 3. 本机服务启停与状态

- `scripts/service.py` 为 query/import 提供 start/status/stop，核对 PID、创建时间、命令、工作目录和带认证的本机控制接口。
- 支持重复启动查询状态、正常关闭、停止后再次启动；状态展示当前调度时间、源码是否变化，保留清理失败及非零退出码。
- 日志和运行状态位于忽略目录 `.sandcastle/logs/services/`；不接管其他启动方式的进程，不配置开机自启或睡眠管理。
- 随机独立端口验证了两种服务的真实启动、OpenAPI 路由、重复启动、拒绝无凭据停止、正常停止及重启；另覆盖非托管端口、过期 PID 和清理失败。测试实例全部退出。

## 4. 最小导航

AGENTS.md 只新增开发入口指针。`docs/agents/development.md` 集中说明轻量环境、Windows/WSL 区别、统一检查、服务工具和验收来源；README、个人指南、自动任务指南及 Sandcastle 提示均指向对应内容。

## 5. 搜索与读取

开发指南提供 `rg --files`、`rg -n ... -g '*.md'` 和 PowerShell `Get-Content -LiteralPath ... | Select-Object -Skip ... -First ...` 方案，先定位再局部读取，输出截断时缩小范围。

## 实测证据

| 检查 | 结果 |
| --- | --- |
| 新轻量环境 `python scripts/check.py` | 716 passed、24 skipped，45.81 秒；Ruff、Git 与 JavaScript 检查通过 |
| `pip check` | 无依赖冲突；Python 3.12.13；未安装 Torch/FlagEmbedding |
| CI YAML | 本地解析和入口核对通过；此快照未包含远端执行结果 |
| MongoDB/子进程恢复验收 | 25/25 通过；固定行情和时钟、真实 MongoDB/子进程/APScheduler |
| 资源清理 | worker 停止与登记集合清理均成功；复查独立测试库无集合 |
| 原业务查询服务 | 127.0.0.1:8001 仍由 PID 52744 监听；OpenAPI 返回 200，查询路由存在 |

恢复验收报告：`.sandcastle/logs/scheduling_recovery_172c89c6b2bb.json`（本机忽略文件）。独立测试库：`afk_retro_b90f8dd1b851`。

- HEAD：`a5dc437799b8e574afc3ac7286ec7c8b3ff5c4d6`，含本轮未提交代码。
- 源码 SHA256：`98253a9f1ca4e466234dd2d1b6ef4bcf61570f39273180d77ab6b43879f43985`。
- `source_unchanged=true`，`changed_files=[]`，`passed=true`。
- 验收时间：2026-11-28 23:54:46 至 23:54:51（Asia/Shanghai）。

## 仍待实际观察

本轮未重跑完整模型/行情验收，也未将 24 个跳过的真实集成用例计为通过。原观察服务继续运行，实际交易日观察仍待 2026-11-30、2026-12-01 收盘后按 `docs/scheduled-operations-validation.md` 保存只读快照。进程恢复测试不能替代连续交易日运行结论。

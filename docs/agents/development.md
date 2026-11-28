# 开发与验收入口

在项目根目录执行命令。离线检查、真实服务验收分别使用适合的 Python 环境。

## 统一检查

Windows PowerShell 首次准备轻量环境：

```powershell
python -m venv .sandcastle/logs/check-venv
& '.sandcastle/logs/check-venv/Scripts/python.exe' -m pip install -r requirements-check.txt
& '.sandcastle/logs/check-venv/Scripts/python.exe' scripts/check.py
```

已有该环境时直接执行最后一条。使用 Node.js 22 或兼容的本机版本完成 JavaScript 语法检查。激活环境后也可执行 `npm run check`。

WSL/Linux 在 Linux 文件系统准备环境，避免 Windows 挂载盘的虚拟环境权限与链接问题：

```bash
python3 -m venv /tmp/shopkeeper-check-venv
/tmp/shopkeeper-check-venv/bin/python -m pip install -r requirements-check.txt
/tmp/shopkeeper-check-venv/bin/python scripts/check.py
```

检查范围由 `scripts/check.py` 定义：Python 致命语法/名称问题、Git 空白错误、前端 JavaScript 语法，以及离线回归。首次失败即返回非零退出码。该入口关闭自动任务、禁用 dotenv 和真实集成开关，使用轻量依赖且禁用模型下载；测试服务仅在随机本机端口启动。真实集成用例默认跳过，不能计入通过数。

`.github/workflows/check.yml` 在 push/PR 时使用同一入口。工作流本地检查通过与 GitHub 实际运行通过分别记录。新增离线测试目录时更新检查入口；验收脚本的来源记录接线由 `tests/tooling` 自动核对。

## 真实验收与来源记录

本机已配置模型的环境是 `.sandcastle/logs/host-check-venv/Scripts/python.exe`，配置保留在 `knowledge/.env`。轻量环境不包含完整向量模型。WSL 使用自己的环境；若提供 `AFK_STORAGE_PYTHON`，该环境仅用于既有存储探针，不向其中安装依赖。真实验收使用独立 `AFK_TEST_DB_NAME` 和本轮随机集合，确认 worker 停止后再清理。恢复验收及实际交易日观察见 [自动任务验证](../scheduled-operations-validation.md)。

所有 `scripts/verify_*.py` 在验收开始/结束采集 `provenance`：HEAD、Python 版本、逐文件 SHA256、整体 SHA256、起止时间和 `source_unchanged`。范围是 Git 可见的 `knowledge/`、`scripts/`、`tests/`、`.github/` 及根目录依赖/检查配置，包含未提交和未忽略的新文件；排除 `.env`、`.env.*`、字节码、日志和结果文档。报告只保存文件名与散列。

验收期间冻结代码。起止代码或 HEAD 不同、Git 信息不可读，均取消整体验收通过标志，并保留已执行检查与 `changed_files`。允许稳定的未提交工作区；报告须同时引用 commit 与整体散列。此记录不锁定环境变量、模型、数据库内容或全部传递依赖，也不能检测改动后又恢复的中间状态。结束后继续修改代码，旧报告仅代表原散列对应版本。

## 本机服务管理

在安装了应用依赖（包含 psutil）的环境执行：

```powershell
& '.sandcastle/logs/host-check-venv/Scripts/python.exe' scripts/service.py start query
& '.sandcastle/logs/host-check-venv/Scripts/python.exe' scripts/service.py status query
& '.sandcastle/logs/host-check-venv/Scripts/python.exe' scripts/service.py stop query
```

将 `query` 换成 `import` 管理导入服务。默认查询端口 8001、导入端口 8000；`--port` 指定其他端口，`start ... --no-schedules` 用于显式禁用自动任务。已运行实例的 `start` 返回其状态，改变启动参数需要先停止再启动。

状态包含当前 PID、任务下次运行时间、端口与源码是否变化。`running` 代表服务启动完成，不代表模型或存储可用。日志与状态放在被 Git 忽略的 `.sandcastle/logs/services/`；停止后仍保留日志和清理结果，清理失败返回非零退出码。

工具核对进程启动时间、命令和控制接口，只管理由它启动的实例。已有前台 uvicorn 或其他监听者显示 `unmanaged_port_in_use`；保持原启动方式停止它，端口空闲后才可切换。`stop` 请求正常关闭，30 秒仍未退出时返回失败并保留现场，可继续查看状态与日志。脚本不配置开机自启、不阻止睡眠。实际使用步骤见 [个人使用指南](../personal-use-guide.md)。

## 搜索与局部读取

先定位文件和命中位置，再读需要的片段；文件类型交给 rg 的 `-g`，避免将 `docs/*` 作为 Windows 字面路径：

```powershell
rg --files docs scripts knowledge
rg -n 'RuntimeConfig|QueryRuntime' knowledge scripts -g '*.py'
rg -n '验收|调度' docs -g '*.md'
Get-Content -LiteralPath 'scripts/service.py' | Select-Object -Skip 60 -First 50
```

独立搜索可同批执行；根据结果选择下次读取范围。输出截断时缩小目录、匹配条件或行数，再读取缺失片段。

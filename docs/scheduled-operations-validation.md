# 自动任务与跨交易日验证

自动摘要与全市场采集依附查询服务（8001）的生命周期。Asia/Shanghai 工作日默认 15:30 生成摘要、16:00 开始采集，交易日历明确休市时跳过。服务离线不自动补跑；上线后在页面指定日期补生成或补采。完整约定见 [每日摘要](daily-summary.md)、[行情采集](market-collection.md) 和 [个人使用指南](personal-use-guide.md)。

## 可重复的恢复验收

`scripts/verify_scheduling_recovery.py` 使用真实 MongoDB、真实子进程及 APScheduler executor，通过服务/API 检查行为。必须指定独立 `AFK_TEST_DB_NAME`，脚本通过 `RuntimeConfig.acceptance` 拒绝业务库与系统库，使用随机集合；不修改业务配置。

```powershell
Set-Location D:\Project\shopkeeper-brain
$env:AFK_TEST_DB_NAME = 'afk_recovery_' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
& '.sandcastle/logs/host-check-venv/Scripts/python.exe' scripts/verify_scheduling_recovery.py
Remove-Item Env:AFK_TEST_DB_NAME
```

验收覆盖：

- 已有成功摘要重新生成过程中，结束本次创建的子进程；有效租约阻止重复任务，过期返回 `lease_expired`，旧成功结果保留，重试后清空错误码。
- 51 条固定行情中，保存前 50 条后结束子进程；恢复只采缺失的 1 条，旧值保持一致，成功后重复请求不再执行。
- 模拟日期从 2026-11-26 推进至 2026-11-27，验证两日记录保留；真实 scheduler executor 连续触发两次，只发布一条当日摘要。
- 周五任务时点之后的下一次 cron 为周一，时区 Asia/Shanghai。

行情与时钟使用明确标记的固定样本，未调用外部行情或模型。租约过期通过注入时钟推进验证，不需要实际等待 300 秒。这不能替代真实行情、模型稳定性或连续多个交易日观察。旧 worker 的 fencing 和休市/未知日历行为同时由现有回归测试验证。

结束时先停止 scheduler、子进程和 worker，再删除本轮登记的随机集合；无法确认停止则保留集合并报告失败。报告保存在被 Git 忽略的 `.sandcastle/logs/scheduling_recovery_*.json`。

报告中的 `provenance` 绑定验收起止代码版本与散列；`passed` 为真还要求 `source_unchanged` 为真。验收运行期间保持代码不变，完成后再开始下一项修改。范围与限制见 [验收来源记录](agents/development.md#真实验收与来源记录)。

## 实际交易日观察

按个人使用指南启动查询服务，保持电脑、虚拟机与所需存储服务运行。此服务不会配置开机自启，也不阻止系统睡眠。调度器在服务停止时停止。

通过服务工具启动的实例可用 `python scripts/service.py status query` 检查进程、源码变化及下一次任务时间；只读业务快照仍使用下方脚本。服务状态不代表当日任务已成功。

在目标交易日收盘任务执行后，使用只读脚本保存快照：

```powershell
Set-Location D:\Project\shopkeeper-brain
& '.sandcastle/logs/host-check-venv/Scripts/python.exe' scripts/inspect_scheduled_operations.py --date 2026-11-30
& '.sandcastle/logs/host-check-venv/Scripts/python.exe' scripts/inspect_scheduled_operations.py --date 2026-12-01
```

日期应替换为实际观察日期。脚本仅请求 `/openapi.json` 核实服务入口，然后直接读取 MongoDB 状态；不调用可能回收过期租约的摘要/采集 GET 接口，不创建索引，不触发任务。报告不包含策略、复盘或摘要原文；只记录状态、次数、时间、固定摘要错误码、行情数量及来源。退出码只表示服务可达与存储读取成功，不能当作任务成功。

每个交易日核对：

1. 15:30 后摘要记录对应目标日期，正常完成时有 `generated_at`；失败读取 `error_code`，不能以历史成功结果替代当日成功。
2. 16:00 后采集应进入运行状态。任务预算为 20 分钟，再核对终态、总数、成功数和缺失数；`partial` 或来源目录不完整必须保留说明，不当作全市场完整。
3. 下一交易日新增自己的日期记录，前一天摘要和行情仍可读取；没有重复发布或把旧行情日期改成今天。
4. 服务异常退出后检查记录和租约；摘要 300 秒、采集 180 秒租约过期后可接管。通过页面读取状态会执行回收，随后手动重试。只读检查脚本不会改变状态。

实际观察完成至少两个交易日后，才能补记连续运行结论。验收脚本通过、服务正在运行、cron 已登记，均不等于已经完成实际跨日验收。

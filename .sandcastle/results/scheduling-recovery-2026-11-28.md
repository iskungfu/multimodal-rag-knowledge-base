# 调度恢复验收与实际观察准备（2026-11-28）

本轮基于 `a5dc437`，完成恢复验证、可重复验收脚本及只读观察脚本；没有修改业务逻辑或业务配置。以下记录为提交前的验收快照。

## 已验证

- 日历、全市场采集、摘要及运行生命周期回归：**137 passed，6.99 秒**。
- `scripts/verify_scheduling_recovery.py`：**25 项检查通过**。使用真实 MongoDB、真实子进程异常退出、真实 scheduler executor 和 API；行情/日期为固定样本。
- 进程中断后保留首批 50 条行情，恢复仅补采 1 条；有效租约阻止并行任务，完成记录去重。
- 摘要租约过期后报告 `lease_expired`，旧成功结果保留，重试完成且错误码清空。
- 模拟两个交易日分别保存记录，周末 cron 跳过；不将此项描述为实际跨交易日运行。
- 独立库 `afk_recovery_20261128_38f6dee898` 清理后零集合。报告 `.sandcastle/logs/scheduling_recovery_743c949f4bc3.json` 全部清理标记为真。
- 两个新增脚本语法有效；实际只读观察脚本已运行成功。

## 实际运行状态

北京时间 2026-11-28 23:19 启动本机查询服务，绑定 `127.0.0.1:8001`，保留现有 `knowledge/.env`。后台启动器 PID 为 53252，实际服务进程 PID 为 52744；记录仅表示本次检查时的进程，不应日后仅按旧 PID 操作进程。

应用 lifespan 内记录了已启动的两项任务：

| 任务 | 下一次触发（Asia/Shanghai） |
| --- | --- |
| 每日投资摘要 | 2026-11-30 15:30 |
| 全市场收盘采集 | 2026-11-30 16:00 |

两者 `max_instances=1`、`coalesce=true`、`misfire_grace_time=3600`。来源 BaoStock 本次返回 2026-11-28 休市、2026-11-30 与 2026-12-01 开市；最终仍要求目标日期真实行情核验。

23:20 只读快照确认查询入口可达、业务库可读，2026-11-30 尚无摘要/采集记录和行情（该日期仍在未来）。未提前伪造记录或触发未来日期任务。

相关本地记录：

- `.sandcastle/logs/scheduling-observation-service.json`：本次服务实际启动与调度信息。
- `.sandcastle/logs/scheduling-calendar-20261128.json`：来源日历结果。
- `.sandcastle/logs/scheduled_operations_2026-11-30_de205ff1.json`：启动后的只读基线。
- `.sandcastle/logs/scheduling-observation.stdout.log`、`.sandcastle/logs/scheduling-observation.stderr.log`：本次后台服务日志。

## 下一次核对

保持电脑、虚拟机及相关服务运行。11 月 30 日 15:30 后检查摘要，16:00 后检查采集启动、16:20 后检查终态；12 月 1 日重复核对并检查前日记录保留。实际未覆盖的日期不能记为验收通过。

可重复操作、恢复边界与只读命令见 [自动任务与跨交易日验证](../../docs/scheduled-operations-validation.md)。当前没有设置提醒、自动监控或开机自启；后台查询服务按应用自身已有调度运行。服务离线漏掉的任务仍需通过页面手动补跑。

# 金融平台设计借鉴与可靠性修复（2026-11-28）

用户要求继续解决档案降级、复盘脱敏长度和摘要校验诊断问题，并参考 GitHub 金融平台优化系统。使用 ask-matt 路线与 codebase-design 设计原则，在当前工作区完成实现；未提交、推送或发布。

## 设计依据

[金融平台设计研究](../../docs/research/financial-platform-design-2026-11-28.md) 核实了 OpenBB、QuantConnect LEAN、vn.py 的固定源码提交及 9 个来源链接。借鉴转换后统一校验、独立风险职责、明确来源及错误状态，保留双 FastAPI、HTML、LangGraph 和现有存储。未复制外部框架代码或增加依赖。决策见 [ADR-0013](../../docs/adr/0013-local-failure-and-transformed-data-contracts.md)。

## 修复后的行为

- 档案读取或模式校验失败时，关闭个人策略检索并明确告知用户；公共知识、复盘及已经取得的行情继续处理。同步、SSE、历史返回一致；恢复后读取当时模式，不扩大个人策略范围，不改行情风险等级。
- 复盘输入和脱敏全文共用 10000 字符限制。新增/编辑在写入前拒绝超长内容，HTTP 422 不携带原文，也不更改旧记录或索引。临界全文可完成索引和引用，不截断交易条件。旧超长记录保留完整可读，但标记 `invalid_text`、提示编辑，不能补建无效索引；即便数据库旧状态是 completed，也不再向用户表示可检索。编辑纠正后恢复正常。
- 摘要模型错误使用固定原因码，区分请求不可用/超时、响应不完整、无效 JSON/schema、未知来源、原文不匹配和未知行情依据等。失败码保存到摘要记录、日志及验收报告；重试清除旧码，失败不覆盖上次成功结果。原文与行情依据校验继续严格执行，不通过重试放宽规则。

## 验证

解释器：`.sandcastle/logs/host-check-venv/Scripts/python.exe`。不安装依赖或下载模型。

1. 先用回归测试复现档案异常、脱敏增长写入、缺少摘要错误分类及旧超长索引假完成，再修复到通过。
2. 最终离线回归 **704 passed、24 skipped，26.04 秒**：market、review、strategy、risk、summary、investment、query/test_rag_regressions、test_runtime。跳过项是未显式启用的真实集成测试。
3. 真实独立摘要主机验收 **17 项通过**，68.31 秒，含真实导入/BGE、模型、API、引用、实际调度与清理。报告 `.sandcastle/logs/summary_accept_8e70b03d23ec490b.json`。
4. 真实完整投资助手主机验收 **392 个唯一检查通过**，483.92 秒，`passed=true`、`full_acceptance=true`、`personal_mvp_acceptance=true`，没有 pending。覆盖三路真实检索/重排/模型、API/SSE/历史、行情、故障注入、档案模式/空范围、复盘纠错、手动摘要和实际调度。报告 `.sandcastle/logs/financial-reliability-07aa49e9fb/investment-host.json`。
5. 真实运行结束后审计：独立库 `afk_reliability_07aa49e9fb` 零集合；Milvus 相对基线无新增、无缺失。报告同目录 `audit.json`。
6. Python 改动语法解析、`node --check knowledge/front/assets/js/reviews.js`、`git diff --check` 通过。

完整真实验收在最后的旧超长索引展示修复前启动；该修复由之后的最终 704 项回归及独立复查验证。记录/调度报告新增 `error_code` 镜像字段也晚于进程启动，因此本次成功报告不含这些镜像字段；模型失败观察器分类在进程启动前已生效。真实模型本次未触发失败，不能将成功报告当作历史失败根因证据。

## Standards

独立规范审查未发现需要修改的硬性规范违反或值得报告的代码异味。档案范围、转换后约束、固定错误码及隐私处理符合 ADR-0005、ADR-0007、ADR-0013。

## Spec

独立需求审查发现 1 项 P2：旧超长记录仍可补索引并显示 completed，实际搜索为空。已修复索引领取、全文校验及展示状态；独立复现确认原文与数据库旧记录不变、没有向量写入，4 项定向测试通过，复查无剩余发现。

审查基准沿用 `main`（`6b9312b4962ec30c1b9846b554aca4830c47d933`），已提交比较为 `git diff main...HEAD`；本轮工作未提交，实际新增审查明确补看 `git diff HEAD -- <相关文件>` 和未跟踪文件，不把旧提交或前轮 runtime 重构重复算入本次结论。规范 0 项；需求 1 项已解决、剩余 0 项。

## 仍需保留的边界

前轮两次摘要 `ValueError` 的具体分支没有留下原始诊断，本轮无法反推唯一原因。新分类及严格失败处理已完成；本轮四次真实摘要模型选择均成功，不等于未来不会失败或长期稳定性已验证。旧超长复盘需用户编辑后恢复可检索，不自动修改用户资料。跨交易日长期调度、所有外部行情来源可靠性不在本轮完成声明内。

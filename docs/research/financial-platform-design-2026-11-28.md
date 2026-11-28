# GitHub 金融平台设计对照（2026-11-28）

本次研究服务于个人投资助手的三个已确认问题：档案读取失败覆盖独立行情、复盘脱敏后的长度与检索约束不一致，以及摘要模型校验失败缺少具体原因。研究结论是强化现有模块的职责和数据契约；以下建议不代表外部框架已替本项目验证正确性。

研究通过 GitHub MCP 读取官方仓库固定提交的原始源码。网页搜索工具本轮返回空结果，因此没有使用搜索摘要或第三方架构文章作为证据。读取日期为 2026-11-28；固定提交可重现本次观察，不承诺之后分支不再变化。请求 `OpenBB-finance/OpenBB` 时，GitHub 返回的规范仓库链接为 `openbq-org/OpenBB`，以下按实际返回链接引用。

## 原始来源及观察

| 平台与核实提交 | 源码中直接观察到的设计 | 对本项目的意义 |
| --- | --- | --- |
| OpenBB：`f0f1852b96dd3fc4d5868f8863cbe053ff49c6cd`，提交日期 2026-11-18 | `Fetcher.fetch_data` 依次转换查询、提取来源数据、转换结果；历史行情使用标准数据模型。`AnnotatedResult` 将结果和元数据分开，异常区分空结果和认证失败。[O1][O2][O3][O4] | 在转换后的数据上建立统一约束；让调用方区分“没有资料”和“资料读取失败”。它没有在这些文件中提供通用的多来源自动容错保证。 |
| QuantConnect LEAN：`c1c7ef32ed2cbcf65c7b81ad73d7f70bce97a3fb`，提交日期 2026-11-27 | `ProcessInsights` 先创建组合目标，再交给 `ManageRisk`，风险目标覆盖同证券目标后交给执行模块；示例风险模型用明确的持仓收益率阈值产生目标。[L1][L2] | 风险计算必须有独立职责和确定性输入输出。借鉴的是风险结果的权威性，不能把其交易风控阈值移作本项目的个股风险分级，也不能据此宣称盈利或预测能力。 |
| vn.py：`c6e231caf32b7fc97e6459817fff66458cf7e7c4`，提交日期 2026-10-06 | `BaseGateway` 接受事件引擎和来源名称；数据对象统一携带 `gateway_name`，日志另带时间、级别和来源。`MainEngine` 登记各 gateway/engine，退出时先停止事件引擎，再关闭登记对象。[V1][V2][V3] | 保留结果来源、职责名称和生命周期归属；已获得的结果与后续可选处理分离。这些文件本身不证明任意异常都能被隔离，因此本项目仍须自行验证故障路径。 |

## 建议落地的三个改动

1. **档案故障局部降级，保留独立行情。** 读取失败应产生明确的“档案暂不可用”状态，不能解释为默认档案或全部策略。已经核验的行情及个股风险结果继续进入最终回答；个人策略范围无法确认时，关闭该路径，不能回退到全部策略。是否继续公共知识和复盘检索，应由各路径自身的归属校验决定。沿用现有接口即可，不需要事件总线。验证同步回答、SSE、会话历史一致；失败前后同一行情的风险等级不变；恢复后重新读取当前模式。

2. **把脱敏转换和文本约束放在同一处。** 借鉴 OpenBB 的转换流程和标准模型，让新增、编辑、索引、检索使用同一份脱敏后文本契约。当前输入长 10,000 字、脱敏后长 10,006 字的情况必须有明确结果：在写入前拒绝超出既定存储契约的输入，或者完整支持经过界定的转换后上限，不能成功保存后静默过滤，更不能截断交易条件、否定或未成交语义。回归测试需覆盖编辑失败不改旧记录、临界长度、合法长原文的完整引用和已有数据兼容；具体上限由本仓库实现统一决定，外部平台没有给出本项目复盘的正确数字。

3. **模型失败使用稳定、可脱敏的原因分类。** 参考 OpenBB 的异常区分和 vn.py 的来源化日志，将未完成响应、无效 JSON、结构不符、未知片段、原文不匹配、未知行情依据分开。分类使用固定字符串，记录处理阶段、尝试次数和有限的数量信息；不存模型原文、用户原文、任意异常消息或凭据。摘要失败不发布伪完成内容，已经核实的风险结论不交给模型重写。先用定向回归证明每个原因可观察，再用真实验收定位曾出现的 `ValueError`；复测成功不能证明历史失败原因。

这三个建议由本项目现有需求、故障复现及外部职责划分共同推导，不是声称外部平台采用了同样的 RAG 降级实现。

## 已有设计与保留范围

本项目 ADR-0004、ADR-0007 已要求偏好不能修改行情规则风险；ADR-0005 要求写入和返回两端脱敏；ADR-0012 的 `QueryRuntime` 已集中应用实例、连接和退出归属。这些与上述职责划分相容，宜补齐行为而不是重新建立框架。行情的 `DailyQuote`、`SnapshotUnavailable` 也已有标准字段及固定原因分类，可复用项目自身的表达方式。

保持 ADR-0006 的双 FastAPI 进程、现有 HTML 和 LangGraph；不增加数据库、消息总线、框架依赖或新的后台常驻进程。不引入 LEAN 的订单执行、持仓、回测系统，不引入 vn.py 的交易柜台与推送能力，也不把 OpenBB 的整个插件注册体系复制到目前有限的行情来源上。此次只借鉴职责和验证方式，不复制这些仓库的实现代码。

## 固定源码索引

- [O1：OpenBB Fetcher 转换流程](https://github.com/openbq-org/OpenBB/blob/f0f1852b96dd3fc4d5868f8863cbe053ff49c6cd/openbb_platform/core/openbb_core/provider/abstract/fetcher.py)
- [O2：OpenBB 历史行情标准模型](https://github.com/openbq-org/OpenBB/blob/f0f1852b96dd3fc4d5868f8863cbe053ff49c6cd/openbb_platform/core/openbb_core/provider/standard_models/equity_historical.py)
- [O3：OpenBB 数据与元数据](https://github.com/openbq-org/OpenBB/blob/f0f1852b96dd3fc4d5868f8863cbe053ff49c6cd/openbb_platform/core/openbb_core/provider/abstract/annotated_result.py)
- [O4：OpenBB 来源异常类别](https://github.com/openbq-org/OpenBB/blob/f0f1852b96dd3fc4d5868f8863cbe053ff49c6cd/openbb_platform/core/openbb_core/provider/utils/errors.py)
- [L1：LEAN 组合构建、风险和执行的调用顺序](https://github.com/QuantConnect/Lean/blob/c1c7ef32ed2cbcf65c7b81ad73d7f70bce97a3fb/Algorithm/QCAlgorithm.Framework.cs)
- [L2：LEAN 确定性风险规则示例](https://github.com/QuantConnect/Lean/blob/c1c7ef32ed2cbcf65c7b81ad73d7f70bce97a3fb/Algorithm.Framework/Risk/MaximumDrawdownPercentPerSecurity.cs)
- [V1：vn.py gateway 接口及日志来源](https://github.com/vnpy/vnpy/blob/c6e231caf32b7fc97e6459817fff66458cf7e7c4/vnpy/trader/gateway.py)
- [V2：vn.py 数据、来源和日志结构](https://github.com/vnpy/vnpy/blob/c6e231caf32b7fc97e6459817fff66458cf7e7c4/vnpy/trader/object.py)
- [V3：vn.py 统一装配和退出](https://github.com/vnpy/vnpy/blob/c6e231caf32b7fc97e6459817fff66458cf7e7c4/vnpy/trader/engine.py)

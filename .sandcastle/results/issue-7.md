# Issue #7：个性化投资助手三路召回

状态：实现、双轴审查和最终验收完成。工作分支：`codex/sandcastle-2026-11-27T16-09-31-667Z`；GitHub Issue 保持 OPEN。

## 范围与基线

任务仓库为 iskungfu/multimodal-rag-knowledge-base，父规格 #1，本轮仅实施 #7。GitHub 原生 blocker #3、#5 仍 OPEN，未修改状态、依赖或评论。根据用户持续授权，采用当前已验收分支的凭据：`2bb3969`、#3 实现 `b6f6dde`、#5 实现 `26053fd` 与验收 `4550158`；祖先关系已核对。#2、#4、#6 功能沿用本分支实现和验收记录。

准备提交 `21efe26` 为本轮审查基点。Issue #7 旧正文将行情作为第三路，遵循更新后的 GLOSSARY/spec/ADR-0003：personal、knowledge、review 三路向量融合，行情独立。

## 执行方式

Sandcastle 环境检查通过：WSL2/bwrap、GitHub 读取、存储认证读取、应用 LLM、Codex 模型。沙箱多次长时间等待，在约50分钟内只产出复盘索引和检索部分；主协作者中断该 agent，保存全部改动至 `d3c894b`，接管本机完成，未丢弃成果。该次沙箱退出为中断失败，不记录为完整 AFK 成功。临时 worktree 已移除，主工作区切到本轮分支。

主机实现检查点 `6028e2b`；之后继续按实际验收与双轴审查修复。不合并 main、不部署、不关闭 Issue、不发送评论。

## 实现

- 新投资 LangGraph 执行三路检索、RRF、BGE rerank、模型选择和服务端核对提示；旧商品 RAG 保留。
- personal/default_user、knowledge/public、review/default_user 在查询和返回端隔离。个人策略校验完成状态，复盘校验发布版本与 manifest；不同来源同数字 chunk_id 不串源。
- 自然语言复盘先保存脱敏原文；独立索引状态、有限重试、租约、发布版本，按记录补索引。修复发布响应丢失误删、抽取晚完成导致旧摘要、长文静默截断；使用 token 预算切片，回答恢复全文前提。
- 个股先核实身份，风险和来源日期独立保留；大盘复用指数能力，不做市场阶段分类。简单追问仅使用有界的用户上下文，历史 AI 回答不作为证据。
- 模型依据问题和行情只选择真实资料来源编号，服务端绑定完整原文与来源；行情及其引用由服务端独立保留。保留策略条件及未成交语义。单路故障、全空、重排/模型失败明确降级。
- 服务端脱敏、唯一免责声明、SSE/status/history 一致；复盘面板增加索引状态与重试。

## 验收记录

- 初次主机全套单元/API回归 346 passed；索引等补齐后 377 passed。
- PDF+SSE 完整 RAG 通过，报告 `.sandcastle/logs/rag_accept_f0b7efa0327d.json`。
- 第一轮真实三路已通过 BGE/Milvus/RRF/reranker/模型三类事实、API/SSE/history、真实个股行情、证券追问与身份冲突；在后续个人问题模型选择检查失败，报告 `.sandcastle/logs/issue7-host-acceptance.json`，该轮整体 FAIL，所有独立资源已清理。未将降级当作模型选择成功。
- 相同个人问题独立实际模型探针正常返回并通过原文校验；增加仅记录异常类型的安全诊断后进行第二轮验收。
- 第二轮在个股模型选择处出现 `APITimeoutError`，明确是45秒调用超时，未破坏行情和原文降级；上限调整为90秒，无无限重试。第三轮在计划问题出现原文选择 `ValueError`，据此简化为模型只选择来源编号和行情ID，服务端附上真实原文，不再要求模型重新抄写。前三轮整体均记 FAIL，独立资源全部清理。
- 最终实现单元/API回归 **397 passed（19.35秒）**，覆盖 investment、market、review、strategy、risk、summary 和旧 RAG；日志 `.sandcastle/logs/issue7-final-regression-r5.log`。实际行情/摘要/复盘/LLM/MongoDB 集成 **24 passed（126.36秒）**，无 skip，日志 `.sandcastle/logs/issue7-live-integrations.log`。
- 真实证券路由探针5/5：换股追问、含股票一词的追问、个人策略加名称风险、指数与个股混问含/不含代码；报告 `.sandcastle/logs/issue7-live-routing.json`。

## 双轴审查

固定 `21efe26...6028e2b`，两名全新上下文分别执行 Standards/Spec；之后复核工作区修复。

- Standards 初审3项：追问新证券被忽略、策略关键词绕过名称核验、含“股票”的追问丢失上下文。已修复，复核原3项通过。补充指数代码括号/“代码”形式歧义已修，内存5/5通过。
- Spec 初审3项：追问错误继承、止损等意图漏路由、大盘吞掉个股。已修复并加定向/API测试。后续指数名配个股代码、带名称止损也已修复，最终复核通过；来源编号方案的白名单、额外字段拒绝、服务端原文绑定、条件与计划保留、脱敏和降级均通过。
- 第四轮在个股选择校验出现 ValueError，整体仍记 FAIL，全部资源清理。新增安全异常类型区分，且最多重试一次（每次90秒）；重试不把无效响应传回模型作为资料。该行为另有成功恢复/重复失败的明确测试，失败仍采用有据降级。
- 第五轮模型检查均通过至计划问题，但发现复盘“计划买入”被误识别为证券名称，整体 FAIL；已剔除交易上下文词并增加两个自然语言复盘回归。第六轮安全错误分类定位 `UnknownMarketSelection`：模型会复制提示示例中的行情ID占位文本。最终模型输出仅保留来源 reference，行情仍参与选择输入，独立行情引用由服务端输出，彻底取消模型写行情ID这一非必要步骤。两轮独立资源均清理。

## 最终真实验收

- 第七轮真实三路验收 **355/355 PASS**，`passed=true`、`full_acceptance=true`，10项清理检查全部成功；报告 `.sandcastle/logs/issue7-host-acceptance-r7.json`。实际使用本机 BGE-M3、BGE reranker、Milvus、MongoDB、LLM 和免费行情。覆盖三类独特事实、来源隔离、完整复盘条件、长文尾部召回、计划不等同成交、证券身份与追问、API/SSE/status/history、单路/全路空或失败、模型/重排故障和按记录补索引。
- 真实浏览器 **20/20 PASS**；报告 `.sandcastle/logs/issue7-browser.json`。自然语言保存、脱敏、planned、索引状态、503故障注入后的失败提示与真实重试恢复、流式三路问答、非流式计划核对、每条回答唯一免责声明均通过。390×844视口截图已目视检查，未出现横向溢出。测试服务经 stop-file 正常停止并完成清理。
- PDF + SSE 完整 RAG **22/22 PASS**；报告 `.sandcastle/logs/rag_accept_f0b7efa0327d.json`。包含真实 PDF/MinerU/BGE/Milvus/RRF/reranker/LLM/SSE/history，MongoDB、Milvus、MinIO 和临时文件已清理。
- 旧 schema 商品 RAG **17/17 PASS**；报告 `.sandcastle/logs/rag_accept_8949b5fd3900.json`，兼容原数据结构，检索、回答与历史删除通过。
- 公共知识 RAG + SSE **22/22 PASS**；报告 `.sandcastle/logs/rag_accept_299c014bd424.json`。真实模型来源选择、引用、唯一免责声明、流式终稿与历史一致性通过。
- 每日摘要最终回归 **16/16 PASS**；报告 `.sandcastle/logs/summary_accept_8352cfbd8f724152.json`，日志 `.sandcastle/logs/issue7-summary-regression-r3.log`。真实模型两次调用分别14.41秒、20.78秒；手动生成、真实调度重试、行情依据、条件保留和幂等均通过。
- 摘要前两次回归分别在调度重试和手动生成检查失败，报告 `summary_accept_7e725c62d21c431a.json`、`summary_accept_b1684eb7de734544.json` 保留，两次资源均已清理。原报告未记录足够阶段信息，不能断言失败均由超时导致。验收脚本补充了模型异常类型、耗时和应用固定状态诊断，并将调度等待由55秒调整为270秒，以覆盖服务已有的240秒预算；未改动摘要生产逻辑。最终通过不代表已证明外部模型的持续稳定性。

## 清理与提交检查

独立检查 `.sandcastle/logs/issue7-resource-audit.json` 确认两个测试库为空、无沙箱测试库、本轮所有测试向量集合及 MinIO bucket 均无残留。浏览器服务已正常退出，8003端口无监听，只保留主工作区，无临时 worktree。

`git diff --check`、7个前端 JavaScript 文件语法检查通过。本轮各提交及最终改动的配置敏感值和常见凭据模式扫描未命中；`.env` 与 `.sandcastle/logs` 均未纳入 Git。诊断日志仅在本机保留，验收结论随本报告提交。

最终模型验收证明的是“模型来源选择 + 服务端原文绑定”，不宣称模型逐字摘录或自由生成投资结论的可靠性。前六轮失败报告保留，最终通过结果不覆盖失败记录。

## 限制

单用户原型，无登录认证；规则脱敏不是任意隐私内容的完备识别。模型只选择资料形成核对提示，不能证明策略有效或前提已满足。CPU 首次模型加载耗时较长。真实交易日连续调度长期运行不在本轮验证范围。上线部署仍需独立评估身份、权限与容量。

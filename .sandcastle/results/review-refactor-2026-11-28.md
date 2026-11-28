# 金融平台代码审查修复与复盘索引重构

日期：2026-11-28，Asia/Shanghai。基线 `72d12ff416201aed389237828a5a3a702de46e96`；分支 `codex/sandcastle-2026-11-27T16-09-31-667Z`。用户授权先处理以 `41f5e09` 为起点的审查发现，再实施推荐的复盘索引重构。未新增业务工单，不合并 main、不部署、不关闭 Issue。

## 已修复问题

1. 个人策略切片丢失父标题适用条件：每个切片正文携带完整祖先标题和短章节引言，长正文重复携带前提，不跨分支。纯标题仍可导入；前提本身超预算则失败，不静默截断。旧策略需要重新导入原文，才能补齐历史索引中缺失的条件。
2. 同日冲突行情被 upsert 覆盖：写入前检查整个来源批次的证券、日期及同日指标一致性；拒绝冲突批次并走备用源，所有来源无有效数据则失败。相同重复行情合并，不缓存失败。
3. `/reviews` 参数校验回显敏感输入：查询应用对正文、额外字段名、路径与查询参数校验错误统一返回固定 422 提示。
4. 指数问题误绑股票并污染追问：指数名称与无市场标识六位代码混用时先澄清，不在删除指数名称后将代码解释为股票。
5. 东方财富日线畸形行阻断备用源：显式校验列表及行类型，错误统一作为来源不可用，部分主源结果不落库。

## 复盘索引 module

`knowledge.review` 对调用方集中提供 `ReviewIndexer.index`、`describe`、`search`：补索引、展示状态、获得可引用复盘。MongoDB 发布证明、manifest/hash 校验与完整脱敏原文恢复保留在 module 内部。`ReviewService` 不再重复解释租约与冷却，`InvestmentRetriever` 不再解释复盘发布字段。

重试资格由 `publication.py` 统一判断，维持180秒租约、失败冷却30秒、10分钟最多3次。沿用原有 MongoDB/Milvus adapter；保留原文先存、抽取/索引失败可读、按记录补索引、旧 worker 不发布新版本、发布回执不确定时不误删向量、全文前提和计划不等同成交。无后台自动重试或新基础设施。

新增独立内存向量 adapter 验证 module interface，不继承生产向量实现。高层召回测试只依赖 interface；版本篡改、owner/type 隔离、超时恢复、发布可见性等移至复盘测试。原 MongoDB CAS、旧 worker、回执丢失、长文本 token 预算测试继续保留。

## 验证

- 修复前定向回归：20 failed、4 passed，覆盖五个根因；修复后相关四组172 passed。
- 最终离线回归：**441 passed，14.74秒**，覆盖 investment、market、review、strategy、risk、summary、旧 RAG。日志 `.sandcastle/logs/review-refactor-regression-700c79ca-final-r2.log`。
- 真实三路投资助手：**359/359 PASS**，`full_acceptance=true`，267.91秒；真实 BGE-M3、Milvus、MongoDB、reranker、LLM、行情、API/SSE/history、故障恢复及清理。样本新增父标题条件检查。报告 `.sandcastle/logs/review-refactor-host-700c79ca-r2.json`，10项清理全通过。
- 原商品 PDF + SSE RAG：**22/22 PASS**，73.41秒，包含 MinerU、真实向量、重排、模型、引用及历史；测试向量、MongoDB、MinIO 清理通过。报告 `.sandcastle/logs/rag_accept_e6799fb31c76.json`。
- 最终策略切分改动后的真实每日摘要：**16/16 PASS**，父标题条件、真实策略导入/召回、手动生成、定时执行重试、引用、幂等与脱敏均通过；模型调用分别13.52秒、23.51秒。报告 `.sandcastle/logs/summary_accept_20a81c3d10674486.json`，MongoDB/Milvus 清理通过。
- `git diff --check` 通过。未修改前端，也未重做浏览器视觉验收。

所有真实写入只使用独立测试库 `afk_review_refactor_700c79ca` 和随机向量集合/对象桶，未触碰业务资料。三路全量验收在最后的策略短引言与纯标题预算修正前完成；这些末尾改动由最终441项回归及后续真实策略导入/摘要验收覆盖，未虚报重复执行三路全量。

## 双轴复核

Standards：明确规范违规0项，需行动的 possible smell 0项；另以1280种纯内存组合核对重试资格与基点一致。

Spec：发现的超长子节短引言丢失 P2 与纯标题恰好占满预算被拒 P3 均已修复并补正反回归，最终未解决项0。两个轴均使用独立上下文只读复核。

## 保留的失败记录与限制

- 首轮真实投资助手使用基础 conda 环境，因缺少 APScheduler 在三路召回检查后报 `ModuleNotFoundError`；切换已有 `host-check-venv` 后通过，未安装或升级依赖。首轮报告 `.sandcastle/logs/review-refactor-host-700c79ca.json` 保留，全部资源已清理。
- 首轮摘要手动生成通过，定时重试时模型返回无效 JSON，状态按现有规则保留为 failed，未发布错误摘要。报告 `.sandcastle/logs/summary_accept_e4beec41299e4209.json` 保留，全部资源已清理。独立复验通过不代表外部模型持续稳定，未扩大本轮范围更改摘要模型协议。
- 仍为单用户 `default_user`，没有登录、券商连接、自动交易或审计日志。规则脱敏不保证识别所有自然语言个人信息；风险与策略提示不构成策略有效性证明。

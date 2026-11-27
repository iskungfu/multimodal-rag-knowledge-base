# Issue #6：每日投资摘要生成

状态：完成（completed；沙箱实施与可运行验收完成；真实 BGE 全流程、完整 RAG 和浏览器待主机验收，不代表最终主机验收通过）

日期：2026-11-27（Asia/Shanghai）

任务：https://github.com/iskungfu/multimodal-rag-knowledge-base/issues/6

父规格：https://github.com/iskungfu/multimodal-rag-knowledge-base/issues/1

## 范围与依赖凭据

本轮仅实施 #6，未实施 #7。已阅读 AGENTS.md、GLOSSARY.md、docs/agents/、docs/afk-baseline-review.md、相关 ADR、父规格与任务。开始时无 issue-6 完成记录，工作树干净；保持启动器独立分支 `codex/sandcastle-2026-11-27T14-24-36-901Z`，未切换或合并 main。

开始与收尾均执行：

```bash
gh api repos/iskungfu/multimodal-rag-knowledge-base/issues/6/dependencies/blocked_by
```

**原生 blocker 为 #2，state=open；#6 正文也依赖 #2。** 没有把本地完成等同于 GitHub 解除阻塞，仅采用本轮用户明确授权的 #6→#2 分支内例外。以下祖先检查开始和收尾均退出0：

```bash
git merge-base --is-ancestor 45501584fab1bc180d2b08108f16c95d642f1b21 HEAD
git merge-base --is-ancestor 5ff71c8 HEAD
git merge-base --is-ancestor dcd09f1 HEAD
git merge-base --is-ancestor 26053fd HEAD
```

核对 `.sandcastle/results/issue-2.md` 完成及原33单元/API+4真实集成证据，读取 `docs/market-data.md` 与实际行情 adapter/repository/service；正式实施前复测 **37 passed in 6.11s**，含真实东方财富/腾讯/独立 MongoDB。核对 #4/#5 主机验收与249项回归/28真实查询/三条 RAG 路线证据，复用现有 StrategyVectors、owner过滤、RiskService、answer_policy 与引用脱敏；未倒退旧公共库兼容。

## 实现

- 查询服务 FastAPI lifespan 内嵌 APScheduler 3.x；默认周一至周五 Asia/Shanghai 15:30，提供开关与时间配置，max_instances=1、coalesce=True、显式 timezone；import 不启动后台线程。
- 独立 `index_quotes`，标识 sh000001/上证指数、sz399001/深证成指；东方财富真实日线主源、腾讯已收盘快照备用，不改变原股票 API 或平安银行000001语义。
- 两指数目标日收盘数据均核实才发布正式摘要；拒绝盘中、未来、周末、超过366天补生成；节假日/旧数据/延迟/源失败不伪造当日行情。单日快照不能冒充历史。缓存显示真实日期与警告。
- 指数规则 N=20，近20条源日收益复合涨跌幅、当日量/此前20条日线均量，明确5%/10%、1.5/2倍初始阈值及低量异常；无完整依据不得低风险，不输出市场阶段分类。规则未经回测，文档及答案明确限制。个股直接复用 #5 RiskService/evaluate。
- 持久化关注列表固定default_user，初始为空，最多20个、单文档原子限制/去重，复用已核实 SymbolResolver；名称代码及市场前缀冲突拒绝。关注不是持仓。
- `daily_summaries` 唯一(owner_id,date)，原子claim、300秒租约与token fencing，跨进程幂等、失败/中断可恢复、过期worker不能覆盖结果；240秒阶段预算、单进程一个worker。失败保留同日与历史上次成功结果。
- 手动生成/补生成/重试、读取最新/单日/历史API；页面展示关注操作、生成/失败/旧结果/重试状态，使用textContent，响应式按钮与正文。
- 真实personal向量召回调用 StrategyVectors+BGE，限定owner/type并核对Mongo已完成导入；保留来源/document_id/chunk_id。空策略、无足够片段、检索失败、模型失败分别处理。
- 实际LLM仅选择有据的原文摘录，服务端校验原文与实际chunk_id；输出完整召回片段及条件式检查清单，不允许LLM改风险或声明策略前提/持仓/成交事实。过长片段跳过，不截断条件。
- 写入前/返回前双端脱敏、免责声明去重，仅实际chunk_id保留引用；异常固定消息不回显凭据，个人正文不记原始调试日志。
- 历史补生成明确使用生成时的当前关注与策略，不伪称还原历史配置。免费源、离线调度与本机模型阻塞边界有文档。
- 提供 `scripts/verify_daily_summary.py` 默认独立存储探针、`--host`真实BGE策略图/API/调度入口、`--serve`浏览器隔离服务；旧风险/RAG验收显式关闭新增调度。完整RAG脚本改为AFK_TEST_DB_NAME内UUID历史集合清理，避免原先任意测试库/drop_database方式。

详见 `docs/daily-summary.md`。没有新增推送、付费数据源、交易、券商、审计日志或多路个性化聊天。

## 真实验证

按要求执行 `python3 -m venv /tmp/shopkeeper-venv`，所有安装/测试使用该Linux环境；无项目内虚拟环境/链接，无Windows环境复用，无大型模型/浏览器/GPU下载，未向AFK_STORAGE_PYTHON安装包。

最终命令：

```bash
DAILY_SUMMARY_ENABLED=0 AFK_RUN_MARKET_INTEGRATION=1 AFK_RUN_RISK_INTEGRATION=1 AFK_RUN_SUMMARY_INTEGRATION=1 /tmp/shopkeeper-venv/bin/python -m pytest -q -p no:cacheprovider knowledge/test/risk knowledge/test/query/test_rag_regressions.py knowledge/test/market knowledge/test/review/test_review.py knowledge/test/strategy/test_strategy.py knowledge/test/summary
```

**最终结果：300 passed in 29.72s，无skip。** 其中287单元/API/回归，13真实集成（行情4、风险/实际LLM5、新增摘要4）；新增摘要本身47单元/API+4真实集成。

- 47项新增单元/API覆盖lifespan启停/默认时区、实际query app关闭调度、盘中/周末/未来/节假日无行情/延迟/旧数据、双指数缺失/身份冲突、同批冲突写前拒绝、快照不足、去重、阈值/零量/NaN、跨workerclaim/lease/fencing、保留旧成功、失败重试、owner隔离、关注列表并发20上限与冲突、模型无依据拒绝、空/不足/失败策略、脱敏/真实引用/唯一尾句、异常隐藏。
- 真实两指数：2026-11-27收盘；东方财富30自然日窗口各23条日线，腾讯各1快照。上证指数3606.49点、3.73%、634626966手；深证成指10814.02点、7.24%、761605757手。成交量乘100存股，成交额元；双源日期/身份/点位/涨跌幅/量额核对通过，金额允许来源舍入差异。保留公开原始样本与参数。
- 独立MongoDB真实多repository竞争只获得1个claim，唯一键、过期恢复、旧token拒绝、关注去重与owner隔离通过。
- 真实指数/平安银行 + 实际应用LLM + API + MongoDB完整生成、最新/历史、引用、尾句、调度同一入口去重通过。该pytest项用自建无敏感固定策略上下文，不冒充BGE验收。
- 实跑 `scripts/verify_daily_summary.py`，12/12 PASS；真实MongoDB+独立Milvus（二维人工向量）+StrategyVectors隔离召回+真实行情+实际LLM+API+APScheduler实际executor。第二次探针额外把本次测试记录设为failed，由真实executor重试生成成功，attempts=2，未重复记录。报告：`.sandcastle/logs/summary_accept_a2e8faf9fcff4509.json`、`.sandcastle/logs/summary_accept_f02cf60b18fe4297.json`，各12项、MongoDB/Milvus清理均通过；这些本地日志被忽略。
- 既有249项回归全数包括在最终300项中，涵盖旧公共RAG兼容、策略导入、自然语言复盘、个股风险与答案保护。图/模型测试替身不代表完整真实RAG。
- `node --check` 新摘要JS与api.js通过；Python改动AST解析通过；diff/sensitive值扫描/`git diff --check`通过。未执行不存在的npm test/typecheck。

首次指数探针遇到venv依赖安装尚未结束造成 `ModuleNotFoundError: requests`，安装完成后实测成功；首次导入完整query app缺少uvicorn，按需安装后所有回归通过。早期完整回归出现既有 `entry_node.py` 的invalid escape sequence SyntaxWarning，本轮未改无关源码；最终300项无warning。未发生真实网络、应用模型或存储验收失败；未以mock结果冒充真实成功。

## 测试隔离与清理

显式集成只写入AFK_TEST_DB_NAME内本次UUID集合：market_quotes_issue2_*、risk_issue5_*、summary_issue6_*、summary_accept_*。新增存储探针只创建summary_accept_*独立Milvus集合，结束删除；本轮不写MinIO，不使用业务策略或私人交易记录。

收尾只读核对：**AFK_TEST_DB_NAME为0 collection/0 document；默认应用 `_app` 库为0 collection/0 document；summary_accept_* Milvus残留0**。未清空数据库或改写业务数据。本轮测试自动cron均关闭，受控测试仅验证lifespan和单次scheduler executor。

## 待主机验收及限制

**真实BGE策略导入→召回→摘要、三条完整公共RAG、浏览器/390px：待主机验收。** 沙箱没有本机BGE、MinerU凭据和浏览器，未运行这些流程，不能算通过。

```bash
python scripts/verify_daily_summary.py --host
python scripts/verify_daily_summary.py --host --serve --port 8002
python scripts/verify_rag.py --pdf --stream
python scripts/verify_rag.py --legacy
python scripts/verify_rag.py --knowledge --stream
```

主机脚本不下载模型，使用自建策略和独立资源；浏览器步骤、配置与退出清理见docs/daily-summary.md。完整RAG脚本本轮修改后的实际主机执行仍待验证。

没有验证长期无人值守调度、供应商真实限流上限、全部证券/更名与历史修订，也没有回测阈值。服务离线无法自动生成，支持手动补生成。无法安全强杀本机BGE底层线程：若底层永久卡住，Mongo状态会过期为failed、禁止旧结果发布，但该进程需要重启释放生成槽；正常网络调用/流程有超时和阶段预算。

仅本地提交，commit格式 `RALPH: 实现每日投资摘要与可恢复收盘调度 (#6)`。未push、创建PR、写GitHub评论、修改Issue状态/依赖或关闭Issue。#2/#6仍OPEN，由用户审阅及主机验收后决定发布。

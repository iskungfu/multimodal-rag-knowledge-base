# 每日投资摘要（#6）

查询服务内嵌收盘生成，前端 `knowledge/front/chat.html` 拉取展示。无推送、持仓接入或交易执行；个人策略/公共知识/复盘三路聊天留给 #7。

## 启动与使用

安装 `requirements.txt`（新增 `APScheduler>=3.10,<4`），保持原有查询服务启动入口。生产配置：

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `DAILY_SUMMARY_ENABLED` | `1` | `0` 关闭自动调度；手动生成仍可用 |
| `DAILY_SUMMARY_HOUR` | `15` | Asia/Shanghai 小时 |
| `DAILY_SUMMARY_MINUTE` | `30` | 分钟；配置不能早于 15:15 |
| `MONGO_DB_NAME` | `kb001` | 应用库，生产请显式配置 |
| `STRATEGY_COLLECTION` | `strategy_chunks_v1` | 复用 #4 个人策略向量集合 |

APScheduler 3.x 的 BackgroundScheduler 仅在 FastAPI lifespan 启动、关闭时停止；导入模块不启动线程。Cron 是周一至周五 15:30，显式 Asia/Shanghai，`max_instances=1`、`coalesce=True`、`misfire_grace_time=3600`。这只减少单进程重入；跨进程幂等由 MongoDB 唯一键及租约实现。

**工作日不代表交易日**。周末、未来、当天 15:15 前直接拒绝；工作日节假日、延迟或来源失败时，必须取得双指数目标日收盘日线/快照，才可生成正式摘要。若无法核实，状态为失败，提示稍后重试。不会将前一交易日数据改成当天，也不以“周一到周五”伪造交易日历。

服务离线期间无法保证定时生成；不使用持久化调度队列，不自动补全多年历史。重新上线后可手动指定最近 366 个自然日内的收盘日补生成。使用**生成时的当前关注列表和当前个人策略**，并非还原历史当日配置；历史个股 PE 可能缺失，风险保持有限依据说明。

```bash
curl -X POST http://localhost:8001/watchlist -H 'Content-Type: application/json' -d '{"query":"平安银行000001"}'
curl http://localhost:8001/watchlist
curl -X DELETE http://localhost:8001/watchlist/000001
curl -X POST http://localhost:8001/daily-summaries/generate -H 'Content-Type: application/json' -d '{"date":"2026-11-27"}'
# 对失败直接再次 generate；对已成功记录显式 retry=true 才重新生成
curl -X POST http://localhost:8001/daily-summaries/generate -H 'Content-Type: application/json' -d '{"date":"2026-11-27","retry":true}'
curl http://localhost:8001/daily-summaries/latest
curl 'http://localhost:8001/daily-summaries?limit=30'
curl http://localhost:8001/daily-summaries/2026-11-27
```

`POST generate` 返回 202、`started` 和持久化 `record`，后台生成，页面每 2.5 秒读取处理中的状态。已成功/处理中重复请求不重复开始。参数错误 422、其他日期占用本进程 409、存储不可用 503，详情不回显凭据或个人原文。`GET latest` 同时返回 `today/current/latest_success`，失败不能遮蔽旧成功结果。历史默认 30 条、最多 100 条，按收盘日倒序；单日不存在 404。

`watchlists` 每个用户一个文档（`_id=default_user`），单文档原子更新限制最多 20 个并按代码去重。初始为空，复用 #5 SymbolResolver 核实 AStock、市场、名称与代码，冲突要求确认。API 不接受 owner 或类型选择。关注不代表实际持仓；删除后已有摘要保留生成时快照。

## 正式数据与指数规则

`index_quotes` 使用 `(symbol,date)` 唯一索引，`sh000001=上证指数`、`sz399001=深证成指`；不能放入 `market_quotes` 的 `code=000001`（平安银行），股票 API 继续只接受原有六位 A 股代码。

东方财富主源使用独立 secid、验证响应 market/code/name，UTF-8 历史日线；腾讯 GBK 快照备用验证市场前缀、名称、代码与收盘时间。保留原始行情样本、抓取时间与来源；已有目标日真实收盘缓存可用于离线补生成，会显示缓存警告。

| 字段 | 存储单位 | 东方财富日线 | 腾讯快照 |
| --- | --- | --- | --- |
| `close` | 点 | f53，已为十进制 | 位置3，点 |
| `change_pct` | 百分数 | f59，无缩放 | 位置32，无缩放 |
| `volume` | 股 | f56 手 ×100 | 位置6 手 ×100 |
| `amount` | 元 | f57，无缩放 | 位置57 万元 ×10000 |

真实双源交叉验证日期、名称、点位、日涨跌幅、成交量与成交额，样本在 `knowledge/test/summary/fixtures/`。这是指数适配，未将旧原型的固定 /100、/10 公式复制到日线。

指数 N=20，需至少 21 条不同日期日线。近 N 日涨跌幅是最近 20 条源日涨跌幅的复合收益；量比是当日成交量/此前20条日线均量，不含当日。近 N 日收益绝对值 5%/10%、量比1.5/2倍分别为中/高；量比≤0.5为中、零量比为高，各指标取最高档。快照仅算一日；重复记录不凑样本，同日冲突拒绝；历史不足/零基准不能判低。代码常量为规则说明单一来源。每个指数单独显示当前位置风险，不输出牛/熊/震荡判断。这些是**未经回测的初始阈值**，没有行业校准或预测结论。

关注个股直接复用 #5 RiskService/evaluate，补生成时评估锚定目标收盘日。不能获取目标日个股行情时明确显示旧数据或无法评级，不让 LLM 覆盖规则。摘要含指数实际数据、来源、警告、个股数据日期与风险说明。

## 个人策略与保护

查询 `strategy_documents` 中本用户已完成导入的 personal 文档，通过本机 BGE-M3 编码问题，调用现有 StrategyVectors 混合检索（owner+personal），再核对命中归属、类型和 MongoDB 已完成文档。保留 `source/owner_id/document_id/chunk_id/title/content`；其他用户和公共文档不能进入。没有已完成个人策略返回 empty；有文档无合格片段为 insufficient；检索异常生成失败。超过 3000 字的片段跳过并计为不足，不通过截断删除条件。

实际应用 LLM 只选择逐字有据的原文引用，输出 JSON 经 schema、chunk_id 与连续原文校验。最终展示完整召回片段，使用服务端固定的“若采用策略，先人工核对所有前提”的检查清单；市场阶段、持仓、成交情况不被推定。模型失败/无依据输出记录失败，保留上次成功结果。策略为空/不足仍可生成明确缺少个性化提示的行情摘要，不调用模型编造策略。

写入前、返回前均使用现有脱敏与 `answer_policy.finalize_answer`，尾部免责声明只追加一次，仅实际引用的 chunk_id 获保护。document_id 保存在结构化引用字段中，页面以安全 textContent 显示。个人策略正文与模型原始响应不写调试日志；异常仅提供固定原因。前端全部按文本渲染摘要与引用，显示未配置、处理中、失败、旧摘要、重试与上次成功状态。

## 幂等与恢复边界

`daily_summaries` 唯一 `(owner_id,date)`；状态 pending → processing → completed/failed，记录 attempts、created_at、updated_at、generated_at、result、error。result 包含实际日期、两指数/关注股票证据、策略引用、警告及答案。

原子 claim、300秒租约、随机 token fencing 防止多个进程发布同日结果。GET/POST/下次任务会将过期 processing 改为 failed，随后可重试；旧 worker 的 token/过期租约不能覆盖新结果。失败不覆盖既存 result 和 generated_at，也不覆盖其他日期成功记录。数据库暂不可用时不能保证立刻写入失败状态，恢复后通过租约回收。

同进程最多一个实际生成 worker，避免连续手动点击堆积后台线程；外部 HTTP/MongoDB/LLM 均设置超时，流程在每个阶段检查 240秒预算。进程崩溃的任务在最多300秒后可重新 claim；本机 BGE 等底层调用无法安全强杀线程，若它永久卡住，记录仍会过期为 failed，但该进程需要重启才能释放生成槽。没有实现通用任务平台、自动无限重试或后台推送。

## 验证与主机验收

沙箱 Linux venv 位于 `/tmp/shopkeeper-venv`。测试须 `DAILY_SUMMARY_ENABLED=0`；显式 MongoDB 验证限定 `AFK_TEST_DB_NAME`，每次 UUID 集合，结束清理；默认应用 `_app` 测试库也须核对无残留。

```bash
DAILY_SUMMARY_ENABLED=0 AFK_RUN_SUMMARY_INTEGRATION=1 /tmp/shopkeeper-venv/bin/python -m pytest -q -p no:cacheprovider knowledge/test/summary
/tmp/shopkeeper-venv/bin/python scripts/verify_daily_summary.py
```

默认探针执行真实 Milvus（二维人工向量、不是 BGE）、MongoDB、指数/个股、应用 LLM、实际 APScheduler executor 与 API/去重/引用，并清理本次资源；不会访问业务策略。报告只记录检查结果，不记录策略原文或密钥，位于被忽略的 `.sandcastle/logs/summary_accept_*.json`。

沙箱无本机 BGE/MinerU/浏览器，不下载大型模型。主机使用已有环境：

```bash
python scripts/verify_daily_summary.py --host
python scripts/verify_daily_summary.py --host --serve --port 8002
# 既有完整公共 RAG 三条路线回归（均关闭摘要自动调度，显式测试库/独立资源）
python scripts/verify_rag.py --pdf --stream
python scripts/verify_rag.py --legacy
python scripts/verify_rag.py --knowledge --stream
```

`--host` 将自建文字策略走真实导入图/BGE/Milvus，并额外导入其他 owner 文档，核验召回、API、实际应用 LLM、失败状态重试和实际 scheduler executor；默认 cron 关闭，仅触发一次独立验收任务。`--serve` 保留独立测试摘要/关注/行情/会话/复盘集合供浏览器，正常退出清理；策略 API 同样映射为本次隔离资源。不要用私人文档验收。验收服务页面“文档导入”导航仍指向原8000服务，验收导入应调用本次8002服务的 `/strategies/text` 或 `/strategies/upload`。

浏览器检查：

1. 打开 `http://127.0.0.1:8002/chat.html`，查看实际收盘日期、双指数、平安银行风险、个人策略、来源ID与唯一免责声明。
2. 添加/重复添加/删除股票；输入“贵州茅台000001”“sh000001”应确认冲突；不预置用户持仓。
3. 查看历史，手动补生成实际交易日；盘中/未来/周末拒绝。指定无行情的节假日呈失败，仍可拉取旧成功摘要。
4. 重新生成时看到 processing；刷新恢复持久记录；源/模型故障显示失败，可恢复后重试，上次成功仍保留。
5. 用自建文字策略导入后重生成，验证引用和条件前提；既有聊天的 SSE/非流式、复盘与策略功能保持可用。
6. 390px 宽度确认输入/按钮换行、正文可滚动，无横向溢出；服务停止后确认本次 MongoDB/Milvus 资源已删除。

本轮沙箱结果见 `.sandcastle/results/issue-6.md`；**真实 BGE 全流程、三条完整 RAG 与浏览器：待主机验收**。长期跨交易日调度、全部证券覆盖和免费来源可靠性不作为已验证能力。

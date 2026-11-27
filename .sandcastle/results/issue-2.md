# Issue #2：行情适配层与市场行情数据源

状态：完成（completed）

日期：2026-11-27（Asia/Shanghai）

任务：https://github.com/iskungfu/multimodal-rag-knowledge-base/issues/2

父规格：https://github.com/iskungfu/multimodal-rag-knowledge-base/issues/1

## 选择与范围

已阅读 AGENTS.md、GLOSSARY.md、docs/agents/、基线审阅、父规格及相关 ADR，采用更新后的本地规范。选择前执行 `gh api repos/iskungfu/multimodal-rag-knowledge-base/issues/2/dependencies/blocked_by`，结果为 `[]`；正文无 blocker，当前分支没有已有完成记录。现有代码只有行情探索原型，实际实现不依赖其他候选。本轮仅处理 #2。

## 实现

- 新增免费源 adapter：东方财富不复权日线为主，另行解析快照补充同日期、同收盘价的估值；腾讯 GBK 收盘快照作备用。支持沪深 A 股六位代码和日期区间。
- 统一单位为价格/金额/市值“元”、成交量“股”、涨跌幅/换手率“百分数”。缺失字段为 `null`，保留原始来源样本，不将历史估值编造为当日估值。
- 接入 `market_quotes`，建立 `(code, date)` 唯一索引，使用 upsert；同日重抓不新增重复记录，空缺估值不清空已有值，历史重抓保留已有快照证据。
- 在查询服务注册 `POST /market/quotes/{code}/refresh` 与 `GET /market/quotes/{code}`。数据日期与抓取时间分别保存；拒绝盘中/未来日期，不将旧数据标为当天。API 返回来源、缓存状态和缺失/延迟提示。
- 每个源请求间隔至少 1 秒，最多尝试两次，连续请求失败冷却 60 秒；同进程同区间成功结果缓存 300 秒。源或存储失败返回明确的 HTTP 503。
- 接口与单位文档：`docs/market-data.md`；原始 HTTP 样本及采集参数：`knowledge/test/market/fixtures/`。

## 对旧 Issue/原型断言的更正

真实样本证明东方财富日线已是十进制元/百分数，不能再次统一缩放。快照 `fltt=1` 的价格按 `f59` 精度转换，涨跌幅为 `f170 / 100`；`f169` 是涨跌额，动态 PE 应取 `f162 / 100`。金额存元，换手率存百分数。东方财富响应为 UTF-8，腾讯快照为 GBK。

贵州茅台样本收盘价 1472.36 元、昨收 1469 元、涨跌额 3.36 元，计算涨跌幅为 0.23%，与两源一致。成交量 39808 手映射为 3980800 股，成交额为 5849112431 元；两源动态 PE 都为 21.41。`f86` 为快照时间；样本中 `f124=0`，不拿它推断交易日。没有沿用“每天一次必不受限”的断言。

## 实际验证结果

Linux Python 3.12.3。首次在 Windows 挂载目录执行 `python3 -m venv .venv`/安装依赖遇到 `Operation not permitted`；随后在 Linux 临时目录执行同一 venv 命令，工作树 `.venv` 链接到该 Linux 环境，安装测试所需的 pytest、requests、pymongo、FastAPI、httpx、python-dotenv。未使用 Windows 虚拟环境，未修改 `AFK_STORAGE_PYTHON` 环境。`.gitignore` 同时忽略 `.venv` 目录和链接。

最终命令：

```bash
AFK_RUN_MARKET_INTEGRATION=1 .venv/bin/python -m pytest -q -p no:cacheprovider knowledge/test/market
```

实际输出：**37 passed, 1 warning in 6.37s**，没有 skip。其中 33 项为单元/API 行为验证，4 项为显式启用的真实集成验证。

- 原始样本映射、UTF-8/GBK、快照精度、缺失字段、异常响应、历史估值隔离、盘中/未来/越界日期、备用源、缓存、错误状态和连接信息隐藏通过。
- 超时、断连、HTTP 429 的有界重试、连续调用冷却与模拟次日恢复通过；此项为受控故障测试，不是故意向真实源施加限流负载。
- 真实 MongoDB 唯一索引、重复 upsert、重复键拒绝、缺失估值及原始证据保留通过。
- 东方财富、腾讯分别通过真实网络 → 实际行情 API router → MongoDB → API 读取的完整流程，重复请求命中缓存且未产生重复记录。
- 实测深市 `000001` 为平安银行，并交叉核对两源日期、收盘价、成交量、涨跌幅、换手率、动态 PE、PB。
- MongoDB 写入仅使用 `AFK_TEST_DB_NAME` 指定独立库内的 `market_quotes_issue2_<uuid>` 集合；每次验证后删除本次集合，没有改写或清空业务库。未连接 Milvus/MinIO，未调用模型。
- 7 个生产 Python 文件通过语法解析。最终 diff、敏感信息检查与 `git diff --check` 通过。

唯一测试 warning 为新版 Starlette 对 httpx TestClient 的弃用提示，未影响本次验证。首次单元测试还出现挂载目录的 pytest cache 权限提示，最终使用 `-p no:cacheprovider` 后消除。

## 未验证项与限制

- 未验证全部 A 股、长期跨交易日运行、供应商真实限流阈值或所有行情修订场景；免费源后续可用性无法保证。
- 历史日线不提供历史 PE/PB/市值；仅同日期有效快照可补充。腾讯备用只返回最新已收盘日，历史区间可能不完整，响应明确提示。
- 尚未安装/运行完整 RAG 模型和查询图；本次验证的是实际行情 router、服务、外部源与 MongoDB，非原有问答全链路。
- 本期单次抓取能力已完成；收盘定时调度/每日摘要留给 #6，个股风险与前端展示留给后续任务。

保持启动器创建的独立分支；仅本地提交。不 push、不创建 PR、不评论或关闭 Issue，由用户审阅后决定发布。

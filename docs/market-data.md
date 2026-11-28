# 收盘行情接口与数据约定

Issue #2，遵循 ADR-0002、ADR-0006 与 `docs/afk-baseline-review.md`。行情路由挂载于现有查询服务（8001），不调用 LLM。

## 使用方式

```bash
# 抓取最近 30 个自然日的已收盘日线，并写入 market_quotes
curl -X POST 'http://localhost:8001/market/quotes/600519/refresh'

# 指定历史区间；GET 只读取已存数据，不请求外部行情源
curl -X POST 'http://localhost:8001/market/quotes/000001/refresh?start=2026-11-25&end=2026-11-27'
curl 'http://localhost:8001/market/quotes/000001?start=2026-11-25&end=2026-11-27'
```

参数是沪深 A 股六位代码（`000001` 为深市平安银行，不能当作上证指数）。暂不支持指数、基金、北交所。区间最长 366 天；`end` 默认取上海时间 15:15 后的当天，否则取前一天。拒绝未来或尚未收盘日期。节假日、停牌和来源延迟不靠本地交易日历推断；返回真实数据日期，旧数据不改写成当天。

`POST` 返回 `code/start/end/source/cached/warnings/quotes`。`source` 表示本次抓取源，每条 `quote` 也保留自身来源。`GET` 返回 `code/start/end/quotes`。没有已存数据时 `GET` 返回空数组。输入无效为 HTTP 422；所有来源均失败或区间内没有可用数据时为 HTTP 503，含 `Retry-After: 60`；存储失败也返回 HTTP 503。数据库配置复用 `MONGO_URL` 与 `MONGO_DB_NAME`，默认集合 `market_quotes`。

## 单位与来源

存储使用基础单位，展示端如需“亿元”应自行换算；百分数 `0.23` 表示 `0.23%`。

| 字段 | 存储单位/含义 | 东方财富 | 腾讯备用快照（从 0 开始的字段位置） |
| --- | --- | --- | --- |
| `date` | 来源交易日期，ISO 字符串 | 日线 f51 | 30 的日期部分 |
| `open/close/high/low` | 元，不复权 | 日线 f52/f53/f54/f55 已是十进制元 | 5/3/33/34，元 |
| `pre_close` | 元，涨跌幅使用的昨收基准 | 日线 close 减 f60 涨跌额 | 4，元 |
| `change_pct` | 百分数 | 日线 f59，无缩放；快照 f170 / 100 | 32，无缩放 |
| `volume` | 股 | 日线 f56 为手，乘 100 | 6 为手，乘 100 |
| `amount` | 元 | 日线 f57，无缩放 | 57 为万元，乘 10000 |
| `turnover` | 百分数 | 日线 f61，无缩放；快照 f168 / 100 | 38，无缩放 |
| `pe` | 动态 PE，倍数 | 快照 f162 / 100 | 52；不是位置 39 的 TTM PE |
| `pb` | PB，倍数 | 快照 f167 / 100 | 46，无缩放 |
| `market_cap` | 总市值，元 | 快照 f116，无缩放 | 45 为亿元，乘 1e8，精度较低 |

东方财富快照请求固定 `fltt=1`，价格按 `10 ** f59` 精度转换；此处快照的 f59 是价格精度，与日线 fields2 中的 f59（涨跌幅）不同。`f169` 是涨跌额，不能用于 PE。日线请求 `klt=101,fqt=0`，不再将十进制价格除以 100。

东方财富两个接口均按实际 UTF-8 JSON 解码，腾讯 `qt.gtimg.cn` 快照按 GBK 解码。源字段 `-`、空字符串和缺失可选值映射为 `null`；不填零、不猜历史估值。

东方财富历史日线为主源。仅当快照日期与日线日期一致、收盘价一致、`f86` 时间在收盘后且不晚于抓取时间时，才补充该日估值。历史日线本身没有历史 PE/PB/市值，无法补充时保留为空并提示。`f124` 在本次实测为 0，不能用作交易日期。腾讯备用仅覆盖最新收盘快照：主源失败时返回真实可用日期并明确提示历史区间可能不完整；不生成虚假的历史记录。

## 存储与失败行为

- `(code, date)` 唯一索引与 upsert 保证重复抓取不新增重复记录；处理并发首次插入竞争。
- 写入前校验整批行情的代码、日期及同日重复记录；同日行情指标冲突时拒绝整批主源数据并尝试备用源，完全相同的重复记录合并。无有效备用数据则返回 503，不将冲突记录覆盖成单条可信行情，也不缓存该次失败。
- 主源日线结构或行类型错误按来源不可用处理，正常进入备用源流程，已解析的部分主源数据不落库。
- `updated_at` 为抓取时间，`source_time` 为可核实的快照时间，`valuation_source` 为估值来源。MongoDB 时间按 UTC 存储，交易日期按 Asia/Shanghai 解析。
- `raw` 保留该日原始日线字段与可用快照，API 不返回 `raw`。重抓缺少可选字段时不清空已有值；只更新提供的原始证据，保留旧快照。
- 每个源的 HTTP 请求串行限速，间隔至少 1 秒；连接超时 3 秒、读取超时 8 秒，最多尝试两次。请求连续失败后冷却 60 秒，再允许恢复；异常不包含连接凭证。
- 同进程同代码同日期区间成功抓取缓存 300 秒，并串行处理刷新，减少重复请求。不同进程仍各有限速器；本期不实现分布式配额控制。
- 免费源不承诺任何频率下必定可用。断连不能直接认定为限流；已测试超时、断连和 HTTP 429 的有界处理及次日模拟恢复。

本期提供单次完整抓取入口；定时调度与摘要属于后续 Issue #6，前端展示与风险评估不在本次范围。

## 验证方式

仅需行情测试依赖，不必下载 RAG 模型：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pytest requests pymongo fastapi httpx python-dotenv
.venv/bin/python -m pytest -q -p no:cacheprovider knowledge/test/market/test_market.py

# 已注入 MONGO_URL、MONGO_DB_NAME、AFK_TEST_DB_NAME 后显式启用真实服务测试
AFK_RUN_MARKET_INTEGRATION=1 .venv/bin/python -m pytest -q -p no:cacheprovider knowledge/test/market
```

集成测试只使用 `AFK_TEST_DB_NAME` 内本次创建的 `market_quotes_issue2_<uuid>` 集合，验证后删除该集合。未设置独立测试库会失败，不回退到业务库。默认测试跳过真实集成项，不将 skip 记为通过。实测记录见 `.sandcastle/results/issue-2.md`。

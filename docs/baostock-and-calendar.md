# BaoStock历史备用与交易日历

## 安装和来源顺序

按 `requirements.txt` 安装依赖，新增固定版本 `baostock==0.9.4`。使用官方SDK默认匿名公共连接，不需要配置用户API key。

单股 `/market/quotes/{code}/refresh`：东方财富 → BaoStock → 腾讯历史日线 → 腾讯快照。全市场仍首先取腾讯批量快照；缺失证券的补采为东方财富 → BaoStock → 腾讯历史。成功时响应/每条记录的 `source=baostock`，源行情和身份保存在raw，API不返回raw。

已实测600519、000001、688981、302132，北交所920002不受此来源支持，适配器在联网前跳过北交所。来源列表不是完整支持证券名录，不保证所有沪深股票均有历史。

## 字段口径

| BaoStock字段 | 本项目字段 | 口径 |
| --- | --- | --- |
| open/high/low/close/preclose | 对应OHLC与pre_close | 元、不复权；adjustflag必须为3 |
| volume | volume | 股，不能再乘100 |
| amount | amount | 元 |
| pctChg | change_pct | 来源百分数，不从相邻收盘价推算 |
| turn | turnover | 百分数，保留来源精度 |
| peTTM | 仅raw | TTM PE不是动态PE，不能替代pe参与既有评级 |
| pbMRQ | pb | 来源PB；估值来源标记baostock |
| tradestatus | 仅raw | 0停牌、1交易；停牌与非零成交冲突时拒绝 |

最新日匹配腾讯收盘快照时可补动态PE/PB/市值和估值时间。身份、日期、OHLC须一致；来源按整手或股、元或小数元显示时允许最多100股和1元的精度差，不修改BaoStock自身成交量和涨跌幅。只补非空估值，腾讯PB缺失时保留BaoStock的PB；混合估值标记 `valuation_source=baostock+tencent`，raw中逐字段记录来源。快照估值全部缺失时保持原来源与时间。历史日期不借用当前估值。

## 日历

```bash
curl 'http://localhost:8001/market/calendar?date=2026-11-27'
```

返回 `date/is_trading_day/source/checked_at/cached/warning`。`true`表示来源标记为交易日，`false`表示已核实休市，`null`表示未知；未知不是休市。日历查询仅限当前日期前后366天；自然年全量校验并缓存24小时，源返回不完整、日期重复或状态错误时不缓存成功结果，并暂停日历请求60秒；此校验冷却不阻止历史日线请求。来源不可用仍返回200与未知状态，输入超范围422，其他服务故障503。

日历仅控制自动任务是否跳过，不改变手动补采、不保证个股有成交、不替代真实行情日期校验。自动任务保持上海时间工作日15:30摘要和16:00采集；节假日已核实休市时不调用生成/采集服务，日历未知或故障时继续原流程。

## 有界运行

SDK在独立子进程使用自己的全局socket，父服务不修改全局socket默认超时。每次请求socket超时5秒，进程总时限20秒，失败冷却60秒，串行请求间隔至少1秒，SDK入口和日历缓存各自排队最多2秒；日历排队超时返回未知，不阻塞至前一请求完成。Windows后台子进程不弹出窗口。源错误只输出固定消息，不回显SDK异常、路径或连接信息。

验证入口：`python scripts/verify_baostock.py`。必须提前指定独立 `AFK_TEST_DB_NAME`；只创建并删除本轮随机测试集合，不操作业务库。此脚本会真实访问公开行情、日历和本地API/MongoDB，部分故障由验收主动注入以验证切换。具体证据和限制见本轮交付报告。

## 一手资料与实测

- [官方PyPI发布页](https://pypi.org/project/baostock/0.9.4/)，安装包的 `baostock/security/history.py`、`metadata/stock_metadata.py`、`util/socketutil.py` 和 `login/loginout.py` 为本轮直接核对的SDK源码。
- [BaoStock官方历史日线文档入口](https://www.baostock.com/mainContent?file=history_A_stock_k_data.md)。网页抓取未返回可读正文，本轮字段行为以官方SDK调用参数及真实响应交叉验证为准。
- 本地只读探针 `.sandcastle/logs/probe_baostock.json`：匿名连接成功；四只沪深样本各22条历史、920002无有效证券，2026-10-01至11-30日历61条。

未运行Pytdx，未验证全市场长期覆盖或金融规则有效性；仍可返回partial/failed。

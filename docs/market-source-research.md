# 免费行情接口调研与接入结论

核对日期：2026-11-28（Asia/Shanghai）。本轮用户要求参考 GitHub 或其他金融平台项目使用的接口，并推进现有平台。以下区分项目声明、直接核对的源代码和本机实测；依赖多个 Python 库不等于拥有多个独立行情源。

## 项目实际使用什么

| 项目 | 本次核对的接口/来源 | 对当前项目的意义 |
| --- | --- | --- |
| TradingAgents-CN | README 列出 Tushare、AKShare、BaoStock | 可以借鉴可替换的数据源管理；本轮未安装整个平台或验证每个适配器 |
| daily_stock_analysis | `data_provider/__init__.py` 配置 Efinance、AKShare、Pytdx、Tushare、BaoStock、Yfinance、Tencent 等适配器；Tushare 优先级取决于 token | 免费源优先、故障切换；腾讯日线是其兜底路径，但本项目不直接照搬其复权、金额或涨跌幅处理 |
| AKShare | `stock_zh_a_hist_tx` 通过腾讯 `proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get` 取日线；其他接口分别封装东方财富等来源 | 库是封装层。可参考接口字段与单位，不能把 AKShare + 东方财富当成必然独立的两家来源 |
| efinance | `stock/getter.py` 多处调用 `eastmoney.com` 的行情、证券和财务接口 | 单纯增加 efinance 不能保证绕过东方财富的故障；财务数据扩展可另行评估 |

Tushare 的官方文档按接口列出 token、积分与访问权限；不能将项目支持 Tushare 等同于全部接口无需配置且免费可用。第一轮将BaoStock和Pytdx列为后续候选，未运行它们的SDK；后续已完成BaoStock专项，见文末。Pytdx仍未运行。

## 第一轮选择：腾讯历史日线

保持既有 requests + adapter 架构，增加一个不要求 API key 的腾讯不复权历史日线 adapter，无新增生产依赖。

- 单股刷新：东方财富日线 → 腾讯历史日线 → 腾讯最新收盘快照。
- 全市场补采：腾讯批量快照缺失 → 东方财富目标日日线 → 腾讯目标日日线。
- 目录仍使用东方财富/新浪；本轮不替换目录，不承诺补齐所有曾经缺失的证券。
- 腾讯历史与腾讯快照属于同一供应商；它们是不同接口能力，不能算作两家独立供应商。
- 保留超时、两次尝试、限速、冷却和全市场总预算；不使用轮换代理、无限重试或绕过鉴权。

## 本机实测发现

样本为 600519、000001、688981、920002、302132，覆盖沪深京以及科创板、302代码。腾讯接口会返回请求区间之外的数据，必须本地按真实交易日期过滤；只接受 `day`，不把 `qfqday/hfqday` 当作不复权数据。单次640条达到上限且不能覆盖起始日期时拒绝作为有效历史范围。

腾讯日线字段0–5为日期、开、收、高、低、量；字段6可能是对象，不能当成交额；字段7为百分数口径换手率，字段8成交额为万元。成交量在688/689为股，其他本项目支持的股票为手。换手率 `0.32` 在本项目仍表示 `0.32%`，不照搬其他库对百分比小数的转换。

真实688981样本：腾讯量为96,836,491股；旧解析统一乘100会放大100倍。东方财富同日以整手报告968,365手，即96,836,500股，二者差9股。跨源校验对科创板允许最多1手的来源精度差，价格仍须一致，不把不同精度的数值强行改成相等。新快照与历史日线共用正确单位规则。

腾讯历史行没有可直接核实的历史动态PE和来源日涨跌幅。它们保留为空；只有同日期、同OHLC和同成交量的真实收盘快照才能补充当日字段。不同日期估值不回填历史，不使用相邻不复权收盘价推算跨除权收益，风险模块继续明确标记信息不足。

旧科创板腾讯记录读取时按原始快照核对身份、日期、价格和旧值：可核实时在返回结果中恢复为股；无法核实时返回缺失成交量，使风险评估降级。数据库原始记录不批量改写；单股刷新可重新持久保存正确值。新旧记录不再仅因单位不同产生虚假量比。

## 来源快照

本次只参考接口契约和行为，未复制其他平台业务实现。以下固定版本用于复核，后续升级以实际响应为准。

- [TradingAgents-CN README](https://github.com/hsliuping/TradingAgents-CN/blob/294a3b56f595cccaf610248e3bddf8f9ad5a8feb/README.md)
- [daily_stock_analysis 数据源清单](https://github.com/ZhuLinsen/daily_stock_analysis/blob/0e505b0b11943e4caf24c7ea73d3ea7337c9f30f/data_provider/__init__.py)
- [daily_stock_analysis 腾讯适配](https://github.com/ZhuLinsen/daily_stock_analysis/blob/0e505b0b11943e4caf24c7ea73d3ea7337c9f30f/data_provider/tencent_fetcher.py)
- [AKShare 腾讯历史日线](https://github.com/akfamily/akshare/blob/2864afc111ae6ffdbfd64673f4ba649af5a96926/akshare/stock/quote/stock_hist_tx.py)
- [AKShare 腾讯成交量转换](https://github.com/akfamily/akshare/blob/2864afc111ae6ffdbfd64673f4ba649af5a96926/akshare/index/index_stock_zh.py)
- [efinance 股票接口](https://github.com/micro-sheep/efinance/blob/c8fd370a3109b2d14a121e3a32a86e9c8354b01b/efinance/stock/getter.py)
- [Tushare 日线接口官方说明](https://tushare.pro/document/2?doc_id=27)

真实验收使用 `python scripts/verify_market_fallback.py --with-storage`；需配置独立 `AFK_TEST_DB_NAME`，不回退到业务库。报告保留每只证券的真实日期、样本数、单位比较、来源不可用情况以及本轮随机测试集合的清理结果。未加 `--with-storage` 时只访问公开行情，跳过数据库部分。详见 `.sandcastle/results/free-market-sources-2026-11-28.md`。

## 后续接入：BaoStock与交易日历

2026-11-28在本机实测官方 `baostock==0.9.4`，匿名公共连接取得600519、000001、688981、302132各22条历史及交易日历；920002没有有效证券数据，适配器明确跳过北交所。直接核对SDK源码与真实响应，不把其他平台“支持BaoStock”的声明当作本项目验收。

当前单股顺序为东方财富 → BaoStock → 腾讯历史 → 腾讯快照；全市场仍以腾讯批量快照为首选，缺失补采为东方财富 → BaoStock → 腾讯历史。BaoStock历史涨跌幅直接用于已有波动率计算，不从不复权价差猜测；`peTTM`不能替代动态PE。SDK以有界子进程隔离全局socket，日历仅在明确休市时跳过自动任务。

来源契约、官方发布页、SDK文件与实测边界见 `docs/baostock-and-calendar.md`，决策见ADR-0010，验收见 `.sandcastle/results/baostock-calendar-2026-11-28.md`。独立接入不代表已核实供应商内部上游，也不保证沪深京全量覆盖或长期可用。

# 原始行情样本

2026-11-27 收盘后从免费公开接口实际取得，股票为沪市贵州茅台 `600519`。文件保留响应 body 原始字节；不存在认证信息。后续测试中修改字段的情形均为显式构造的异常/边界样本。

- `eastmoney_daily.json`：`https://push2his.eastmoney.com/api/qt/stock/kline/get`，参数 `secid=1.600519,klt=101,fqt=0,beg=20261125,end=20261127,fields1=f1,f2,f3,f4,f5,f6,fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61`；HTTP 200，`application/json; charset=UTF-8`。
- `eastmoney_snapshot.json`：`https://push2.eastmoney.com/api/qt/stock/get`，参数 `secid=1.600519,fltt=1,fields=f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f86,f116,f124,f162,f167,f168,f169,f170`；HTTP 200，`application/json; charset=UTF-8`。`f86=1795767114` 对应上海时间 2026-11-27 16:11:54，`f124=0`。
- `tencent_snapshot.gbk`：`https://qt.gtimg.cn/q=sh600519`；HTTP 200，`text/html; charset=GBK`。源时间为 2026-11-27 16:14:54。文件是 GBK 字节，勿转码后再测试。

三份样本一致：收盘价 1472.36 元、昨收 1469 元、涨跌额 3.36 元、涨跌幅 0.23%、成交量 39808 手、成交额 5849112431 元、换手率 0.32%。`3.36 / 1469 * 100` 四舍五入为 0.23%，可独立排除旧原型的 `/10` 公式。东方财富 `f162=2141` 与腾讯动态 PE 21.41 一致；腾讯 TTM PE 为 22.40，不能混用。历史 PE 没有出现在日线响应中。

本次另外实测深市 `000001`（平安银行）并交叉核对两源主要字段，详见集成测试与任务结果。少量样本不能证明全市场覆盖或未来稳定性。

# 证券身份来源样本

2026-11-27 从公开 `https://searchapi.eastmoney.com/api/suggest/get` 实测采集原始 UTF-8 JSON，HTTP 200，无认证信息。

- `symbols_000001.json`：`input=000001,type=14,count=20`。同码包含平安银行（`AStock, MktNum=0, QuoteID=0.000001`）、上证指数（`Index, MktNum=1`）、基金等；不能仅以六位代码命中所有类型。
- `symbols_600519.json`：`input=贵州茅台,type=14,count=20`，`AStock, MktNum=1, QuoteID=1.600519`。

这两份样本只用于验证解析约定，不是静态全市场代码表。运行时仍请求来源核验，失败时不猜身份。

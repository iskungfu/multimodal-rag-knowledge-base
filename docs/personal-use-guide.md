# 个人使用指南

适用于已确认的A股个人使用版。30条需求与追加功能的实现状态见 `docs/requirements-acceptance.md`，最终验收见 `.sandcastle/results/final-personal-delivery-2026-11-28.md`。

## 本机启动

先启动VMware中的CentOS及既有MongoDB、Milvus、MinIO服务，保证配置的模型接口和PDF解析服务可用。保留已有 `knowledge/.env`；本项目的模型路径、连接信息在该文件配置，勿用示例覆盖。

当前这台Windows电脑可复用已验收的Python环境，不需要重新下载BGE模型。在两个PowerShell终端中分别运行：

```powershell
# 终端1：导入服务
Set-Location D:\Project\shopkeeper-brain
& '.sandcastle/logs/host-check-venv/Scripts/python.exe' -m uvicorn knowledge.api.import_api:create_app --factory --host 127.0.0.1 --port 8000 --env-file knowledge/.env
```

```powershell
# 终端2：查询、摘要和行情采集
Set-Location D:\Project\shopkeeper-brain
& '.sandcastle/logs/host-check-venv/Scripts/python.exe' -m uvicorn knowledge.api.query_api:create_app --factory --host 127.0.0.1 --port 8001 --env-file knowledge/.env
```

该Python路径是本机已有环境，未随Git提交。其他机器按照README安装依赖并激活环境，以 `python` 代替该路径。始终在项目根目录执行命令，勿切到 `knowledge`；保持两个终端运行。按Ctrl+C可停止对应服务。

也可用 `scripts/service.py start query` / `start import` 在后台启动，使用 `status` 查询、`stop` 正常停止；沿用上述 Python 环境执行，需安装 requirements.txt 中的 psutil。命令与日志位置见 [开发与验收入口](agents/development.md#本机服务管理)。工具只管理自己启动的实例；已有前台服务继续使用 Ctrl+C 停止，端口空闲后再切换。

浏览器访问：

- 导入页：`http://127.0.0.1:8000/front/import.html`
- 投资助手：`http://127.0.0.1:8001/front/chat.html`

模型首次加载可能较慢。数据库或模型来源失败时先查看页面的明确错误，不将测试报告里的历史成功当作当前连接可用。

## 第一次使用顺序

1. 在导入页选择个人策略，上传自己的PDF、Markdown或心得文字，等待导入完成；公共投资书籍使用公共知识入口。新上传只发布脱敏文本，PDF解析期间仍使用临时原件及已配置MinerU。
2. 在聊天页的“用户档案与策略模式”保存风险偏好、期限和板块。可将不同交易方法的文档放入不同模式并切换；默认使用全部个人策略，空模式不会借用其他模式资料。
3. 在“交易复盘”直接写自然语言，例如“今天计划以12元买入000001，但没有成交，提醒自己别追高”。系统自动抽取并建立索引；原文保存、抽取、索引状态分别查看。纠错时编辑原文，失败仍保留已保存文字。
4. 添加关注股票，再问“结合我的策略和复盘，平安银行000001当前有哪些风险需要核对？”确认回答中的证券、行情日期、资料引用和风险依据。关注列表不等于实际持仓。
5. 展开每日摘要，选择实际有行情的收盘日生成。摘要结合当前关注、模式和公共知识；变更配置不会重写旧摘要，需要查看新配置结果时手动重新生成。
6. 在“全市场收盘行情采集”选择收盘日采集。`partial`表示有缺失或目录边界，查看具体原因后按需重试；不会覆盖已有记录，也不能保证免费来源能补齐全部证券。

## 每日运行

查询服务运行时，默认工作日15:30生成摘要、16:00采集行情，时区Asia/Shanghai。日历明确休市则跳过，来源未知仍由实际行情核验。摘要可独立取得关注证券行情，无需等全市场采集结束。

`DAILY_SUMMARY_ENABLED`控制自动摘要；`MARKET_COLLECTION_ENABLED`控制自动采集，未单独配置时跟随摘要开关。设置为0可关闭对应自动任务，手动入口仍可使用。服务离线时不保证补跑；重新启动后通过页面补生成/补采。详见 `docs/daily-summary.md` 与 `docs/market-collection.md`。

真实进程中断恢复验收、只读任务状态检查和连续交易日观察步骤见 [自动任务与跨交易日验证](scheduled-operations-validation.md)。恢复测试使用独立测试库及固定行情，不替代实际跨日运行结果。

2026-11-28为周六。本次验收采用来源实际返回的2026-11-27收盘数据；以后使用时应以页面显示的真实日期为准。

## 完成范围

本版交付文档导入、知识问答、自然语言复盘与纠错、策略模式、偏好档案、自动行情、规则风险、每日摘要、脱敏与引用。免费源缺失、长期运行观察、历史资料重新导入等事项见验收清单，不代表新增功能待办。多用户、公网部署、券商持仓、交易执行、付费源与回测仍在当前范围外。

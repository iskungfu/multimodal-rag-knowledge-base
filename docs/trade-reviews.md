# 自然语言复盘（Issue #3）

查询服务（8001）的 `chat.html` 新增“交易复盘”折叠区。用户输入自然语言即可保存，不要求逐项填写交易表。保存后显示逐笔买卖、原因、反思教训、情绪与原文依据；“查看最近复盘”可读取最近 20 条。

## API 与存储

- `POST /reviews`：JSON `{ "text": "今天以 12.5 元买入 000001。", "request_id": "<UUID>" }`。`text` 为 1–10000 字符且不能全空白；可省略 `request_id`，此时服务生成 UUID。
- `GET /reviews?limit=20`：返回 `{ "items": [...] }`，`limit` 为 1–100，按创建时间倒序。
- `GET /reviews/{id}`：返回一条复盘，不存在为 404。
- 固定 `owner_id=default_user`，客户端不能传入其他 owner。所有读写都按 owner 过滤；这是无认证的个人使用模式，不能据此声称有多用户权限保障。

使用现有 `MONGO_URL`、`MONGO_DB_NAME`，集合 `trade_reviews`，按 `owner_id + created_at` 建索引。复盘 UUID 使用 MongoDB `_id` 唯一索引去重。同一 UUID 与文本再次提交只读取已有结果，不重复调用模型；同一 UUID 配不同文本返回 409。前端失败重试会保留当前输入和 UUID，输入变化后才生成新编号。

文档包含 `original_text`、`extraction`、`extraction_status`（`pending/succeeded/failed`）、`extraction_error`、`owner_id`、`doc_type=review`、UTC 创建及更新时间。响应将 `_id` 转为 `id`，附带固定免责声明。

先插入文本，再调用模型。网络、配置、模型响应截断均降级为 `model_unavailable`；JSON/schema/原文依据校验失败降级为 `invalid_extraction`。这些情况下 HTTP 201 表示文本已保存，`extraction=null`，不能把它当作抽取成功。首次数据库写入故障为 503；若只有后续抽取结果写入失败，返回已保存文本与提示，数据库保持 `pending`。进程在抽取阶段退出也可能留下 `pending`，原文仍可读；可以通过编辑原文重新发起抽取，没有后台自动重抽任务。

## 编辑与纠错

每条复盘的“编辑原文 / 纠错”仍使用自然语言输入。`PUT /reviews/{id}` 接收 `text`、当前 `revision` 和本次编辑的 `request_id`。保存脱敏新原文时原子递增版本、清空旧抽取结果并撤销旧索引发布，再抽取、重建索引。新抽取失败仍保留纠正后的原文，状态单独显示；再次编辑保存可重抽，补索引按钮只处理索引。

旧记录没有 `revision` 时按 1 处理。旧版本提交返回 409，不覆盖其他页面的修改；同一编辑编号、同一原文和基准版本重试不会再次抽取。请求超时可保留输入原样重试，或点击“查看已保存版本”核对后明确采用新版本继续保存。旧抽取任务和旧索引 worker 不能覆盖新原文或发布过时结果。

本功能更新当前记录，不新增审计或编辑历史。历史聊天已经引用的内容作为历史回答保留；纠错只改变后续检索。

## 抽取语义

使用注入的 `OPEN_API_BASE`、`OPEN_API_KEY`、`LLM_DEFAULT_MODEL`，独立 OpenAI 客户端（45 秒网络超时、无自动重试），无需安装或加载向量模型。LLM 返回单个 JSON 对象；允许代码围栏和前后说明，拒绝多个对象、重复键、半截 JSON、错误字段类型和无依据字段。失败不保存半可信的局部结果或模型原始响应。

`extraction.events` 为逐笔事件数组，避免把买入和卖出、成交和计划合并到同一价格字段：

| 字段 | 语义 |
| --- | --- |
| `status` | `executed` 已成交；`planned` 计划或挂单未成交；`unknown` 无法确定 |
| `stock_code` | 原文明示的六位代码；未给出或存在名称冲突时为 null |
| `mentioned_stock_code` | 保留用户实际写出的代码，便于核对冲突 |
| `stock_name` | 仅保留明示名称，不从代码补全 |
| `direction` | `buy/sell/null` |
| `price` | 明示的每股价格，单位元；计划价只放在计划事件中，缺失为 null |
| `reason`、`evidence` | 原文连续片段；后者包含该事件上下文 |
| `identity_conflict` | 模型发现名称代码冲突时为 true，冲突事件不关联个股 |

`extraction.lesson` 为原文明示反思片段或 null；`emotions` 为原文明示情绪短语列表，未写则为空。应用校验代码、名称、原因、教训、情绪及价格具有文本依据；语义分类仍依赖模型，不是证券主数据核验器。`identity_conflict=false` 不代表名称代码已获权威校验，不能自动用于交易或持仓更新。暂不识别中文数字单价等所有价格写法，格式不符合契约时保留文本供核对。

## 隐私与范围

依照 ADR-0005，`original_text` 指保留用户措辞的**脱敏输入文本**：无敏感字段时逐字保留；有敏感字段时不另存未脱敏副本。模型调用前、数据库写入前及返回前应用基础规则，覆盖连续手机号/证件号/银行卡号、带“姓名/户名/我叫”前缀的姓名、带“持仓金额/买入金额”等明确标签的总额。总额保留数量级，不处理单价和六位代码。

这些规则不保证识别所有自然语言个人信息（例如无标签的人名、分段号码、中文金额）；未实现全应用的通用脱敏。不要将规则覆盖范围当作匿名化保证。异常响应不返回数据库地址、模型原始响应或 SDK 错误详情。

查询服务中 `/reviews` 及其子路径的参数校验失败统一返回固定 422 提示，不回显请求正文、额外字段名、路径编号或查询参数中的敏感输入。

Issue #7 已将复盘接入 Milvus 和三路召回，并统一回答的最终脱敏和免责声明。索引发布与补索引行为见 `docs/personalized-investment-assistant.md`；2026-11-28 增加自然语言编辑纠错及版本保护。

## 验证命令

```bash
python3 -m venv /tmp/shopkeeper-venv
/tmp/shopkeeper-venv/bin/python -m pip install pytest mongomock fastapi httpx pymongo openai python-dotenv requests
/tmp/shopkeeper-venv/bin/python -m pytest -q -p no:cacheprovider knowledge/test/review/test_review.py knowledge/test/market/test_market.py
AFK_RUN_REVIEW_INTEGRATION=1 /tmp/shopkeeper-venv/bin/python -m pytest -q -s --tb=short -p no:cacheprovider knowledge/test/review/test_review_integration.py
node --input-type=module --check < knowledge/front/assets/js/reviews.js
node --input-type=module --check < knowledge/front/assets/js/api.js
```

真实集成测试需注入 `AFK_TEST_DB_NAME`（不同于业务库），只创建 `trade_reviews_issue3_<uuid>` 集合，测试后仅删除该集合。10 个模型样本全部为自建无敏感文本，覆盖已成交、计划、只给名称、名称代码冲突、缺失字段、同文多次买卖及计划、撤单未成交、纯反思、数量百分比和减仓计划。另有一项真实 MongoDB + 故障注入验证坏 JSON 仍保存文本，不把该项计作真实模型测试。

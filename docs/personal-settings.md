# 用户档案与策略模式

打开查询页“用户档案与策略模式”，设置自述风险偏好、投资期限、偏好板块和补充文字，点击“保存偏好并应用模式”。不设置时保持全部个人策略，没有默认替用户选择风险偏好。

在“管理策略模式”中为已导入的策略文档分组，例如“回踩纪律”与“现金流纪律”。可以创建、改名、调整绑定文档、删除，并通过“当前策略模式”切换。模式绑定的是完整文档，不要求将策略改写为结构化规则。支持最多20个模式、每个最多100份文档；页面最多展示500份已完成个人文档，达到上限会提示。

选中的模式只限制个人策略一路，公共知识与本人的复盘继续检索。选择没有文档的模式时，个人策略为空，不回退到全部文档。模式过滤在稠密及稀疏向量查询前生效，返回后复核文档归属、模式成员和导入完成状态。已有32位十六进制导入编号保持原样，不改写为带连字符的UUID。

## API 与持久化

- `GET /investor-settings`：读取当前配置；未保存过时返回 revision=0、空档案和全部个人策略。
- `PUT /investor-settings`：完整保存 `revision`、`profile`、`modes`、`active_mode_id`。版本匹配后原子保存并递增，409表示其他页面已经更新，422表示内容不符合约束，503表示无法确认保存。
- `GET /investor-settings/documents`：返回本人的已完成个人策略文档与截断标记。

MongoDB `investor_settings` 只写 `_id=default_user` 的一个文档。`profile` 接收 `risk_preference=unspecified/cautious/balanced/adventurous`、`horizon=unspecified/short/medium/long`、`preferred_sectors`（最多10个，每个40字）与 `notes`（1000字）。`modes` 每项包含UUID `id`、`name`（60字）与 `document_ids`；`active_mode_id=null` 表示全部个人策略。API 不接受客户端 owner。

偏好、板块、补充文字和模式名称在写入与读取时使用现有脱敏规则。校验错误不回显输入或底层异常。保存失败保留表单；重新加载有未保存内容时，需要明确选择用保存的配置替换表单。

长度限制同时检查原输入与脱敏后的内容。脱敏替换导致长度超限时，写入前返回422并提示缩短，原配置保持可读。

## 生效方式

每次问答和每次摘要生成读取一次配置快照。偏好进入检索及资料选择上下文，服务端展示本次使用的模式与自述偏好。风险评估仍由行情规则决定，偏好不降低风险等级、不证明适合投资，也不推断持仓。档案读取失败时不默默扩大到全部策略。

摘要结果保存本次偏好和模式快照。切换模式不会改写历史回答或摘要；当天已有成功摘要时需“重试 / 重新生成”才应用新配置。只在生成开始时固定配置；过程中切换影响下一次任务。

## 验证

离线测试覆盖 `knowledge/test/investment/test_personalization.py`、`knowledge/test/review/test_review_edit.py` 及摘要测试。真实验收使用独立测试库：

```powershell
$env:AFK_TEST_DB_NAME='afk_personal_mvp_test'
python scripts/verify_investment_assistant.py --host --personal-mvp
```

需要现有模型、行情与存储配置。探针生成独立随机集合，走真实导入、模式切换、自然语言纠错、BGE召回、LLM选择、API及一次摘要调度；结束只清理本次资源。加 `--serve --port 8003 --stop-file <尚不存在的文件>` 可在检查后进行浏览器验收。

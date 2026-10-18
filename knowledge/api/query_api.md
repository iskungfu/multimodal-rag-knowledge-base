# 查询服务接口文档

| 项目 | 说明 |
|------|------|
| 服务名称 | 掌柜智库查询服务 |
| Base URL | `http://127.0.0.1:8001` |
| 实现文件 | `knowledge/api/query_api.py`、`knowledge/service/query_service.py`、`knowledge/schema/query_schema.py` |
| 对应前端 | `knowledge/front/chat.html` |
| 启动命令 | `python -m knowledge.api.query_api` |
| 聊天页面 | `http://127.0.0.1:8001/front/chat.html`（`/` 会自动跳转） |
| 在线文档 | `http://127.0.0.1:8001/docs` |
| 鉴权 | 无 |

---

## 1. 核心标识

| 标识 | 生命周期 | 用途 |
|------|---------|------|
| `session_id` | 浏览器标签页（localStorage，多轮对话共享） | MongoDB 历史记录归属 |
| `task_id` | 单次提问（后端生成，用完即弃） | 任务状态追踪 + SSE 队列定位 |

两者分离的原因：同一 session 下连续提问时，如果共用 task_id，任务状态和 SSE 队列会互相覆盖。

---

## 2. 前端调用时序

```
页面加载
  └─ GET /history/{session_id}          拉取历史消息渲染

用户发送问题（流式输出开启）
  ├─ POST /query                        返回 task_id
  └─ GET  /stream/{task_id}             SSE：progress → delta… → final

用户发送问题（流式输出关闭）
  └─ POST /query                        阻塞等待，直接返回 answer

点击「清空对话」
  └─ DELETE /history/{session_id}
```

---

## 3. POST /query

提交一次查询。

### 3.1 请求

| 位置 | 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| Body | `query` | string | 是 | 用户问题 |
| Body | `session_id` | string \| null | 否 | 会话ID，不传则由后端生成 |
| Body | `is_stream` | boolean | 否 | 是否流式，默认 `false` |

```json
{
  "query": "RS-12 数字万用表如何测量直流电压？",
  "session_id": "sess-3f2a1c9b8d7e6f5a",
  "is_stream": true
}
```

### 3.2 响应 A：流式（`is_stream = true`）

HTTP 200，立即返回，主流程在后台执行：

```json
{
  "message": "Query submitted",
  "session_id": "sess-3f2a1c9b8d7e6f5a",
  "task_id": "8c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f"
}
```

拿到 `task_id` 后立即建立 SSE 连接：`GET /stream/{task_id}`。

### 3.3 响应 B：非流式（`is_stream = false`）

HTTP 200，等待主流程执行完毕后返回：

```json
{
  "message": "处理完成",
  "session_id": "sess-3f2a1c9b8d7e6f5a",
  "answer": "使用 RS PRO RS-12 数字万用表测量直流电压时……",
  "done_list": ["确认问题产品", "切片搜索", "倒排融合", "重排序", "生成答案"],
  "error": "",
  "image_urls": []
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `message` | string | 响应消息 |
| `session_id` | string | 会话ID |
| `answer` | string | 生成的答案，失败时为空 |
| `done_list` | string[] | 已完成节点（中文名），用于渲染进度 |
| `error` | string | 错误信息，空字符串表示成功 |
| `image_urls` | string[] | 答案引用的图片URL（当前为空，图片由前端从答案的【图片】区块解析） |

### 3.4 错误

| 状态码 | 场景 |
|--------|------|
| 422 | 请求体校验失败（如缺少 `query`） |
| 500 | 服务内部异常 |

> 主流程内部异常（如 LLM 调用失败）不会抛 500，而是走各节点的降级逻辑，最终在 `answer` 中返回提示文案。

---

## 4. GET /stream/{task_id}

SSE 长连接，推送查询进度与流式答案。

| 位置 | 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| Path | `task_id` | string | 是 | `POST /query` 返回的任务ID |

响应 `Content-Type: text/event-stream`，每条消息格式：

```
event: <event_name>
data: <json>

```

### 4.1 `progress` —— 节点进度

每次节点状态变化推送**全量快照**（前端覆盖式渲染）：

```json
{
  "status": "processing",
  "done_list": ["确认问题产品"],
  "running_list": ["切片搜索", "切片搜索(假设性文档)", "网络搜索"]
}
```

| 字段 | 说明 |
|------|------|
| `status` | `processing` / `completed` / `failed` |
| `done_list` | 已完成节点（中文名） |
| `running_list` | 运行中节点（中文名），并行分支可能同时出现多个 |

### 4.2 `delta` —— LLM 流式增量

```json
{ "delta": "使用 RS PRO" }
```

前端把多个 `delta` 累加为完整答案并实时渲染。

### 4.3 `final` —— 最终答案 / 连接结束

```json
{
  "answer": "完整答案文本",
  "image_urls": []
}
```

- 流式生成时后端可能推送空对象 `{}`（答案已通过 `delta` 推送完），前端回退使用累积文本；
- “已有答案”分支（如商品名无法识别）会直接推送 `{"answer": "抱歉..."}`。

> 前端同时监听了 `final_answer` 作为别名，后端当前只发送 `final`。

### 4.4 连接生命周期

| 场景 | 行为 |
|------|------|
| 正常完成 | 前端收到 `final` 后主动 `es.close()` |
| 客户端断开 | 后端检测 `is_disconnected()` 后退出，并清理队列 |
| 任务不存在 | 生成器直接结束，前端触发 `error` 回调 |

---

## 5. GET /status/{task_id}

任务状态查询（前端定义了 `poll()` 但未启用，用于调试或轮询兜底）。

```json
{
  "status": "completed",
  "done_list": ["确认问题产品", "切片搜索", "倒排融合", "重排序", "生成答案"],
  "running_list": [],
  "durations": {},
  "answer": "完整答案文本",
  "error": ""
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `processing` / `completed` / `failed`，任务未开始时为空串 |
| `done_list` | string[] | 已完成节点 |
| `running_list` | string[] | 运行中节点 |
| `durations` | object | 各节点耗时（秒），查询侧当前未记录，恒为空对象 |
| `answer` | string | 答案，完成后有值 |
| `error` | string | 错误信息 |

---

## 6. GET /history/{session_id}

读取某个会话的历史消息。

| 位置 | 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| Path | `session_id` | string | 是 | 会话ID |
| Query | `limit` | int | 否 | 返回条数，默认 `50` |

```json
{
  "session_id": "sess-3f2a1c9b8d7e6f5a",
  "items": [
    {
      "id": "",
      "session_id": "sess-3f2a1c9b8d7e6f5a",
      "role": "user",
      "text": "RS-12 数字万用表如何测量直流电压？",
      "rewritten_query": "RS-12 数字万用表如何测量直流电压？",
      "item_names": ["RS PRO RS-12 数字万用表"],
      "ts": 1773926682.007168
    },
    {
      "id": "",
      "session_id": "sess-3f2a1c9b8d7e6f5a",
      "role": "assistant",
      "text": "使用 RS PRO RS-12 数字万用表测量直流电压时……",
      "rewritten_query": "",
      "item_names": ["RS PRO RS-12 数字万用表"],
      "ts": 1773926695.412345
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `items[].role` | string | `user` / `assistant` |
| `items[].text` | string | 消息内容 |
| `items[].rewritten_query` | string | 用户消息的重写查询（助手消息为空） |
| `items[].item_names` | string[] | 关联商品名 |
| `items[].ts` | float | Unix 时间戳（秒） |
| `items[].id` | string | 消息ID，当前未使用 |

说明：

- 返回按**时间正序**（老 → 新），前端顺序渲染；
- 数据来源 MongoDB `chat_history` 集合，由 `answer_output_node` 在每次查询结束后写入。

---

## 7. DELETE /history/{session_id}

清空某个会话的全部历史消息。

```json
{
  "message": "History cleared",
  "deleted_count": 12
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `message` | string | 固定文案 |
| `deleted_count` | int | 实际删除条数 |

---

## 8. 错误码汇总

| 状态码 | 说明 |
|--------|------|
| 200 | 成功 |
| 404 | `chat.html` 不存在（访问 `/chat.html` 时） |
| 422 | 请求体 / 查询参数校验失败 |
| 500 | 服务内部异常 |

---

## 9. 前端字段依赖清单

便于后端调整响应结构时对照：

| 接口 | 依赖字段 | 缺失时前端表现 |
|------|---------|---------------|
| `POST /query`（流式） | `task_id` | 无法建立 SSE，直接报错 |
| `POST /query`（非流式） | `answer`、`done_list`、`error`、`image_urls` | `done_list`/`image_urls` 按空数组处理，`error` 按成功处理 |
| `GET /stream` `progress` | `status`、`done_list`、`running_list` | 按空数组处理 |
| `GET /stream` `delta` | `delta` | 跳过该块 |
| `GET /stream` `final` | `answer`、`image_urls` | `answer` 缺失回退使用累积 delta 文本 |
| `GET /history` | `items[].role`、`items[].text`、`items[].ts` | 渲染空内容 |
| `DELETE /history` | 仅检查 HTTP 状态码 | 提示“仅清空本地显示” |

---

## 10. 调用示例

### 10.1 非流式

```bash
curl -X POST http://127.0.0.1:8001/query ^
  -H "Content-Type: application/json" ^
  -d "{\"query\":\"RS-12 数字万用表如何测量直流电压？\",\"session_id\":\"sess-demo\",\"is_stream\":false}"
```

### 10.2 流式

```bash
# 1. 提交，拿到 task_id
curl -X POST http://127.0.0.1:8001/query ^
  -H "Content-Type: application/json" ^
  -d "{\"query\":\"RS-12 数字万用表如何测量直流电压？\",\"session_id\":\"sess-demo\",\"is_stream\":true}"

# 2. 建立 SSE 连接
curl -N http://127.0.0.1:8001/stream/{task_id}
```

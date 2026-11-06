# HTTP、网页回复和对话回调协议

所有 API 使用 JSON，对话传输与任务状态分开。一个任务拥有一条原对话，问题可以分多轮发布，网页回复始终回到这条对话。

## 请求与角色

默认服务为 `http://127.0.0.1:8765`。API 请求使用：

```http
Authorization: Bearer YOUR_ROLE_TOKEN
Content-Type: application/json
```

`POST` 必须带准确 `Content-Length`；不接收分块编码。路径不带查询参数，访问凭据不放在查询字符串中。服务校验 `Host` 与同源 `Origin`，没有开放跨站调用。

本地 `.bridge/credentials.json` 保存两种随机凭据：

| 凭据 | 权限 |
| --- | --- |
| `browser_token` | 创建上传任务、读取任务和文件、回复当前问题、重试已明确失败的事件。 |
| `worker_token` | 读取任务和文件、发布问题、提交完成结果、补录不确定事件回执；本机还可关闭服务。 |

工作端是受信任执行端，凭据持有者可以处理本实例全部任务。网页同样使用实例共享凭据，没有用户级隔离。网页不会收到 worker 凭据。

`GET /`、`/static/app.js`、`/static/styles.css` 仅提供静态页面，不包含任务数据；任务 API 均需认证。静态页面接收 `/#token=...` 后，在浏览器本地取出 token 并移除 fragment。

## API 一览

| 方法与路径 | 角色 | 请求 / 响应 |
| --- | --- | --- |
| `GET /api/config` | 网页 | 返回适配器类型、版本及上传限额，不返回回调配置或凭据。 |
| `GET /api/tasks` | 网页 | `{"tasks":[...]}` |
| `GET /api/tasks/{task_id}` | 网页 | `{"task":{...}}` |
| `POST /api/tasks` | 网页 | 上传并创建任务，返回 `201` 与 `{"task":{...}}`。 |
| `GET /api/tasks/{task_id}/files/{storage_name}` | 网页 | 下载当前任务的原上传文件。 |
| `POST /api/tasks/{task_id}/reply` | 网页 | 回复当前问题组，返回更新后的任务。 |
| `POST /api/tasks/{task_id}/events/{event_id}/retry` | 网页 | 请求体 `{}`，仅重试 `failed` 事件，保留原事件 ID。 |
| `GET /api/worker/tasks` | 工作端 | 任务列表，文件附本机路径及工作端下载路径。 |
| `GET /api/worker/tasks/{task_id}` | 工作端 | 单任务，文件附本机路径及工作端下载路径。 |
| `GET /api/worker/tasks/{task_id}/files/{storage_name}` | 工作端 | 使用 worker 凭据下载原上传文件。 |
| `POST /api/worker/tasks/{task_id}/questions` | 工作端 | 发布问题组，返回更新后的任务。 |
| `POST /api/worker/tasks/{task_id}/complete` | 工作端 | 提交结果并完成任务。 |
| `POST /api/worker/tasks/{task_id}/events/{event_id}/resolve` | 工作端 | 仅为 `unknown` 事件补录已核实回执，不重发。 |
| `POST /api/worker/shutdown` | 工作端 + 本机 | 请求体 `{}`，仅回环地址或服务绑定的本机地址可关闭此服务实例。 |

任务、事件、文件存储编号均由服务生成，HTTP 路径中使用 32 位小写十六进制 ID。文件原名仅作为元数据和下载名称，不用作磁盘路径。

## 创建任务与上传文件

```json
{
  "title": "检查需求文档",
  "data": {"message": "请整理关键事项，缺少信息时先提问。"},
  "files": [
    {"name": "notes.txt", "content_base64": "SGVsbG8="}
  ],
  "request_key": "upload-example-0001"
}
```

四个顶层字段均须提供。`files` 可为 `[]`，`data` 是 JSON 对象；至少提供文件或任务数据。标题最多 200 字，HTTP 接口的 `data` 编码后不超过 256 KiB。

默认限额为单文件 4 MiB、合计 12 MiB、最多 10 个文件，实际值见 `/api/config`。文件内容使用严格 Base64 编码，路径分隔符或无效文件名会被拒绝。服务先保存文件并计算 SHA256，再将任务和初始 `dialogue.start` 事件放入数据库。

任务文件元数据：

```json
{
  "name": "notes.txt",
  "storage_name": "00000000000000000000000000000000",
  "sha256": "文件内容的64位SHA256",
  "size": 5
}
```

示例编号仅说明形状，不是可用的实际文件。工作端任务响应还包含 `local_path` 和 `download_path`。远程接收端应使用 `download_path` 加 worker 凭据下载；Webhook 载荷会移除 `local_path`，不会传送本机绝对路径或文件 Base64 内容。

下载前再次验证文件属于当前任务且内容指纹未变。上传文件只作为待处理数据，不会由桥接器自动执行。

## 发布问题

工作端向 `/api/worker/tasks/{task_id}/questions` 提交：

```json
{
  "questions": [
    {
      "id": "output_format",
      "title": "希望结果以什么形式交付？",
      "options": ["简要摘要", "逐项清单"]
    }
  ],
  "request_key": "question-example-0001"
}
```

每组 1 至 10 个问题。`id` 在组内唯一，以英文字母开头，可含字母、数字、`_.:-`，总长最多 80；问题正文最多 4000 字。`options` 可省略；提供时为 1 至 10 条不重复的建议，每条最多 300 字。

服务生成单调递增的 `question_group.revision`，并将任务设为 `needs_input`。相同待回复问题重复发布保留原 revision；改成另一组问题时，旧问题进入 `history`，新组产生新 revision。

前一轮网页回复的回调尚未确认送达时，不接受发布下一组问题，返回 `409 reply_not_delivered`。CLI 的 `publish-questions` 会对这个短暂竞态做有限等待；回调最终失败或结果不明仍需先核对投递状态。

## 网页回复

网页向 `/api/tasks/{task_id}/reply` 提交：

```json
{
  "revision": 1,
  "answers": [
    {"question_id": "output_format", "text": "逐项清单，并标出缺失信息。"}
  ],
  "request_key": "reply-example-0001"
}
```

`revision` 必须是当前待回复组的版本，`answers` 必须且只能覆盖本组全部问题。每条回复去除两端空白后不得为空，最多 12000 字，合计最多 24000 字。

问题已被替换、已回复或任务已完成时，不接受新的回复。版本冲突返回 `409`，网页保留旧草稿供核对；不会将旧回复自动套到新问题上。

回复与 `dialogue.reply` 事件在同一事务中提交。成功返回时，问题组已为 `answered`，网页立即移除输入卡，任务回到 `running`；**这时仅表示回复已保存，不表示 AI 已经收到**。实际送达情况查看对应事件状态。已提交的回答可在回复记录中查看。

建议选项只协助填写，允许自由文本。回复属于需求澄清，不代表批准执行文件改写、生产变更或其他需要独立授权的操作。

## 完成结果

工作端向 `/api/worker/tasks/{task_id}/complete` 提交：

```json
{
  "result": {"summary": "已整理出五项待办。"},
  "request_key": "complete-example-0001"
}
```

`result` 是不超过 1 MiB 的 JSON 对象，网页显示其内容。任务进入 `completed` 终态，待回复组归档，尚未发送的 `pending` / `failed` 回调变为 `cancelled`。已在发送中的事件仍记录其实际回执或不确定状态，不把未知动作伪装为已撤销。

当前下载接口提供原上传文件；结果对象中的路径文字不会自动变成可下载的生成文件。需要专门输出文件下载时，应在自己的接入服务中扩展结果存储与下载约定。

## 任务与投递状态

| 任务状态 | 含义 |
| --- | --- |
| `queued` | 任务和发起对话事件已保存。 |
| `running` | 对话正在启动、执行，或网页回复已保存等待继续。 |
| `needs_input` | 当前有一组问题等待网页回复。 |
| `completed` | 已提交最终结果，终态。 |

| 事件状态 | 含义 |
| --- | --- |
| `pending` | 等待调度。 |
| `sending` | 正在调用接入端。 |
| `delivered` | 接入端返回明确有效的原对话回执。 |
| `failed` | 已知未被接收，可人工重试原事件。 |
| `unknown` | 动作可能已发生，不能直接重放。 |
| `cancelled` | 任务完成前尚未发送的事件已取消。 |

SQLite 保存任务、问题版本、历史、幂等请求和持久化待投递队列。一个数据目录由单个服务进程持锁运行。调度器只在前置对话存在时投递回复；已有对话身份不能被回调结果替换。

服务重启时，遗留 `sending` 一律转为 `unknown`，不会推断它从未执行。调用方应区分“接入端接受消息”和“AI 已处理完成”，后者通过问题或完成 API 回报。

## 幂等约定

`request_key` 为 8 至 128 个字母、数字或 `_.:-`。创建任务的键在实例内去重；问题、回复和完成的键分别绑定任务与操作。

- 同键、同内容：返回当前原任务，不新增任务、回复或事件。
- 同键、不同内容：`409 idempotency_conflict`。
- 网络中断后重试同一次提交：继续用原键和原内容。
- 修改内容后提交：换新键；回复仍必须符合当前 revision。

网页会为当前提交保存一个键，网络失败但内容未变时复用。CLI 默认从任务、操作和内容生成稳定键；发布问题还绑定当前提问轮次，使下一轮可以重新询问相同问题，而本轮重试继续幂等。也可显式传 `--request-key`。

数据库事务防止重复本地创建；外部接收端仍须自行按回调事件 ID 去重。协议不承诺跨网络的通用“恰好一次”执行。

## Webhook 回调

回调地址由服务端本地配置提供，不由网页提交。服务不跟随跳转，也不使用环境 HTTP 代理。可选请求头：

```http
POST /your-callback-path
Authorization: Bearer YOUR_CALLBACK_TOKEN
Content-Type: application/json; charset=utf-8
Idempotency-Key: EVENT_ID
```

请求结构：

```json
{
  "event_id": "事件ID",
  "type": "dialogue.reply",
  "task": {
    "id": "任务ID",
    "title": "任务标题",
    "data": {"message": "需求文本"},
    "files": [],
    "thread": {"adapter": "webhook", "thread_id": "your-thread-id"}
  },
  "event": {
    "id": "事件ID",
    "task_id": "任务ID",
    "type": "dialogue.reply",
    "revision": 1,
    "questions": [{"id": "details", "title": "需要补充什么？"}],
    "answers": [{"question_id": "details", "text": "用户填写的回复"}]
  }
}
```

以上只列出主要字段；实际 `task` 和 `event` 还含创建时间、状态、历史和回执等。`dialogue.start` 时 `task.thread` 为 `null`；`dialogue.reply` 时已包含原对话。发送事件时尚未核实其回执，不应把 `event.status` 当成已经接受的证明。

接收成功返回 `2xx`，JSON 为：

```json
{"accepted": true, "thread_id": "your-thread-id"}
```

对话 ID 只能包含字母、数字、下划线、连字符，总长最多 128。回复事件必须返回任务原有的同一 `thread_id`。回调配置的 URL 指纹也绑定原对话，不能将旧任务切换到另一接收地址后继续投递。

只有**确实没有接收或执行**事件时，接收端才可返回如下明确拒收回执：

```json
{"accepted": false, "retryable": true}
```

服务将其视为 `failed`，允许人工重试原事件。明确的连接拒绝、DNS 失败也属于未送达。超时、响应丢失、无效 JSON、缺少有效回执、返回另一对话，以及没有明确拒收证明的 HTTP 错误都按 `unknown` 处理。成功响应体上限为 64 KiB，发送回调载荷上限为 4 MiB。

不要在“已经接受，但处理较慢”时返回 `accepted:false`；应先持久化接收、返回原对话回执，再异步处理。接收端应将事件 ID 与接收状态、原对话身份一起存储，重复事件返回相同结果。

## 核对 unknown 事件

`unknown` 没有网页重试按钮，重试 API 也会拒绝它。管理员应先在外部接收服务或桌面客户端核实原动作：是否已创建对话、原回复是否已送达、实际归属哪个项目。服务不会扫描或改写客户端私有数据库。

确认原动作确已接受后，工作端调用：

```json
{
  "receipt": {
    "adapter": "webhook",
    "thread_id": "verified-original-thread-id",
    "callback_peer": "当前原回调URL的SHA256"
  }
}
```

这是 `/api/worker/tasks/{task_id}/events/{event_id}/resolve` 的请求体。这里需要适配器内部的**完整已核实回执**，不是直接照抄接收端的 `accepted` 响应。Webhook 的 `callback_peer` 是原配置 URL 的 UTF-8 SHA256，可通过 `python bridge.py check` 只读查看；桌面回执包含 `adapter`、`thread_id`、`host_id`、`project_id`、`project_path`，均须与已核实的原任务绑定一致。不要用新建对话绕过结果不明的原动作。

CLI 的等效调用：

```bash
python bridge.py resolve-event --task-id TASK_ID --event-id EVENT_ID --file receipt.local.json
```

补录只将原事件标为 `delivered` 并登记原对话身份，不会重发。如果无法核实，不要编造回执，应保留 `unknown` 供进一步调查。

## 错误格式

```json
{"error":{"code":"stale_questions","message":"问题已变化，请刷新任务。"}}
```

常见状态码：`400` 参数或格式无效，`401` 角色凭据无效，`403` 来源或主机不符，`404` 任务/文件/接口不存在，`409` 幂等、版本、终态或原对话绑定冲突，`413` 上传过大，`415` 请求类型无效。

HTTP 成功只证明对应接口操作已保存或确认，不替代 AI 完成结果、文件实际处理或外部业务批准。

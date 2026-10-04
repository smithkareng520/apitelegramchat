# Responses API Response Chain 状态设计（官方 server-managed state）

## 目标

多轮上下文的唯一载体是 OpenAI Responses API 的服务端 response chain。
每个 chat 只保存一个 `previous_response_id`（连同产生它的
model / vendor_key），成功创建 response 后原子推进链头。本地
`conversation_history` 仍按项目自身语义维护（供 UI / 渲染 / bootstrap
使用），但**绝不**再参与日常轮次的上下文拼装——不存在"水位 + 增量
历史"的镜像账本，也不存在 manual replay fallback。不创建、读取、回拉
或删除 Conversations 对象。

## 正常轮次

```python
client.responses.create(
    model=model,
    instructions=instructions,          # 每轮显式传入（不随链继承）
    previous_response_id=chain_head,    # 上一条成功 response
    input=[user_item],                  # 只带本回合新增的用户内容
    ...
)
```

- 链式轮次的 `input` 由"最后一条 assistant 消息之后的尾部"推导
  （`_turn_input_messages`），即本回合新增的 user 消息（及其后追加的
  运行时 system 提示——system 项汇入 `instructions`，不进 input）。
- 只有收到 `response.completed` 且取得有效 `response.id` 后，回合收尾
  才把该 id 提交为 chat 级链头（generation fencing 保证 /clear 后的
  迟到回合提交被拒绝）。

## 工具轮次

同一 agent turn 内：

```
Response A（function_call，可能完全没有文本）
   ↓  从结构化 output 识别 function_call（绝不从 raw_content /
      assistant 文本 / 历史切片推导）
执行工具（MCP / 内置工具）
   ↓
responses.create(
    previous_response_id=A.id,
    input=[function_call_output(..., call_id 与 A 中配对)]
)
   ↓
Response B（最终回复或下一个 function_call）
```

- `response.output` 是权威来源；流式 delta 只负责实时 UI。
- 工具调用 response 完全可能没有 `output_text`——绝不以
  `output_text == ""` 判定 response 为空。
- `response.failed` / `response.incomplete` / 顶层 `error` 永远不能驱动
  工具执行，也不会成为新的 `previous_response_id`。
- `pending_tool_input_items` 是 wire 级续轮载荷：工具执行可能改写本地
  历史，续轮 input 绝不通过任何历史差分重新发现。

## Empty-input invariant（严格禁止空 input）

在真正调用 SDK 之前做协议级校验：

```python
if not input_items:
    raise ResponsesProtocolError(
        "Responses continuation requires non-empty input"
    )
```

绝不发送 `input: []`，也绝不伪造一条假消息去满足供应商的传输契约。

## instructions 单独处理

官方语义：使用 `previous_response_id` 时，上一 response 顶层的
`instructions` 不会自动继承。因此每一轮都从当前 canonical system
prompt 重新生成 `instructions`（系统提示、技能上下文等变化立即生效）。

## 异常状态：原子推进

```
old_response_id
       │
       ├── request failed          → 保持 old_response_id
       ├── stream missing terminal → 断链（已出网，半轮差异）
       ├── turn interrupted        → 断链（已出网）
       ├── tool rounds exhausted   → 断链（function_call_output 未发出）
       └── response success        → commit new_response_id
```

请求失败但未出网（create 抛错）时保留旧链头是安全的：服务端没有收到
任何新内容。已出网后的任何异常都显式断链，下一轮 bootstrap。

## previous_response_id 失效（明确的异常恢复路径）

服务端**明确**返回 previous response 不存在/不可用（错误文本严格匹配
`previous_response_id` / `response not found` 等标记，仅限 400/404）时，
只重试一次：

1. 清除旧链头；
2. 用本地 canonical history 全量 bootstrap（只调用 `/v1/responses`，
   不存在 Chat Completions / Conversations fallback）；
3. bootstrap 成功后链头恢复为该 response 的 id。

普通 4xx（参数校验、空 input、工具参数错误等）不能证明链失效，按普通
错误上抛，不触发 bootstrap。bootstrap 是异常恢复路径，不是常规工作
模式——平时每一轮绝不偷偷 replay 全历史。

### 例外：工具续轮（`previous_response_id` + 纯 `function_call_output`）

这一类请求被网关/上游以 **任意 4xx（400/404/409/422）** 拒绝时，一律按
“网关不支持链式续轮”处理，不再匹配错误文本：

- 网关的拒绝措辞随上游而变（`input must be non-empty`、
  `Upstream request failed: invalid request` ……），文本匹配会让恢复路径
  在换模型/换上游后失效，整回合失败并丢掉已执行的工具结果。
- 恢复动作：丢弃 `previous_response_id` 与 pending 的 `function_call_output`，
  用本地 canonical history（已含 `function_call` 与其 output）bootstrap
  一次；每个续轮最多一次，bootstrap 也被拒则原样上抛（说明请求本身有问题）。
- 记住该 `(端点, 模型)`，之后的工具续轮直接 bootstrap；记忆带 30 分钟
  TTL，到期后重新探测一次链式续轮。
- 401/403/429/5xx 与链无关，不触发此恢复。
- 每次触发都会 WARNING 输出网关原始错误体、请求形状、`response.created`
  与 `response.completed` 的 id，用于定位网关真实原因。

## function_call 的识别与兜底

- 以 `response.completed.output` 为权威来源；整个 response 已 `completed`
  时，`status=in_progress` 的 function_call 视为已完成（翻译型网关不会回写
  item 状态），`incomplete/failed/cancelled` 仍不执行。
- 权威 output 里一个 function_call 都没有、但流事件里累积到了调用时，用
  累积器兜底执行；此时服务端保存的 response 未必认得这些 `call_id`，续轮
  直接 bootstrap，不走链。
- 空终局（无文本、无工具调用）不提交链头并作废链：本地历史会把该 user
  消息标为未回应，服务端链却已包含它和那个空 response，提交会造成两边错位。
- 每轮输出一行响应摘要日志（output item 的 type:status、事件计数、文本/
  reasoning 长度），空响应类问题据此定位。

## 链失效的显式来源

| 事件 | 处理 | 调用点 |
| --- | --- | --- |
| `/clear` | generation +1，链头清空 | `state.safe_clear_history` |
| 模型/端点切换 | 断链（不复活旧模型链） | `ResponseState.resolve_chain` |
| 本地压缩淘汰 | 断链，下一轮以压缩后上下文 bootstrap | `app_turns.pre_flight_context_check` |
| Chat Completions / Anthropic / Gemini 回合 | 断链（该回合不在服务端链里） | `protocols/*` 的 `mark_legacy_divergence` |
| 请求已出网后中断/异常 | 断链 | bridge `_invalidate_on_interrupt` |
| 服务端判定链头不存在 | 断链 + 一次性 bootstrap | bridge stale-ID retry |

## 重启恢复：恢复的是 ID，不是伪造历史

可持久化/恢复的状态单元只有 `(chat_id, model, previous_response_id)`
（`responses_state.export_chain_state` / `restore_chain_state`）。服务端
持有全部对话上下文，恢复链头后继续官方 chain；本地历史为空不影响链式
续轮。若服务端明确返回该 ID 不可用，走上述一次性 bootstrap。

## 已移除的机制

- `mirror_seq` 序列号 / `synced_through_seq` 水位 / 增量切片（incremental）
- 写入者台账（writer ledger）与 `noop` 模式
- `ResponseSyncPlan` / `plan_response_request` 水位规划器
- `note_entry_rewritten` / `note_entry_retracted` 镜像改写作废
- `structural_epoch` / `mark_structural_fork`（由 `invalidate_response_chain`
  取代）
- `client.conversations.*`（此前已移除，保持移除状态）

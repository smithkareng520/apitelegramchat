# 多厂商 / 多模型下的 Conversation 与上下文同步状态机

> 本文档描述 2026-09 会话同步层重构的设计与实现，是
> `src/conversation_state.py` / `src/server_compaction.py` /
> `src/ai/responses_bridge.py`（Conversation State 一节）的设计依据。
> 各模块 docstring 与本文互为对照。

## 一、核心架构原则

### 1. 单一事实来源（Single Source of Truth）

本地维护一份标准、权威的对话上下文镜像：`ctx["conversation_history"]`
（内部 `core.messages.Message` 列表，所有厂商共享）。任何协议适配器都只是
把它渲染成各自的线上形状；厂商的服务端会话（Responses `conversation`
对象）只是这份镜像的**投影副本**——镜像可以被回拉覆盖刷新，但永远不会
因为"服务端有什么"而被静默改写。

### 2. 日常态（增量优先）

在同一厂商 / 支持 Responses API 的体系内，最大化利用服务端托管的
`conversation_id`：日常交互只发送增量 `input`（通常就是本轮新增的 user
消息；工具续轮只发配对的 `function_call_output`），不再每轮全量重发历史。

### 3. 分叉态（作废重建）

凡是遇到跨厂商历史分叉、本地主动压缩或清空对话等场景，坚决避免脆弱的
"双向增量差分追加"，一律**作废**当前 `conversation_id`；下一次请求以本地
最新全量上下文初始化一个全新会话，借由服务端的前缀匹配机制自动命中底层
Prompt Cache。

## 二、状态机与厂商隔离

### 相位（每个 chat × 每个厂商一个 `VendorConversationRef`）

```
IDLE ──首次 Responses 请求──▶ DAILY ──增量请求（仅发 input 增量）──▶ DAILY
                                │
                                │ 触发分叉的事件（见下）        ┌──┘
                                ▼                              │
                              FORK ◀───────────────────────────┘
                                │ 下一次 Responses 请求：作废旧 id
                                │ + 本地全量上下文自举 → 新 DAILY
DAILY ──服务端压缩事件──▶ SYNCING（持 chat 锁回拉）
                                │ 回拉成功：Adapter 清洗覆盖镜像 → DAILY
                                │ 回拉失败：兜底作废 → FORK
```

触发分叉（作废重建）的事件：

| 事件 | 判定机制 | 对应需求 |
| --- | --- | --- |
| 本地压缩结构性淘汰（滑动窗口 / 摘要合并） | `structural_epoch` 纪元闸门（`app_turns.pre_flight_context_check` 调用 `mark_structural_fork`） | 二.2 |
| 跨厂商写入 / 传统模型写入 / 打断保全写入 | 写入者台账：水位之后镜像条目的写入者必须 ∈ {`user`, 本厂商} | 二.3 |
| 服务端压缩回拉覆盖（其他厂商的镜像基准被替换） | 覆盖时显式作废其他厂商 ref | 二.1 |
| 系统提示变化（技能激活等） | `_head_instructions_key` 指纹比对（`instructions_changed`） | 派生规则 |
| `/clear` | `reset()` 清空全部厂商绑定 + generation++ | 二.4 |

### 厂商间 ID 强隔离

`vendor_conversations: Record<VendorKey, VendorConversationRef>` 按厂商分区。
VendorKey = `provider|endpoint|protocol`（`derive_vendor_key`）：

- 同厂商同端点内切换模型（均支持 Responses API）⇒ 同一键 ⇒ **保留
  conversation_id**，请求按需更新 `model` 参数，仅发送增量 input；
- 不同厂商 / 不同端点 / 不同协议 ⇒ 不同键 ⇒ 服务端会话 ID 严禁跨用。

### 同步账本（为什么不是 "revision == 消息条数"）

旧实现把游标同步位置与 `canonical_revision`（批次计数）混用，且未扣除
system 头、未考虑"一次 append 批次 ≠ 一条消息"，导致增量判定极脆弱
（实测：commit 时序缺陷使游标每轮必然失效，增量从未跨轮生效）。新设计为
三个正交账本：

1. **`mirror_seq`（消息序列号）**：每条镜像消息在 `Message.meta
   ["mirror_seq"]` 携带 chat 内单调递增的序列号。跨回合增量 = "镜像中
   seq > `synced_through_seq` 的条目"，与列表下标、system 头、出站视图
   裁剪全部解耦。所有重建出站视图的路径（`_append_history_async` /
   `context_manager` 裁剪 / `tool_visibility`）都已保证 meta 随拷贝保留。
2. **写入者台账（`append_batches`）**：每次镜像追加记录 `[seq..., writer]`，
   writer ∈ {`user`（用户输入，厂商无关）| `<vendor_key>`（该厂商模型产出）
   | `recovery`（打断/异常保全）| `server_sync`（回拉覆盖）| `unknown`。
   增量合法性要求水位之后条目的写入者全部 ∈ {`user`, 本厂商}。
3. **`structural_epoch`（结构纪元）**：镜像结构被替换的事件递增；ref 记录
   同步时的纪元，不匹配即作废。

回合内（单次 agentic 循环）的增量切片按**对象身份**追踪
（`_TurnSyncContext.sent_ids` / `local_assistant_ids`）：服务端响应产生的
assistant 消息已随响应自动进入会话，重发会造成重复条目，因此只有配对的
tool 结果作为增量发送。

### Fencing

`generation` 计数器只有 `/clear` 递增。回合结束时 `commit_vendor_sync`
校验 `turn.generation`——被 `/clear` 打断的 TIMER 回合迟到的结果会被拒绝，
绝不复活已作废的会话。回合中途异常 / 被打断时，桥接层作废本回合使用的
厂商会话（`turn.sync_ctx` + `_invalidate_on_interrupt`），避免服务端残留
半轮内容与本地镜像静默错位。

## 三、服务端压缩（Server-side Compaction）监听与同步

实现在 `src/server_compaction.py`：

1. **事件监听**：`detect_compaction_signal`（Streaming 事件 type 子串匹配，
   保守语义限定，避免把输出截断 `response.incomplete` 误判为历史压缩）+
   `detect_compaction_metadata`（`response.compaction` 专用字段 /
   `metadata` 键）。命中后只**登记**待同步事项（每厂商去重），绝不在
   流式中途抢占镜像。
2. **回拉覆盖**：回合收尾后（`maybe_spawn_server_sync` 派发）以
   `GET /v1/conversations/{conversation_id}`（SDK `conversations.items.list`，
   升序分页拉全量）拉取服务端最新真实消息列表。
3. **数据清洗（Adapter）**：`adapt_items_to_messages` 把专有 items 清洗为
   标准 `messages`：message → user/assistant 文本；function_call +
   function_call_output → 成组物化 `assistant(tool_calls)` + `tool(result)`
   （并行调用乱序输出安全；悬空调用/无主输出丢弃并计数）；reasoning 与
   未知形状跳过并计数（推理内部状态不入通用镜像）。
4. **并发加锁**：覆盖执行期间持有该 chat 的会话锁，ref 相位置 `SYNCING`；
   存在在途回合时自动让路并重新登记，由该回合收尾后再触发——同步完成前
   下一轮用户输入不能抢占写入本地镜像。覆盖保留镜像头部 system 摘要槽位、
   替换对话主体，重新发号并记 `server_sync` 台账，重新对齐本厂商水位，
   作废其他厂商 ref。
5. **兜底（需求 三.2）**：拉取失败（网络/超时/网关不支持/客户端缺失）或
   超限（页数/条目上限，`PullOverflowError`）时，本地镜像**原样保留**
   （单一事实来源不可破坏），作废该厂商会话进入分叉态，下一轮以本地
   全量上下文重建新会话。`_begin_turn_sync` 本身的异常也会降级为无状态
   全量请求，绝不阻断回合主流程。

## 四、跨厂商 / 跨模型切换流转（需求 二.3 汇总）

| 场景 | 行为 |
| --- | --- |
| 同厂商内切换模型（均支持 Responses API） | 保留 conversation_id，请求更新 model 参数，仅发增量 |
| 切至传统 Completions / Messages / Gemini 模型 | 传统适配器全量请求本地完整上下文；输出完整追加进本地镜像；`mark_legacy_divergence` 作废全部 Responses 会话（双保险：写入者台账同时记录传统写入者） |
| 从传统模型切回 Responses API | 传统问答尚未在云端登记 ⇒ plan 判定 `foreign_writer` 分叉 ⇒ 作废旧 id，本地全量上下文（含传统模型回复）自举新会话 |
| `/clear` | 清空本地消息队列 + 清理全部厂商 conversation_id 绑定（尽力删除服务端会话对象，可关）+ 账本归零 + generation++ |

## 五、配置项

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `RESPONSES_STATEFUL_CONVERSATION_ENABLED` | `true` | 总开关：关闭后 Responses 路径回退"每轮全量重发" |
| `RESPONSES_SYNC_MAX_PAGES` / `RESPONSES_SYNC_PAGE_LIMIT` | 40 / 100 | 回拉分页上限（超限保守作废重建） |
| `RESPONSES_SYNC_MAX_ITEMS` | 2000 | 回拉清洗后镜像条目上限（保留最近 N 条） |
| `RESPONSES_SYNC_TIMEOUT` | 15.0 | 单次 items 拉取 / 删除超时（秒） |
| `RESPONSES_COMPACTION_EVENT_PATTERNS` | 空 | 追加网关自定义压缩事件名（逗号分隔） |
| `RESPONSES_DELETE_CONVERSATION_ON_CLEAR` | `true` | /clear 时尽力删除服务端会话对象（失败静默） |

## 六、相关测试

- `tests/unit/test_conversation_sync.py`：状态机（序列号/台账/分叉判定/
  fencing//clear）+ Adapter 数据清洗 + 压缩检测 + 回拉覆盖与兜底。
- `tests/unit/test_responses_bridge_sync.py`：桥接接入点（跨轮增量、
  工具轮增量、本地压缩分叉、跨厂商切回分叉、系统提示变化分叉、中断作废、
  会话类 4xx 判定）。

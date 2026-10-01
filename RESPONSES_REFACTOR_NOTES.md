# OpenAI Responses 协议重构说明

本次重构针对 `openai_responses` 原生 `/v1/responses` 路径，把多轮状态
管理统一为**官方 server-managed state**（`previous_response_id` 单链头），
彻底移除旧的"水位 + 增量历史"混合机制。

## 主要变化（本次：server-managed state 统一）

- `responses_state.py` 重写：每个 chat 只保存一个链头
  `(vendor_key, model, previous_response_id)`；成功创建 response 后原子
  推进，请求失败保持旧链头。移除镜像账本（`mirror_seq`）、水位
  （`synced_through_seq`）、增量切片、写入者台账、`noop` 模式、
  `ResponseSyncPlan` 规划器与 `structural_epoch`。
- 普通轮次：`input` 只携带本回合新增的 user item；历史上下文由
  `previous_response_id` 承载。bootstrap（异常恢复）才全量重放本地
  canonical history。
- 工具轮次：从 `response.completed` 的权威结构化 `output` 识别
  `function_call`，生成配对的 `function_call_output`，以该 response 的
  id 作为 `previous_response_id` 续链。工具调用 response 完全可能没有
  最终文本——绝不以 `output_text == ""` 判定 response 为空。
- 空 input invariant：调用 SDK 前校验，空 input 抛
  `ResponsesProtocolError`（新增于 `ai/errors.py`），绝不发送
  `input: []`，也绝不伪造消息。
- `instructions` 每轮从当前 canonical system prompt 显式传入
  （官方语义：`previous_response_id` 不继承上一响应的 instructions）。
- previous_response_id 失效恢复：仅当错误明确指向 previous response
  不存在/不可用时，丢弃链头并用 Responses API 全量 bootstrap 重试一次；
  普通 4xx 不触发。不存在 Chat Completions / Conversations fallback。
- 重启恢复能力保留但只恢复 ID：`export_chain_state` /
  `restore_chain_state`（chat_id / model / previous_response_id）。
- 跨协议回合（openai_chat / anthropic / gemini）与本地压缩淘汰显式断链
  （`mark_legacy_divergence` / `invalidate_response_chain`），下次
  Responses 回合从本地全量 bootstrap。
- `response.output` 仍是 Response output 的权威来源；流式 delta 只负责
  实时 UI。`response.failed` / `response.incomplete` / 顶层 `error` 不再
  驱动工具执行，也不会成为新的 `previous_response_id`。
- assistant `Message.meta` 中的原生 output item 快照保留（reasoning、
  `encrypted_content`、`phase`、annotations 等），仅用于 bootstrap 时的
  原样重放。

## 官方语义对应

该实现遵循 Responses API 的关键官方约定：

1. 多轮 Responses 通过 `previous_response_id` 续链；一个 conversation
   选择一种状态策略，不把本地 replay 和 server-managed state 混用。
2. 工具调用续轮沿同一 response 链提交新的 input（function_call 与
   function_call_output 通过 `call_id` 关联）。
3. 使用 `previous_response_id` 时 `instructions` 不自动继承，每轮显式传入。
4. Responses streaming 使用 typed event，而不是 Chat Completions 的
   `delta` envelope。
5. incomplete / failed response 在继续 tool loop 前先按终态处理，绝不
   成为续接点。
6. 无法解析 previous response ID 时重新提供完整 context，属于异常恢复
   路径而非正常工作模式。

## 验证

已通过：

- `tests/unit/test_responses_state.py`（重写：单链头 / 原子提交 /
  generation fencing / 模型切换断链 / 恢复 roundtrip）
- `tests/unit/test_responses_chain_bridge.py`（新增：普通多轮 bootstrap →
  commit → 只发新增 user item；function_call → function_call_output
  continuation；stale-ID 一次性 bootstrap 重试；失败不推进链头；空 input
  invariant；流中断断链）
- `tests/unit/test_response_events.py`、`tests/unit/test_response_protocol.py`、
  `tests/unit/test_responses_tool_continuation.py`（协议边界不变）
- 全套 `python -m pytest tests/unit`（691 passed；其中
  `test_bash_background.py::test_dispatch_tool_call_forwards_background_params`
  为沙箱环境 `preexec_fn` 限制导致的固有失败，与本次改动无关——原始
  压缩包同样失败）
- 全项目 `python -m compileall -q src tests`

完整 pytest 套件中的 `tests/integration` 需要安装全部运行依赖
（quart / aiohttp 等）；本环境未安装，与上一版本说明一致，未改动依赖锁。


## 网关兼容性补丁（2026-10）

部分 OpenAI-compatible `/v1/responses` 网关会在客户端收到合法的
`function_call_output` 后，在自身转换层把该 item 丢弃，最终以
``input must be non-empty`` 拒绝请求。该错误不是本地 `input=[]`：
bridge 已在 SDK 调用前验证 input 非空。

现增加按“单次工具续轮”计数、且每个续轮最多一次的受限恢复：仅当“当前请求确实是
function_call_output continuation + 已有 previous_response_id + HTTP 400
明确为 input must be non-empty”时；恢复额度按每个工具续轮重新计算，放弃本次 server-managed chain，清除
pending continuation，改用 canonical history 通过 Responses bootstrap 重放
assistant function_call + function_call_output。普通 400、stale response、
本地空 input 均不触发该恢复，避免无限重试或错误掩盖。



## 工具续轮排查结论与修复（2026-10-01）

**结论：续轮请求本身符合官方规范，`input must be non-empty` 是网关对“链 + 孤立 function_call_output”的处理缺陷，不是本地空 input。**

- 线上请求体抓包（真实 `AsyncOpenAI` + `httpx2.MockTransport`）：续轮为
  `previous_response_id=<产生 function_call 的 response.id>` + `input=[{"type":"function_call_output","call_id":...,"output":"..."}]`
  + `instructions/tools/store`，与官方 function calling 续轮形状一致；`call_id` 与 function_call 配对。
- 生产日志证据：3 个工具续轮 3/3 被拒（每次约 2.5–3.3s），随后的 bootstrap（无 `previous_response_id`，
  input 含 function_call + 同一条 function_call_output）3/3 成功。确定性、与内容无关；而普通文本回合带
  `previous_response_id` 正常（200）。
- 直接证据请在能访问网关的环境运行 `scripts/probe_responses_gateway.py`（A/B/C 三组对照）。

本次代码修复：
1. 按 `(vendor_key, model)` 记住“链式工具续轮被拒”（`responses_state.mark_tool_continuation_chain_unsupported`）。
   首次被拒后，后续工具续轮直接 bootstrap，不再每轮白发一个必败请求（实测日志每轮多 ~3s 延迟与一次多余调用）。
2. 修复真实 bug：流缺终态事件分支调用 `invalidate_response_chain` 多传了一个参数，
   会抛 `TypeError` 掩盖 `AIResponseProtocolError` 且链不会被作废；已修并加回归测试。
3. 测试：旧的“每个工具续轮都重试 400”契约改为“学习一次后跳过”；新增流中断回归测试。

4. 诊断补强：工具续轮 400 时额外记录网关原始错误体与请求形状（previous_response_id、input item 类型、
   call_id、output 长度、顶层字段），不含用户内容。下次生产复现时日志里的
   `工具续轮 400 诊断` 一行即可判定：call_id 是否配对、output 是否为空、错误是否来自网关上游。

**关于“是不是代码问题”的边界说明**：续轮请求形状与 OpenAI / xAI 官方文档一致（`previous_response_id` + 仅含
`function_call_output` 的 `input`），静态核查未发现偏离；但没有在 lfree 网关上实测，不能 100% 排除网关特有要求。

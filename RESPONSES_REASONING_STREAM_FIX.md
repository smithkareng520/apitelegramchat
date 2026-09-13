# Responses API 推理内容流式延迟问题

## 现象

用户反馈：同一模型、同一端点，走 OpenAI Responses API
（`client.responses.create(..., stream=True)`）时，感觉"思考字段和
文本字段全部回来后才推送到草稿"，有明显延迟；诊断脚本显示上游确实
持续在推送 `response.reasoning_summary_text.delta` 事件（每条间隔
几毫秒到十几毫秒），说明上游/网关的流式转发本身没有问题。

## 根因

**位置**：`src/ai/responses_bridge.py`，`_agentic_loop_openai_responses`
的事件分发循环。

修改前的事件分支里，`response.output_text.delta`（正文）有正确处理：
每收到一块就立刻 `builder.append_stream_delta(text)` 推送草稿。

但 `response.reasoning_summary_text.delta`（推理摘要的增量）**完全没有
对应分支**，会被 `if/elif` 链隐式忽略。推理内容实际的推送时机在
`response.output_item.done`（`type == "reasoning"`）里，从 `item.summary`
读出**整段**文字一次性推送——也就是说，不管上游发了多少条 delta，
这段代码都只在"这一段思考彻底结束"时才把内容显示出来。

这跟同一文件里 `anthropic_bridge.py` 对 Anthropic 协议 `thinking_delta`
事件的处理方式不对称——`thinking_delta` 是逐块累加、逐块推送的，
`reasoning_summary_text.delta` 却被漏掉了。两条原生协议桥接在"设计
原则"（模块头注释）里声明要与彼此同构，这里出现了一处遗漏。

结果就是：正文流式是正常的，但只要模型有思考过程（Opus/GPT-5 系列
默认都会有），推理阶段会表现为"卡住不动，直到整段思考完才刷新"，
思考越长这个"假死"观感越明显——这正是用户描述的"等响应完全回来
才推送"的体验来源（严格说是"思考完才推"，文本本身没问题，只是
思考通常排在文本前面，掩盖了文本流式其实正常的事实）。

## 修改

新增 `response.reasoning_summary_text.delta` 分支，逻辑与
`response.output_text.delta` 完全对称：累加进 `reasoning_acc`、
`switch_stream("reasoning")`、`builder.append_stream_delta(text)`、
`live_slot.sync(...)`——收到一块就推一块。

为避免同一段内容被 delta 推送一次、又被 `.output_item.done` 的整段
兜底逻辑重复推送一次，引入了 `reasoning_seen_via_delta` 标记：

- 每个 reasoning item 在 `response.output_item.added` 时重置为
  `False`（一轮响应内可能有多个 reasoning item，例如工具调用前后
  各思考一次，标记按 item 生命周期独立判断，不跨 item 误判）；
- 收到 delta 时置为 `True`；
- `.output_item.done` 里：如果标记为 `True`，说明这段内容已经逐块
  推送过，不再重复处理；如果为 `False`（某些网关只在 `.done` 给出
  完整 summary、完全不发 delta 的兼容情况），退回原来的整段推送
  逻辑作为兜底，保证内容不丢。

这个"delta 优先、`.done` 兜底"的模式与文件里 `function_call` 参数的
既有处理方式（`response.function_call_arguments.delta` 累积、
`.done` 权威覆盖兜底）是一致的写法，没有引入新模式。

## 验证

本地沙箱环境因缺少 `httpx`/`anthropic`/`mcp` 等重型依赖，无法完整
`import` 该模块跑集成测试（这是深度依赖真实服务集成的生产项目，
import 链会一路牵扯到几乎全部子系统）。改为把改动的事件分发逻辑
原样抽取到独立脚本中，用假事件序列验证三个场景，全部通过：

1. 正常场景（本次要修的问题）：reasoning delta 逐块到达 → 逐块
   推送；`.done` 事件不重复推送整段。
2. 兜底场景：网关不发 delta、只在 `.done` 给出完整 summary → 仍能
   拿到完整内容，不因改动而丢失信息。
3. 多 reasoning item 场景：一轮响应内出现多个 reasoning item →
   delta 标记按 item 生命周期正确重置，互不干扰。

建议合入后用真实网关跑一次此前的诊断脚本复核：预期能看到
`response.reasoning_summary_text.delta` 事件到达的同时，客户端侧的
草稿/UI 也在同步逐字增长，而不是等到该 reasoning item 结束才整段
刷新。

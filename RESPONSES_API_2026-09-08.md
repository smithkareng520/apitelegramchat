# 新增协议：OpenAI 原生 Responses API（`/v1/responses`）

## 背景

`config.py` 里 `gpt-5.6-sol`（XXTF 中转）此前带一段风险说明：该模型
在平台上标注的入口是 `/v1/responses`（OpenAI 较新的 Responses API），
但项目当时只有 Chat Completions（`/v1/chat/completions`）循环，只能
先按 Chat Completions 协议接入并注明"未验证、可能 404/400"的风险。

Responses API 与 Chat Completions 虽同属 OpenAI，但线上协议形状完全
不同：请求字段（`input` vs `messages`）、工具调用结果的配对方式
（`function_call_output` item + `call_id` vs `role:"tool"` 消息）、
工具 schema 形状（扁平 vs 嵌套 `function`）、推理参数位置
（顶层 `reasoning.effort` vs `reasoning_effort`）、流式事件模型
（强类型判别事件 vs `choices[0].delta` 增量）均不同。直接复用 Chat
Completions 循环会在这些差异点上全部出错。

## 修复方案

按项目已有的"原生协议桥接"惯例（`anthropic_bridge.py` /
`gemini_bridge.py` 的边界转换模式）新增一条专用循环，而不是往
`_agentic_loop_openai_compat` 里加分支：

### 新增文件

- **`src/ai/responses_bridge.py`**：Responses API 专用 agentic 循环。
  - `_convert_messages_to_responses_input()`：内部 `Message` 列表 ->
    `(instructions, input_items)`；system 消息拼进顶层 `instructions`，
    assistant 的 `ToolCallBlock` 转 `function_call` item，`tool` 消息
    转 `function_call_output` item（按 `call_id` 与产生它的
    `function_call` item 配对）。
  - `_convert_tools_to_responses()`：Chat Completions 嵌套工具 schema
    ->  Responses 扁平工具 schema。
  - `_agentic_loop_openai_responses()`：消费 `response.output_text.delta`
    / `response.output_item.added|done`（function_call 与 reasoning
    item）/ `response.function_call_arguments.delta|done` /
    `response.completed` / `response.failed|incomplete` / `error`
    等流式事件；把本轮 function_call 累积转换回 OpenAI Chat
    Completions 形状的 `tool_calls`，复用现有的
    `tool_call_loop._run_tool_calls_and_append` 执行工具、
    `ai/bridge_common.py` 的回合骨架（草稿流切换、超限强制总结、
    终局收束）。
  - `_responses_usage_to_openai()`：`ResponseUsage` -> Chat
    Completions 形状的 usage dict，接入既有的 prompt cache 命中率
    观测（`ai/cache_usage.py`）。
  - `openai_responses_chat_completions_create()`：非流式一次性调用
    封装，返回值模拟 `resp.choices[0].message`，供
    `subagent_tool.py` 复用（与 `anthropic_chat_completions_create`
    同一模式）。
  - 与其它两条原生桥接完全同构的关键约束：本文件收到的 `messages`
    参数、追加进 `new_history_entries` 的内容，全部是内部 `Message`
    /（等价的）OpenAI 形状——绝不把 Responses 原生 item 形状写回共享
    历史，保证用户随时切换模型/厂商时历史仍能被其它协议正确解读。

- **`src/protocols/openai_responses.py`**：协议适配器
  （`ChatProtocolAdapter` 实现），取模型对应的 `AsyncOpenAI` 客户端
  （Responses API 复用同一个 OpenAI SDK 客户端，无需新增原生 SDK
  依赖），转发进 `_agentic_loop_openai_responses`。

### 改动的既有文件

- **`src/config.py`**
  - `_VALID_PROTOCOLS` 新增 `"openai_responses"`。
  - `gpt-5.6-sol` 的 `protocol` 从隐式 `openai_chat`（风险接入）改为
    显式 `"openai_responses"`；**不覆盖 `base_url`**，沿用
    `PROVIDERS["xxtf"].base_url = "https://xxtf.baby/v1"`——
    `AsyncOpenAI` SDK 默认 `base_url` 本身就带 `/v1`
    （`https://api.openai.com/v1`），不会像 `AsyncAnthropic` 那样自动
    拼接 `/v1`，因此这里必须显式带上 `/v1` 前缀，实际请求会打到
    `https://xxtf.baby/v1/responses`。
  - 移除原来的风险警告注释，替换为准确的协议说明。

- **`src/protocols/registry.py`**：`CHAT_PROTOCOLS` 注册
  `"openai_responses": OpenAIResponsesAdapter()`。

- **`src/subagent_tool.py`**：`_create_chat_completion` 补一个协议
  分支，`protocol == "openai_responses"` 时调用
  `openai_responses_chat_completions_create`（此前会直接落到
  `client.chat.completions.create`，对 Responses 端点必然出错）。

## 验证

- 全量单测（`tests/unit` + `tests/integration`）：158 项通过，0 项因
  本次改动失败（沙箱环境下 `tiktoken` BPE 文件下载被出站白名单拦截
  导致的若干失败，在未改动的原始压缩包上同样复现，与本次改动无关）。
- 手写端到端联调（fake `client.responses.create` 模拟两轮流式事件：
  第一轮产出 `function_call`，第二轮产出最终文本）：验证了
  - 请求体形状正确（`input` / `instructions` / 扁平 `tools` /
    顶层 `reasoning.effort`）；
  - 流式事件正确驱动草稿 UI（工具卡片上屏、参数增量、文本流）与真实
    的 `_run_tool_calls_and_append` 工具执行链路；
  - 第二轮请求正确把上一轮的 `function_call` + `function_call_output`
    重放进 `input`，`call_id` 全链路一致；
  - 落回共享历史的消息是标准 Chat Completions 形状
    （`role:"assistant"` 带 `tool_calls` / `role:"tool"` /
    `role:"assistant"` 纯文本），与另外三条协议完全同构。

# Responses API `conversation` stateful implementation

本版本把 OpenAI Responses 的显式 `conversation` 接入现有项目，同时保持
`conversation_history` 为跨厂商 canonical history。

## 行为

- 首次使用 Responses：懒创建 `/v1/conversations`，把当前 canonical history + 本轮输入 bootstrap 到该 conversation。
- 同一 Responses conversation 的后续 USER 回合：只发送最新 user input；system instructions 每次请求重新发送，因为它们不是 conversation item。
- 同一 Agent 回合的后续 tool loop：只发送新产生的 `function_call_output`；上一轮的 assistant `function_call` 已由 Responses conversation 保存。
- USER / TIMER 共用同一个 chat-level conversation ID；TIMER 的临时唤醒指令走 `instructions`，不写入 canonical history。
- 如果切换到 Chat/Anthropic/Gemini 等其他 provider 后 canonical history 发生变化，再切回 Responses：旧 Responses conversation 被放弃并重新 bootstrap，避免两个历史分支错误合并。
- `/clear` 同时清空 canonical history、Responses conversation ID 和同步指纹，并轮换现有 LLM session token。
- 如果上游兼容网关没有 `conversations.create`，自动回退到原有的全量 `input` 模式，不影响基本可用性。

## 状态字段

存放在每个 chat 的 `user_contexts[chat_id]`：

- `openai_responses_conversation_id`
- `openai_responses_canonical_fingerprint`

fingerprint 只用于判断 provider-side conversation 是否仍与项目 canonical
history 同步，不承担业务历史存储职责。

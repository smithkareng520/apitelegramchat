# Responses reasoning content fix — 2026-09-09

问题：GPT-5.6 Sol 使用 Responses API 时，Telegram/内部消息只有普通文本和工具，没有可展开的 reasoning 内容。

原因：Responses streaming 的 reasoning summary 不是只通过 `response.output_item.done` 返回。当前 API 会发送独立的 `response.reasoning_summary_text.delta` / `response.reasoning_summary_text.done` 事件；原桥接没有消费这些事件，因此 `reasoning_acc` 经常保持为空。

修复：
- 请求参数在 `reasoning` 下明确加入 `summary: "auto"`，主动 opt-in reasoning summary。
- 消费 `response.reasoning_summary_text.delta`，实时写入 `ReasoningBlock` / DraftManager reasoning 流。
- 消费 `response.reasoning_summary_text.done`，兼容只在 done 事件提供完整文本的网关。
- `response.output_item.done` 仍保留作为兼容兜底，并避免和 delta 重复。
- 非流式 Responses 兼容入口同样默认请求 `reasoning.summary="auto"`。

注意：OpenAI 不提供原始隐藏 chain-of-thought 给客户端；这里展示的是 API 提供的 reasoning summary，而不是内部原始思维链。官方文档说明 reasoning summary 需要显式 opt-in，并以 reasoning output item / summary 事件返回。

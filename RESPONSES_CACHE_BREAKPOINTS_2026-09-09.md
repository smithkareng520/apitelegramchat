# GPT-5.6 Sol Responses 缓存断点策略（2026-09-09）

本版把 Responses API 的缓存结构硬编码为：

1. 前部固定断点：输入中第一个可用 `input_text` content block。
2. 尾部两个滚动断点：输入中最后两个可用 `input_text` content block。
3. 保留一个 implicit 自动断点：请求使用 `prompt_cache_options.mode = "implicit"`，而不是 `explicit`。

因此通常形成 **3 个显式 + 1 个自动**，但如果当前请求可标记的文本 block 少于 3 个，则实际显式断点数会相应减少，永远不会超过 Responses API 的每请求 4 个 breakpoint 上限。

`prompt_cache_key` 继续与项目的 Telegram `session_id` 使用完全相同的字符串和生命周期。

## TTL

当前 Responses API 对 GPT-5.6 及以后模型的 `prompt_cache_options.ttl` 只有 `30m` 这一档。因此不能像 Anthropic 的 `cache_control` 那样，在同一个请求里给“第一个显式断点 1h、后两个断点 5m”分别设置不同 TTL。

本实现通过“前部固定、尾部滚动”的位置策略复刻旧显式缓存层级，同时保留自动断点；TTL 统一为：

```json
{
  "mode": "implicit",
  "ttl": "30m"
}
```

# Responses API Prompt Cache Fix — 2026-09-09

本次修复针对 `gpt-5.6-sol` 的 `/v1/responses` 链路：

- XXTF 的 `gpt-5.6-sol` 现在启用 `supports_prompt_cache=True`。
- 每个 Telegram 会话都会生成稳定、<=64 字符的 `prompt_cache_key`，所有 Responses 主循环与 over-limit 合成请求复用同一个 key。
- 继续按 Responses API 原生 usage 读取 `usage.input_tokens_details.cached_tokens`，并保留 `cached_tokens=0` 的显式上报。
- 保持现有 `ai.cache_usage` 日志格式，因此命中时会重新出现：
  `[... ] prompt cache usage: {'Provider': '...', 'Model': '...', 'Input_tokens': ..., 'Output_tokens': ..., 'Cached': ..., 'Hit_ratio': ...}`
- OpenAI Python SDK 从原来的 `1.66.3` 升级到 `2.54.0`，该版本已包含 Responses 的 `prompt_cache_key` / `prompt_cache_options` 请求定义，同时继续使用项目当前的经典 `httpx` 路径，减少 3.x HTTPX2 迁移风险。

注意：真正的 `Cached` 数值只能由 `/v1/responses` 上游/中转返回。客户端可以正确发起缓存请求并正确解析 usage，但如果 XXTF 本身没有把缓存统计字段透传回来，日志仍无法凭空得到命中数。

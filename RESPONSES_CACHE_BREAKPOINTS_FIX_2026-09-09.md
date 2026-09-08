# Responses API breakpoint 修复与可选策略 — 2026-09-09

## 兼容性问题

内容块上的 `prompt_cache_breakpoint` 必须使用对象形式，而不是布尔值：

```json
"prompt_cache_breakpoint": {
  "mode": "explicit"
}
```

## 当前策略

项目现在默认只使用 Responses API 的自动缓存：

```json
"prompt_cache_options": {
  "mode": "implicit",
  "ttl": "30m"
}
```

默认不会向内容块写入 `prompt_cache_breakpoint`，以兼容只支持自动缓存的中转服务。

如需启用原有显式策略，设置环境变量：

```text
RESPONSES_EXPLICIT_CACHE_ENABLED=true
```

开启后，最多添加 3 个显式断点，同时保留 1 个自动断点，即 **自动 1 个 + 手动最多 3 个**。可用文本块少于 3 个时，显式断点数量会相应减少。

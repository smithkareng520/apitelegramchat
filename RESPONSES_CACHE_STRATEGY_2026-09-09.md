# Responses API 缓存策略 — 2026-09-09

## 默认行为

项目默认使用供应商的自动缓存，不在 Responses API 内容块上添加 `prompt_cache_breakpoint`。请求仍发送稳定的 `prompt_cache_key`，并发送：

```json
{
  "prompt_cache_options": {
    "mode": "implicit",
    "ttl": "30m"
  }
}
```

这样只支持自动缓存的中转也可以正常工作。

## 手动显式断点

如需恢复原来的手动策略，在运行环境设置：

```text
RESPONSES_EXPLICIT_CACHE_ENABLED=true
```

开启后，每次 Responses 请求会在最多 3 个文本内容块上添加：

```json
"prompt_cache_breakpoint": {
  "mode": "explicit"
}
```

同时保留 `prompt_cache_options.mode = "implicit"`，因此目标结构是 **自动 1 个 + 手动最多 3 个**。可标记文本块少于 3 个时，显式断点数量会相应减少。

## 兼容性策略

该开关只控制 Responses API 的内容块显式断点，不影响 Anthropic 的原生 `cache_control` 策略，也不把 Responses 专用字段发送到 Chat Completions、Gemini 或其他协议。默认关闭是为了兼容尚未实现显式断点的中转服务；确认中转支持 GPT-5.6 Responses 显式缓存后再开启即可。

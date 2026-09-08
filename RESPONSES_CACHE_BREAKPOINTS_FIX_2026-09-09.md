# Responses API breakpoint 修复 — 2026-09-09

## 问题

上一版把内容块上的 `prompt_cache_breakpoint` 序列化成了布尔值：

```json
"prompt_cache_breakpoint": true
```

GPT-5.6 Responses API 当前要求它是对象，因此 xxtf 返回：

```text
Invalid type for 'input[0].content[0].prompt_cache_breakpoint': expected an object, but got a boolean instead.
```

## 修复

现在改为：

```json
"prompt_cache_breakpoint": {
  "mode": "explicit"
}
```

请求级别仍然保持：

```json
"prompt_cache_options": {
  "mode": "implicit",
  "ttl": "30m"
}
```

因此仍然是项目目标的结构：3 个显式 breakpoint + 1 个 implicit 自动 breakpoint。

OpenAI 当前 Responses API 文档说明，GPT-5.6 及以后支持在 content block 上添加 `prompt_cache_breakpoint`，每次请求最多写 4 个 breakpoint；保持 `mode="implicit"` 时会额外保留 1 个自动 breakpoint。当前 `ttl` 支持 `30m`。

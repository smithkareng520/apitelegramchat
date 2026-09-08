# Responses API 缓存参数自动降级修复 — 2026-09-09

## 问题（线上报错）

```
RuntimeError: [xxtf] Responses API error: prompt_cache_breakpoint is not supported on this model
```

Telegram 侧表现为「⚠️ GPT 5.6 Sol (XXTF) 请求失败 … 详情：内部错误」，用户回合中断。

## 根因

上一版给 `gpt-5.6-sol` 的 Responses 请求硬编码注入缓存结构：

1. content block 上的 `prompt_cache_breakpoint: {"mode": "explicit"}`（前 1 + 尾 2 共 3 个显式断点）；
2. 请求级 `prompt_cache_options: {"mode": "implicit", "ttl": "30m"}`；
3. 请求级 `prompt_cache_key`（与 Telegram session_id 同源）。

但 xxtf 网关的 `gpt-5.6-sol` 上游**不支持 content block 上的 `prompt_cache_breakpoint` 字段**——流式请求先返回 HTTP 200，随后以 SSE `error` 事件报错，桥接层命中「零输出即失败」分支直接抛出，整轮失败。

静态配置（`_PROVIDER_DEFAULTS["xxtf"].supports_prompt_cache=True`）只能表达「该厂商设计上走缓存」，表达不了「网关当前具体接受**哪些**缓存字段」。同一模型名在不同网关 / 上游副本上，`prompt_cache_breakpoint` / `prompt_cache_options` / `prompt_cache_key` 的支持度可能各不相同，靠改配置项追着网关能力跑不可维护。

## 修复方案：运行时能力表 + 零输出自动降级重试

改动集中在 `src/ai/responses_bridge.py`：

1. **进程级缓存字段能力表** `_RESPONSES_CACHE_CAPABILITIES`
   - key 为 `模型名@网关base_url`，value 为 `{字段名: 是否支持}`；
   - 一旦网关明确拒绝某字段，记为 `False`，后续请求直接按降级后的参数集发送，不再浪费失败往返；
   - 初始状态全支持（与静态配置一致），只在网关「亲口拒绝」后才学习降级。

2. **错误文本识别** `_cache_field_unsupported_in_error()`
   - 从错误文本中识别被拒字段名（`not supported` / `unknown parameter` / `unrecognized` 等措辞）；
   - **只匹配「不支持 / 未识别」类错误**，形状类错误（如上一轮修过的 `expected an object, but got a boolean`）不识别为降级信号——那是代码 bug，应当修代码而不是静默绕过缓存能力。

3. **流式主循环（`_agentic_loop_openai_responses`）：降级重试**
   - 当且仅当**本轮零输出**（零文本 / 零思考 / 零工具调用，对用户界面零副作用）且错误指向缓存字段时，抛内部信号 `_ResponsesCacheFieldRejected`；
   - 轮次级 except 把该字段记入能力表并**原地重试同一轮**（不消耗 `MAX_TOOL_CALLS` 轮次预算），用户回合不中断；
   - 重试请求已剥离被拒字段（断点打标函数支持 `enabled=False` 剥离已打标记）；
   - 重试上界：每个字段至多触发一次，全程最多 3 次；
   - 已有部分输出时维持原行为（尽力保留已产出内容收尾，不重试），非缓存类错误维持原行为直接抛出。

4. **非流式路径（`openai_responses_chat_completions_create`，subagent 用）：同款降级**
   - SDK 直接抛 4xx 且错误指向缓存字段时，剥离该字段记入能力表并立即重试；
   - 该路径本就不挂 content block 断点，通常只涉及 `prompt_cache_options` / `prompt_cache_key`。

## 降级阶梯（对应当前线上错误）

```
第 1 次尝试：3 个显式断点 + prompt_cache_options(implicit, 30m) + prompt_cache_key
     ↓ 网关报 "prompt_cache_breakpoint is not supported on this model"
第 2 次尝试：无断点 + prompt_cache_options + prompt_cache_key   ← 本次用户回合在此成功
     ↓ 本进程后续请求：直接不发断点（能力表命中）
```

`prompt_cache_key` 是 OpenAI 长期稳定字段，保留意味着会话亲和缓存路由不丢；隐式自动缓存（`mode="implicit"`）与网关自动缓存继续生效，只是放弃显式断点。

## 行为不变项

- `prompt_cache_key` 与 Telegram `session_id` 的同源关系不变；
- usage 归一化与 `prompt cache usage` 命中日志不变；
- 零输出即失败的错误处理语义（非缓存错误）不变；
- 已有部分输出时的「尽力保留」语义不变。

## 测试

新增 `tests/unit/test_responses_cache_degrade.py`（12 个用例）：

- 错误文本识别（生产报错原文 / options / key / 形状错误不误判 / 无关错误）；
- 断点打标与剥离的幂等性；
- 流式主循环：SSE error 事件路径降级重试、SDK 异常路径降级重试、无关错误不重试不污染能力表、部分输出不重试、能力表学习后**首次请求即降级**；
- 非流式路径：`prompt_cache_options` 被拒后剥离重试、无关错误照常抛出。

回归：全量 pytest 通过（3 个失败用例为改动前即存在的环境性问题，与本次改动无关）。

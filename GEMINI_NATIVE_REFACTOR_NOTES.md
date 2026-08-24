# Gemini 原生 SSE 流式改造交付说明

## 改造目标

把 Gemini 专用循环从 `OpenAI 兼容端点（非流式）` 切换到 `原生 :streamGenerateContent?alt=sse` 流式，从而同时获得：

1. **思考增量推送**——`reasoning_content` 逐 token 显示，不再等整段返回；
2. **`thought_signature` 链跨轮保存**——OpenAI 兼容端点会剥离签名，原生端点保留完整的 `parts/thought/thoughtSignature` 结构，多轮思考链不再断；
3. **`thought` 布尔位**——精确区分"思考"与"可见正文"，渲染更准确。

## 文件改动清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/apitelegramchat/ai/gemini_native_protocol.py` | **新增** | OpenAI ↔ Gemini 原生格式双向转换器（约 280 行，含注释） |
| `src/apitelegramchat/ai/agentic_loops.py` | 修改 | ① 顶部新增 converter 导入；② 删除旧 `_agentic_loop_gemini_openai_compat` 实现（约 220 行）；③ 新增 `_agentic_loop_gemini_native` 流式实现（约 400 行）；④ 保留旧名作为别名，避免外部引用破坏 |
| `src/apitelegramchat/ai_handlers.py` | 修改 | import 与调用点从 `_agentic_loop_gemini_openai_compat` 改为 `_agentic_loop_gemini_native`（共 2 行） |
| `src/apitelegramchat/config.py` | 修改 | `gemini` provider 的 `base_url` 改为原生端点根路径；`use_dedicated_loop` 注释更新；`ProviderConfig.use_dedicated_loop` 注释更新 |

**未改动**：`RichMessageBuilder`、`tool_call_loop`、`tool_summary`、`utils.send_rich_message_draft`、`api_client`、`app.py` 等。草稿推送链路与工具执行链路完全复用 OpenAI‑compat 路径已建立的机制（`switch_stream` / `append_stream_delta` / `add_tool_item` / `request_flush` / `_run_tool_calls_and_append` / `rollover_at_turn_boundary`）。

## 鉴权与密钥

- **零密钥改动**：仍使用环境变量 `GEMINI_API_KEY`，仍走 `Authorization: Bearer ...`。
- 端点 URL 改为：
  ```
  POST https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse
  ```
- 请求头加 `Accept: text/event-stream` 让中间代理识别为 SSE。
- 非流式回退 URL：
  ```
  POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
  ```

## 关键设计点

### 1. `loop_messages` 仍以 OpenAI 格式维护
内部循环仍用 OpenAI 格式 `[{role, content, tool_calls, reasoning_content, _gemini_thought_signatures}]`，便于与 `_run_tool_calls_and_append`（OpenAI 风格的 role=tool 消息）共享。每轮请求前调 `openai_messages_to_gemini_contents(loop_messages)` 转成原生格式，幂等。

### 2. `thought_signature` 链保留
- 流式响应中每个 `thought:true` part 的 `thoughtSignature` 都被收集到 `gemini_state["thought_signatures"]`（按出现顺序）；
- 收尾时 `build_assistant_msg_from_gemini_state` 把签名列表挂在 assistant_msg 的 `_gemini_thought_signatures` 字段上；
- 下一轮请求时，`openai_messages_to_gemini_contents` 把签名列表的**最后一个**作为合成 `thought:true` part 的 `thoughtSignature` 字段，Gemini 即可校验上一轮思考链完整性。

### 3. Part 流式分发到 builder
对每个 SSE chunk 的 `parts` 数组逐项分发：
- `thought:true` + `text` → `switch_stream("reasoning")` + `append_stream_delta(text)`：思考增量推送
- `text`（无 thought 或 thought=false） → `switch_stream("content")` + `append_stream_delta(text)`：正文增量推送
- `functionCall` → `merge_gemini_part_into_state` 累积到 `tool_calls`，然后**立即** `add_tool_item` + `request_flush`：工具声明第一时间可见
- `thoughtSignature` 单独 part、`executableCode`、`codeExecutionResult` 等扩展 part 由 converter 统一兜底处理

### 4. 工具声明/结果推送时机与 OpenAI‑compat 路径一致
- 工具被 LLM 声明的瞬间 → `add_tool_item` + `request_flush(force=False)`：异步即时推
- 工具结果（done/error） → `update_tool_item` + `request_flush`：异步即时推
- 工具批次收尾 → `finish_group` + `await builder.flush()`：同步兜底
- 长工具（bash/子agent）的 `tool_progress_callback` → `flush(force=True)`：进度同步推

### 5. 非流式回退
若 SSE 流空内容（极端情况，如网络中断首片前），自动回退到 `:generateContent` 非流式端点，保证不丢失这一轮。

### 6. `thinkingConfig: {includeThoughts: true}`
请求体显式声明让 Gemini 把思考作为独立 `thought:true` part 返回，否则思考会被塞进普通 `text` part，无法区分。

## 验证

### 语法检查
```bash
python -m py_compile \
  src/apitelegramchat/ai/gemini_native_protocol.py \
  src/apitelegramchat/ai/agentic_loops.py \
  src/apitelegramchat/ai_handlers.py \
  src/apitelegramchat/config.py
```
全部通过。

### 转换器单元验证
`scripts/verify_gemini_native_protocol.py`（独立运行，不依赖 openai/aiohttp SDK），覆盖 47 个测试点：

1. OpenAI messages → Gemini contents（system 合并、user 映射、assistant 含 thought/text/functionCall 重建）
2. `thought_signature` 链：assistant_msg → 原生 parts → 还原 → 双向幂等
3. `role=tool` → `functionResponse`（JSON 解析、非 JSON 兜底）
4. 多模态：`data:image/png;base64,XXX` → `inlineData`；HTTP URL 退化为文本占位
5. 工具转换：OpenAI tools → Gemini `functionDeclarations`，`$schema` 自动剥离
6. `executableCode` / `codeExecutionResult` 退化为正文文本，不丢失内容

```bash
python /home/z/my-project/scripts/verify_gemini_native_protocol.py
# === RESULT: 47 passed, 0 failed ===
```

## 真实联调待做

下列项只能用真 GEMINI_API_KEY 联调确认，本地静态测试无法覆盖：

1. **首次 SSE chunk 的真实字段顺序**：Google 文档可能未明说 `usageMetadata` 是单独一帧还是嵌在最后一帧。代码已对两种情况都做了处理。
2. **`thoughtSignature` 在 SSE 流中的实际位置**：可能在每个 thought part 内（已覆盖），也可能作为单独 part 出现（已覆盖），也可能在最终 `finishReason` chunk 出现（converter 已收集，但需要日志确认数量）。
3. **某些 Gemini 模型可能不接受 `thinkingConfig`**：极老的模型 ID（如 `gemini-1.5-pro`）不支持思考，会报 400。代码已捕获并把异常冒泡到 `ai_handlers` 的标准错误处理路径。
4. **`toolConfig.functionCallingConfig.mode = "AUTO"`** 是否所有 Gemini 模型都接受。如不接受，可以删掉这一行（默认就是 AUTO）。
5. **`functionCall` args 是否会跨多个 SSE chunk 分片到达**：Google 文档显示是完整到达，但若实测发现分片，需要在 `merge_gemini_part_into_state` 里加上 args 累积逻辑。

## 回滚方案

如果改造出现不可恢复的线上问题，回滚步骤：

1. 把 `_agentic_loop_gemini_native` 函数体替换为旧的 OpenAI‑compat 非流式实现（保留在 git 历史里）；
2. 在 `ai_handlers.py` 把 `_agentic_loop_gemini_native` 改回 `_agentic_loop_gemini_openai_compat`；
3. `config.py` 的 `gemini.base_url` 改回 `https://generativelanguage.googleapis.com/v1beta/openai/`。

新文件 `ai/gemini_native_protocol.py` 可以直接删除（旧实现不依赖它）。

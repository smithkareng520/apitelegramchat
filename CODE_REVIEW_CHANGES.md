# 代码审查报告 & 本轮修改说明

审查范围：AI 请求/响应处理链路（`src/ai_handlers.py`、`src/ai/*.py`、
`src/protocols/*.py`），对照用户提出的七个维度逐项排查。

## 整体结论

架构本身是健康的：四协议路由（OpenAI 兼容 / Anthropic 原生 / Gemini 原生 /
Responses）用适配器模式统一分发，避免了 `if provider == xxx` 式硬编码；
`bridge_common.py` 把 Anthropic/Gemini 两条原生循环里本会逐字重复的骨架
（草稿流状态机、超限总结、终局收束）真正抽成了共享实现；大量注释是
"生产事故驱动"的记录（写明 bug 根因、修复日期、不这样做的后果），
属于成熟项目的标志，不是啰嗦。

以下是本轮**已修改**的问题，以及**已定位但未改动**（需要你决策或涉及
真实 API 容错语义、风险较高）的问题。

---

## 已修改

### 1. 修复：熔断状态借用 `builder` 对象做 `setattr`/`getattr`/`vars()` 反射存储

**位置**：`src/ai/tool_call_loop.py`

**问题**：工具连续相同错误的熔断计数，此前用动态属性名模拟字典：
```python
key = f"_streak:{error_msgs[0]}"
prev = getattr(builder, key, 0)
setattr(builder, key, curr)
...
for attr in list(vars(builder).keys()):
    if attr.startswith("_streak:"):
        delattr(builder, attr)
```
这段状态本质是"本轮工具循环"的临时数据，却被塞进了 `DraftManager`
（一个只负责 UI 草稿渲染的对象）身上，靠字符串前缀 + 反射管理生命周期，
违反了 `DraftManager` 自身文档写明的职责边界。

**修改**：改为显式的 `error_streak: dict` 参数，从各 bridge 的
`BridgeLoopState`（Anthropic/Gemini）或本地字典（OpenAI 兼容路径）
一路显式传递到 `bridge_common.run_tool_batch` 再到
`tool_call_loop._run_tool_calls_and_append`。省略该参数时函数内部
退化为一次性局部字典，不会报错，但正常路径应始终显式传入。

**改动文件**：
- `src/ai/bridge_common.py`：`BridgeLoopState` 新增 `error_streak: dict`
  字段；`run_tool_batch` 新增同名参数并透传
- `src/ai/tool_call_loop.py`：`_run_tool_calls_and_append` 签名新增
  `error_streak` 参数，核心逻辑改为普通字典读写
- `src/ai/anthropic_bridge.py` / `src/ai/gemini_bridge.py`：调用处传入
  `state.error_streak`
- `src/ai/agentic_loops.py`：新增本地 `error_streak: dict = {}`，
  `_agentic_loop_openai_compat` 调用处传入

**新增测试**：`tests/unit/test_tool_error_streak.py`（3 个用例：熔断触发、
成功后清零、省略参数时的默认行为；并显式断言 `builder` 实例不再出现任何
`_streak:` 前缀属性）。

---

### 2. 拆分：子 agent 进度渲染逻辑独立成模块

**位置**：`src/ai/tool_call_loop.py` → 新增 `src/ai/subagent_progress.py`

**问题**：`tool_call_loop.py` 文档声称的职责是"并行执行工具调用"，但
前 130 行左右其实是子 agent 状态文案的正则解析/HTML 渲染——一个独立的
表现层关切，和工具调用编排耦合在同一文件里，修改任一部分都要跨越
不相关的上下文。

**修改**：将 `_subagent_progress_phase` / `_format_subagent_progress_html`
及其配套正则整体移至 `src/ai/subagent_progress.py`，`tool_call_loop.py`
中改为导入（保留原函数名做内部重导出，不影响该文件其余代码）。

**新增测试**：`tests/unit/test_subagent_progress.py`（13 个用例，覆盖
全部阶段判定分支、工具名折叠、数字字段提取、未识别文本兜底）。

---

### 3. 安全修复：子 agent 状态文案中的自由文本字段未转义即嵌入 Rich Message HTML

**位置**：`src/ai/subagent_progress.py`（原属 `tool_call_loop.py`）

这是在为上一项拆分补测试时发现的：**测试本身（不是我事先设想的审查项）
直接命中了一个真实的转义缺口**。

**问题**：`format_subagent_progress_html` 此前用
`convert_markdown_to_telegram_html`（即 `markdown_converter
.render_telegram_fragment`）来"转义"模型名、工具名等自由文本字段。
但该函数的真实行为是：
```python
if not _contains_markdown(text):
    # 没有 Markdown 时，仍然返回现有 HTML/纯文本原貌。
    return text
```
——**只有检测到 Markdown 语法时才会走到内部的转义分支**；纯文本（包括
裸 `<script>alert(1)</script>` 这类内容）会被判定为"没有 Markdown"而
**原样直通，不做任何转义**。也就是说该函数名字和文档字符串暗示的
"Markdown 转换 + HTML 转义"并不是无条件成立的。

**触发路径**：`subagent_tool.py` 里 `tool_names = [tc_entry["function"]
["name"] ...]` 取自模型选择调用的工具名，正常情况下工具名是固定注册表
里的简单标识符（`fetch_url`、`str_replace`），风险较低；但一旦有工具名
（例如未来接入的 MCP 动态工具）恰好含 `<`/`>` 等字符，会直接进入
Telegram Rich Message HTML 而不被转义。

**修改**：自由文本字段（模型名、工具名、未结构化兜底文本）改用
`html.escape`；数字字段（轮次、耗时——均来自 `\d+`/`[0-9.]+` 正则捕获，
天然不含 HTML 元字符）直接使用，不再套用任何转义函数（原先调用
`convert_markdown_to_telegram_html` 对纯数字字符串恒为直通，属于纯粹的
无意义开销，顺带清理）。

**范围说明**：这是本模块内的局部修复，不是对
`markdown_converter.convert_markdown_to_telegram_html` 的行为改动——
后者被全项目几十处调用，且很多调用方本就依赖"已是合法 HTML 的片段原样
透传"这一行为（例如 `render_telegram_fragment` 自己文档举的
`f"<b>轮次</b>：{...}"` 场景）。**贸然让它无条件转义可能导致其他调用方
的合法 HTML 被双重转义**，属于影响面大、需要单独评估的改动，这次没有
动。如果你希望系统性核查其他调用方是否也有类似的自由文本未转义风险，
我可以下一轮专门排查。

**测试覆盖**：`test_subagent_progress.py::test_format_escapes_model_
controlled_text` 直接断言 `<script>` 不会原样出现在渲染结果中。

---

## 已定位但未改动（需要你决策）

### 4. Gemini 原生桥接缺少瞬时故障重试

`anthropic_bridge.py` 有 `_is_retryable_stream_error`，对流式请求中途
遇到 503/529 过载错误、且尚未产出任何内容时会重试；`gemini_bridge.py`
的对等循环没有这层保护，上游瞬时故障会直接让整轮对话失败。

**没有改动的原因**：补齐这层重试涉及真实的 API 容错语义（哪些错误码
可重试、要不要读 `Retry-After`、重试上限和退避策略），错误的重试策略
可能比没有重试更危险（例如对非幂等操作重复计费、或掩盖了应该让用户
感知的持续性故障）。如果你确认要加，我可以照抄 Anthropic 侧已验证
的模式移植过去。

### 5. 三套独立实现的 prompt cache 断点算法

`attachment_content._apply_cache_control`（OpenAI 兼容协议）、
`anthropic_bridge.py` 内联实现、`responses_bridge.py` 内联实现，三处
做的是同一件事——"system 末尾打 1 个断点 + 从尾部往前找 N 条消息打
断点，幂等重打"——只是三种协议的 JSON 形状不同。策略层（选哪些位置
打断点）可以抽出来复用，机制层（如何在各自的 JSON 结构里打标记）留给
各协议自己实现。

**没有改动的原因**：三处的 JSON 形状差异不小，抽取公共层需要谨慎设计
接口以避免"为了复用而复用"导致代码更难读；且这三处目前都在生产环境
稳定运行，抽取过程中任何细节偏差都可能影响缓存命中率（性能问题，
不会导致功能错误，但难以在测试里发现）。这类改动建议单独一轮、有
针对性的 A/B 或缓存命中率监控来验证。

### 6. 测试覆盖缺口

`json_repair.py`（852 行，工具调用参数纠错逻辑）目前零测试覆盖；
三条主循环（`anthropic_bridge` / `gemini_bridge` /
`_agentic_loop_openai_compat`）加起来近 3000 行，直接单测同样接近为零。
这次新增的两个测试文件是针对本轮实际改动的区域，没有覆盖到这个更大的
缺口——建议作为独立的测试补齐任务安排。

### 7. 文档债

README 引用的 `REFACTOR_PROTOCOL.md` 在仓库里不存在，压缩包里也没有
找到。未重建（无法确认原文件内容，不应该臆造），建议你确认是否遗漏
了这个文件或链接需要更新。

---

## 未改动、但确认"看起来乱实则合理"的地方

- `get_ai_response`（781 行）、`_agentic_loop_openai_compat`（647 行）、
  `_run_tool_calls_and_append`（595 行）：函数很长，但内部注释详实、
  每个分支都有明确的历史原因，不是"写乱了"，是复杂度本身摊在了一个
  函数里。建议未来拆分，但拆分本身有风险（这三处是全项目最核心、
  边界情况最密集的代码），这次没有动。
- `memory_tool.py` / `todo_tool.py` 结构相似（约 25% 文本相似度）：
  两个独立的状态持久化工具，结构echo但不是复制粘贴，判断为合理的
  并行实现，未改动。

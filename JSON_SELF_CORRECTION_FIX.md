# 工具参数 JSON 错误回传优化（Self-Correction 增强）

## 问题

模型偶发返回畸形 JSON 工具参数（字符串内未转义引号、单引号、尾逗号、
Python 字面量、裸换行、未转义反斜杠、截断等）。旧实现只回传一句笼统的：

```
Error: tool bash was not executed because the model returned malformed JSON
arguments (arguments were not valid JSON). Reissue the same tool call with a
valid JSON object.
```

模型既不知道错在哪一行哪一列，也不知道具体病因，只能回复
"Let me try again with proper JSON format" 后盲猜重试，往往连续失败多轮
（text_editor / bash 工具均受影响）。

## 修复后的三层机制

以 `src/apitelegramchat/ai/json_repair.py` 为核心（本改动新增模块）：

1. **保守自动修复**（`repair_json_arguments`）：只做无歧义的语法级修复
   （单/智能引号转双引号、尾逗号删除、转义裸控制字符、转义字符串内未
   转义双引号、`True/False/None/NaN/undefined → true/false/null`、
   Windows 路径反斜杠转义、去注释、剥 markdown 围栏与前后杂质文本）。
   修复成功且为 dict → **直接用修复后的参数执行工具，省掉一整轮模型
   重试**；执行结果末尾附加透明提示（`repair_note_for_result`），告知
   模型参数被修过、请核对结果是否符合意图。
   **安全约束**：截断的 JSON 绝不猜测补全后执行（避免把 `rm -rf /tmp/ju`
   猜补成完整命令执行）。

2. **诊断信封**（`build_invalid_arguments_envelope`）：修复失败时构建
   可恢复错误信封，包含：
   - `parse_error`：解析器报错原文（含行/列/字符位置，如
     `Expecting ',' delimiter: line 1 column 20 (char 19)`）
   - `error_context`：出错位置的上下文摘录（带 `^` 指示符）
   - `diagnosed_issues`：字符级扫描检测到的具体病因清单
   - `raw_arguments_excerpt`：原始参数摘录
   - `looks_truncated`：是否疑似截断

   信封本身是合法 JSON，回传 provider 不会 400，也不会污染下一轮请求。

3. **可操作的错误消息**（`invalid_arguments_message`）：执行层
   （`tool_call_loop.py` `run_one`）把信封渲染成模型可直接照做的修复
   指引——先精确指出解析器报什么错、错在第几行第几列、错在哪一个
   字符（^ 指示），再针对病因给出对应修复规则，最后明确要求
   "Reissue the SAME tool call"（只改 JSON 语法、不改工具与任务）。

## 模型现在收到的错误消息示例

```
Error: tool bash was NOT executed: its arguments are not valid JSON, so they could not be parsed into tool input.

[Parser error] Expecting ',' delimiter: line 1 column 20 (char 19)
[Where the parser stopped]
(line 1)
  {"command": "grep "todo" notes.txt", "timeout": 30}
                     ^
[Problems detected in your arguments]
- escaped unescaped double quotes inside string values
[Your raw arguments]
{"command": "grep "todo" notes.txt", "timeout": 30}
[How to fix] Reissue the SAME tool call (bash) with corrected arguments:
- Escape every double quote INSIDE a string value as \" .
Do not change the tool or the task — only fix the JSON syntax of the arguments.
```

## 改动文件

| 文件 | 改动 |
| --- | --- |
| `src/apitelegramchat/ai/json_repair.py` | **新增**：修复器 + 诊断信封 + 消息渲染 + 透明提示 |
| `src/apitelegramchat/ai/tool_summary.py` | `_normalize_tool_arguments` / `_normalize_tool_call_arguments` 接入修复与信封；`_safe_parse_args` 流式预览兜底；修复 `except-as` 变量块外引用的 UnboundLocalError |
| `src/apitelegramchat/ai/tool_call_loop.py` | `run_one` 渲染诊断消息；旁路路径（dict/OpenAI 对象）就地构建信封；弹出内部提示键并附到结果末尾 |
| `src/apitelegramchat/ai/anthropic_bridge.py` | Anthropic `partial_json` 累积完成后：先修复，失败写信封（不再静默替换为 `{}`） |
| `src/apitelegramchat/ai/agentic_loops.py` | 两条循环（OpenAI 兼容 / Gemini）每轮调用 `_normalize_tool_call_arguments` 批量规范化 |

## 关键 bug 修复

`tool_summary.py` `_normalize_tool_arguments` 原实现：

```python
try:
    parsed = json.loads(raw)
    ...
except (json.JSONDecodeError, TypeError, ValueError) as exc:
    pass
...
envelope = build_invalid_arguments_envelope(raw, exc=exc)  # ← UnboundLocalError!
```

Python 3 的 `except ... as exc` 在块结束时**删除**绑定名，而"畸形且不可
自动修复"（例如截断）恰恰必须走到这最后一行——也就是说最关键的诊断
路径此前必然崩溃，模型什么都收不到。已修复为先把异常转移到
`parse_exc` 局部变量再使用。

## 验证

`scripts` 外部验证脚本覆盖 39 项断言（自动修复 / 诊断信封 / 消息渲染 /
端到端规范化 / 透明提示），全部通过：

- 单引号、尾逗号、Python 字面量、裸换行、未转义内嵌引号 → 全部自动修复
- 截断参数 → 不猜测补全，进入诊断信封路径
- 合法 JSON 但顶层为数组 → 明确告知"必须是 JSON object"
- 信封序列化合法（回传 provider 不 400）
- 修复后的参数保持可执行（command/path 等字段完整）

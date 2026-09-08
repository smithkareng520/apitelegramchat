# 修复说明（2026-09-09）：富文本转换三问题

对应 错误.txt 记录的三个问题。所有修复均通过 `tests/unit/test_markdown_converter.py` 全量用例与端到端重放验证。

---

## 问题 1：message 工具（message_user）的富文本没有做 Markdown → Telegram HTML 转换

### 现象
message_user 发出的卡片 / 回答后编辑的卡片里，`**粗体**`、`` `代码` ``、`- 列表` 等按字面显示。

### 根本原因
全项目所有富文本发送路径都经由 `_rich_message_html_payload()`（内部第 0 步跑 `convert_markdown_to_telegram_html` 兜底转换），**唯独** `message_user_tool._edit_question_message()`（回答/取消/超时后的 `editMessageText` 编辑路径）直接裸拼 `{"content": ..., "html": ...}` payload，绕过了转换；且 `_question_html()` / `_answered_html()` 构造时也只做 `escape_html`，从未做 markdown 转换。初始卡片靠发送时兜底转换"碰巧"能看，编辑后的卡片则完全无转换。

### 修复（src/message_user_tool.py）
1. 新增 `_question_rich_text()`：`escape_html` → `convert_markdown_to_telegram_html` → `wrap_mixed_content_as_blocks`，统一渲染 question 文本；初始卡片、回答卡、超时编辑三个出口共用，渲染结果一致。
2. `_edit_question_message()` payload 改用 `_rich_message_html_payload(body_html)`，与最终回复 / 草稿 / deliver_reply 完全同源（顺带补上了媒体清理与 `skip_entity_detection`）。
3. 安全语义不变：仍先 `escape_html` 再转换，`<script>` / `<img onerror=...>` 注入防护保持（有测试回归）。

---

## 问题 2：折叠块 `<summary>` 展示文本被转义了两次

### 现象（日志 16:08 一轮）
原始内容 summary：``我可以使用 `&lt;tg-time&gt;`…``（转义一次，正常）
payload HTML summary：`<code>&amp;lt;tg-time&amp;gt;</code>`（**二次转义**，长度 614→633）
用户看到字面量 `&lt;tg-time&gt;` 而不是 `<tg-time>`。

### 根本原因
两遍转换 + 非幂等转义：

1. 第一遍：`_get_reasoning_summary()` 用 `escape_html` 转义了 `<`、`>`，但 backtick 保留（summary 不做 markdown 转换）→ `` `&lt;tg-time&gt;` ``；
2. 发送前 `_rich_message_html_payload()` 第 0 步对整条 HTML 再跑一遍 markdown 转换器，summary 里的 backtick 此时才转成 `<code>`，而旧代码该分支用 `html_lib.escape()` 转义代码内容——`&` 被无条件再转义：`&lt;` → `&amp;lt;`。

`_escape_prose()` 早已为正文修过这个问题（只转义裸 `&`），但**行内代码分支和围栏代码块分支漏改**。

### 修复（src/markdown_converter.py）
- `_convert_inline()` 行内代码分支、`_extract_code_block()` 围栏代码块分支：`html_lib.escape` → `_escape_prose`（对已有实体幂等）。两遍转换结果完全一致，附回归测试 `test_inline_code_and_code_block_idempotent_under_second_pass`。
- 附带修正：代码内容中的引号不再多余转义（`'` 不再变 `&#x27;`，显示无差异）。

---

## 问题 3：`content/structure error` 是什么？为什么整条消息退化为纯文本？

### 这个日志的含义
`sendRichHtmlMessage retrying with plain-text paragraph fallback after content/structure error` **不是 Telegram 的错误字符串**，是项目自己的 WARNING：Telegram `/sendRichMessage` 返回 HTTP 400 且 body 含 `rich_message_*` 错误码（`RICH_MESSAGE_CONTENT_REQUIRED`、结构非法等）时，`telegram_messaging.py` 会把整条 HTML 剥成单个 `<p>` 纯文本再重发。所以那条消息最终"发出去了"，但折叠块、列表、`<h1>`、`<tg-time>` 全部丢失。

### 本轮 400 的根本原因（src/ai/rich_message_builder.py `_render_reasoning_html`）
01:11 那轮模型的思考是「文字 + Markdown 列表」混排：

```
CST 可能是：
- China Standard Time (UTC+8)
- Central Standard Time (UTC-6)
- Cuba Standard Time (UTC-5)
```

旧逻辑：转换产物"不以块级标签开头"就**整体包进单个 `<p>`**，列表转换出的 `<ul>` 被吞进段落，产出：

```html
<p>…CST 可能是：<br/><ul><li>China Standard Time (UTC+8)</li>…</ul><br/>…</p>
```

`<ul>` 是块级元素，`<p>` 内不允许嵌套（Telegram Rich Message 解析器结构校验严格，与日志中 payload 逐字吻合）→ 400 → 降级纯文本。

### 修复
1. `markdown_converter.py` 新增 `wrap_mixed_content_as_blocks()`：按块级标签（`<ul>/<ol>/<pre>/<blockquote>/<table>/<h1-6>/<details>/<hr>`）把转换产物切段——块级段原样保留、纯文本段分别包 `<p>`，杜绝任何块级标签被包进段落。
2. `rich_message_builder._render_reasoning_html()` 改用上述函数，替换"整体包单个 `<p>`"的旧逻辑；纯文本思考行为不变（仍是单个 `<p>`）。
3. 加固（src/ai_handlers.py 系统提示词）：tg-time 文档补充「format 值中不得包含空格」——失败消息中 `format="wDT t"` 的空格是次要嫌疑，虽无法从日志证实，但一并规避（正确写法 `wDTt`）。

---

## 修改文件清单

| 文件 | 修改 |
|---|---|
| `src/markdown_converter.py` | 行内代码/代码块幂等转义；新增 `wrap_mixed_content_as_blocks()` |
| `src/ai/rich_message_builder.py` | `_render_reasoning_html()` 块级安全切段 |
| `src/message_user_tool.py` | question 全链路 markdown 渲染；编辑路径改用 `_rich_message_html_payload` |
| `src/ai_handlers.py` | 系统提示词 tg-time format 补充禁空格约束 |
| `tests/unit/test_markdown_converter.py` | 修正 2 个过时断言；新增双遍转换幂等回归测试 |

## 验证结果

- `pytest tests/unit`：修复前 3 failed / 196 passed → 修复后 **1 failed / 198 passed**（唯一剩余失败 `test_draft_manager.py::test_tool_batch_end…` 为修复前即存在的既有失败，与本次改动无关）。
- 用 01:11 轮次真实输入做端到端重放：payload 不再含 `<p>…<ul>` 非法嵌套、无 `&amp;lt;` 二次转义，不再触发 plain-text 降级。

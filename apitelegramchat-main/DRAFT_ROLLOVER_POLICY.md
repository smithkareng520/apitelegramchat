# Telegram 富消息草稿滚动策略

## 已确认的 API 限制

根据用户提供的、基于 Telegram Bot API 与 TDLib 的限制资料，`sendRichMessage` 的富消息文本上限为 **32768 个 UTF-8 字符**。该限制适用于 HTML 与 Markdown 表示，按实体解析后的富文本内容计算：HTML 标签和属性不计入；HTML 实体按解析后的字符计入；自定义 emoji 的替代文本及公式源也计入。

`sendRichMessageDraft` 是临时的 30 秒预览；生成完成后，机器人必须调用 `sendRichMessage` 才会将内容持久化。`InputRichMessage` 的 `html`、`markdown` 与 `blocks` 必须三选一，本项目所有富消息请求现只提交 `html`。资料同时给出富消息的相关结构上限：最多 500 个块、16 层嵌套深度、50 个媒体与表格最多 20 列。

> **重要更正**：普通 `sendMessage` 的 4096 字符限制不适用于 Rich Message。本项目此前的 3200 个 HTML 源码字符阈值过度保守，现已替换。

## 已实现策略

| 项目 | 实现 |
|---|---|
| 真实硬上限 | `RICH_MESSAGE_TEXT_CHARS_MAX=32768`，按 HTML 实体解析后的可见文本长度估算。 |
| 默认滚动阈值 | `RICH_DRAFT_ROLLOVER_TEXT_CHARS=30000`；为 custom emoji 替代文本、公式源与实现差异保留 2768 字符余量。 |
| 结构阈值 | `RICH_MESSAGE_BLOCKS_MAX=500`，并在 `RICH_DRAFT_ROLLOVER_BLOCKS=440` 时主动滚动。 |
| 计数方法 | 先去除富文本标签、再解析 HTML 实体，按 Unicode 代码点计数。它是 API 语义的保守近似；若项目引入原生 Rich Blocks，应以服务端返回为最终准则。 |
| 旧段处理 | 达到文本或块阈值时，先通过 `sendRichMessage` 将当前草稿段永久化，再标记旧草稿失效并创建新的 `draft_id`。不会为压缩 UI 而静默丢弃内容。 |
| 真正超限拆分 | 仅当一段内容已经超过 32768 解析后字符时，才降级为安全的转义 `<p>` 段分割。正常的 30000 字符滚动段保留原有富文本格式。 |
| 最终收尾 | 最终段若触发滚动，已永久化的内容不会再被重复发送；否则按普通最终富消息路径发送。 |
| 工具界面 | 单个工具详情最多 6000 字符，单工具组最多 24000 字符；这是草稿界面优化，不会影响模型上下文、checkpoint 或 workspace ledger。 |

## 配置

```bash
# 真实限制；除非 Telegram 更新 API，不建议上调。
RICH_MESSAGE_TEXT_CHARS_MAX=32768
RICH_MESSAGE_BLOCKS_MAX=500

# 主动滚动阈值；可按照实际错误率和用户体验谨慎调整。
RICH_DRAFT_ROLLOVER_TEXT_CHARS=30000
RICH_DRAFT_ROLLOVER_BLOCKS=440
```

## 验证

```bash
python3 validate_syntax.py
python3 validate_draft_rollover_static.py
PYTHONPATH=src python3 validate_token_context.py
```

隔离草稿测试直接抽取生产构建器的解析后字符计数、块计数、滚动判断与分段方法体，验证实体解析后的字符预算、500 块前滚动、超长安全拆分与 `InputRichMessage` 单表示请求体。最近验证结果：超长测试内容拆分为 9 段，最大解析后长度为 743 字符（测试缩小了阈值以提高覆盖率）。

## 来源

- 用户附件 `pasted_content_2.txt`，第 4–24、31–42、102–110 行。
- [Telegram Bot API：sendRichMessageDraft](https://core.telegram.org/bots/api#sendrichmessagedraft)
- [Telegram Bot API：InputRichMessage](https://core.telegram.org/bots/api#inputrichmessage)

# 工具上下文可回溯压缩

本次改动将工具调用历史的压缩从“优先删除旧轮次”调整为“优先卸载旧工具的大载荷”。当前仅处理 `wikipedia`、`fetch_url` 与 `text_editor`；其他工具调用与结果保持原样。

## 策略

当历史消息超过 `MAX_HISTORY_MESSAGES`，或预检发现当前请求快照加上新用户输入将超过模型上下文预算时，系统会执行一次工具压缩。系统只枚举仍保留完整结果的目标工具调用轮次，并只压缩这些轮次中**最早的约一半**。最新一半保持完整，以维持近期任务的连续性。

每个被处理的目标工具调用会保留提供方要求的 `id`、`type`、函数名和最小可复现定位参数，同时删除冗余字段：

| 工具 | 历史调用中保留的参数 |
|---|---|
| `wikipedia` | `query`、存在时的 `lang` |
| `fetch_url` | `url` |
| `text_editor` | `command`、`path` |

工具结果会被替换为一个短指针，例如：

```text
Tool result archived at .context-archive/tool-results/round-0001-<hash>.json. Use text_editor view with path ".context-archive/tool-results/round-0001-<hash>.json" to retrieve the original fetch_url call and result if needed.
```

原始的完整调用参数与结果被保存为 JSON 文件，位于该用户私有工作区的 `.context-archive/tool-results/`。后续模型可通过现有的 `text_editor` 工具读取该相对路径；压缩只在文件成功写入后才替换内存中的历史消息。

## 不变性与边界

压缩不修改工具调用 ID，也不删除 `assistant` 工具调用消息或配对的 `tool` 结果消息，因此保留工具协议结构。压缩模块对已经指向归档的结果是幂等的，不会重复压缩同一调用。原先的预检和 30 条历史阈值不再直接按用户轮次删除对话；对实际 API 请求，仍由既有的 `select_request_context()` 负责选择结构有效且有大小上限的上下文快照。

当前归档位于本地私有工作区，与该项目既有工作区语义一致。若部署环境要求跨实例或跨重启恢复，应在现有的单文件持久化层上为 `.context-archive/` 增加显式同步与按需恢复；本次改动没有引入工作区全量同步。

## 相关文件

| 文件 | 职责 |
|---|---|
| `src/apitelegramchat/tool_context_compaction.py` | 归档完整载荷、最小化目标调用、生成检索指针、只压缩旧半数轮次。 |
| `src/apitelegramchat/app.py` | 在预检与历史阈值处接入压缩，移除这两个位置的直接删轮次逻辑。 |
| `tests/validate_tool_context_compaction.py` | 验证压缩比例、原始载荷可回取、非目标工具不变和单轮边界。 |

## 验证

已执行以下本地回归测试：

```bash
python3 tests/validate_context_management.py
python3 tests/validate_tool_loop.py
python3 tests/validate_tool_arguments_json.py
python3 tests/validate_tool_crash_fix.py
python3 tests/validate_draft_rollover.py
python3 tests/validate_draft_frontend_flow.py
python3 tests/validate_draft_interrupt_paths.py
python3 tests/validate_stream_timeout_recovery.py
python3 tests/validate_tool_context_compaction.py
```

所有测试通过。

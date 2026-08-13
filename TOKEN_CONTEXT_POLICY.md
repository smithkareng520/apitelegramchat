# Token 驱动的 Agent 上下文策略

本项目已移除以下两个正常运行时的固定数量截断机制：

- 聊天历史不再使用 `MAX_HISTORY_MESSAGES = 30`。
- 主 Agent 不再以固定工具轮数停止；子 Agent 不再以固定 32 轮停止。

## 当前行为

### 1. 活跃主/子 Agent 上下文

每次工具调用后，主 Agent 与子 Agent 都计算当前 `loop_messages` 的保守 token 估算值。估算器覆盖中文、英文、长无空格 JSON/日志、tool calls 与多模态占位开销。

当上下文达到当前模型输入硬预算的 **68%** 时，系统自动：

1. 提取原始用户目标。
2. 将近期工具结果压缩为有界的结构化检查点。
3. 保留已完成事项、待处理工具意图、最后观察、下一动作和“禁止重复”指令。
4. 将下一次模型调用切换为 `system prompt + 原始目标 + checkpoint` 的紧凑上下文。
5. 在同一个草稿中发送“已保存执行检查点，正在以紧凑上下文继续任务”。

因此，任务不会因为运行 30、40 或 32 轮而自然停止。它会在接近 token 预算时自动进入下一执行段。

### 2. 聊天历史

一次任务完成后，`conversation_history` 只持久化：

- 用户消息；
- 最后的用户可见 assistant 答复；
- 工具调用/结果数量的轻量元数据。

`assistant.tool_calls`、`tool` 原始结果和推理字段不再写入聊天历史，因此单次大任务不会用几十条协议消息挤掉自己的原始目标或最终结论。

旧完成回合仅在 token 预算超限时按“完整用户回合”删除。刚提交的当前回合受保护；如最终答复异常巨大，只压缩其长期存档文本，绝不删除该任务。

### 3. 紧急熔断

仍保留与 token 无关的无限循环保护，但它不是常规轮数上限：

| 环境变量 | 默认值 | 含义 |
|---|---:|---|
| `AGENT_MAX_TOOL_CALLS_EMERGENCY` | 512 | 主 Agent 出现异常循环时的总工具调用熔断。 |
| `SUBAGENT_MAX_TOOL_CALLS_EMERGENCY` | 512 | 子 Agent 出现异常循环时的总工具调用熔断。 |
| `SUBAGENT_DEFAULT_TIMEOUT` | 900 秒 | 子 Agent 的总墙钟时间上限。 |

如果触发紧急熔断，系统生成明确的暂停终态，而不是把它伪装成正常的“请继续”。

## 模型预算

`agent_context.py` 根据 `ModelConfig.max_context` 和 `max_output_tokens` 计算：

```text
input_hard_limit = (max_context - max_output_tokens - 1024) × 0.85
checkpoint_trigger = input_hard_limit × 0.68
```

`85%` 为模型 tokenizer 差异、动态工具 schema 与提供商协议留出安全余量。`68%` 是主动压缩阈值，避免模型在接近硬窗时才被动失败。

## 已覆盖的验证

执行以下命令：

```bash
python3 validate_syntax.py
PYTHONPATH=src python3 validate_token_context.py
```

验证包含：长工具轨迹不会进入聊天历史、检查点会显著缩短活跃上下文、旧回合被 token 预算移除时当前任务仍受保护。

## 后续增强建议

当前实现已解决“30 条消息删除当前 Agent 任务”和“固定轮数中断”问题。生产级下一步是将 checkpoint、工具事件和工件引用持久化到 SQLite/PostgreSQL，使进程重启也能恢复正在执行的长任务。

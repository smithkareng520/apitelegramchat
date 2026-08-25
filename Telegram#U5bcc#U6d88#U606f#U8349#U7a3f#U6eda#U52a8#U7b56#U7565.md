# Telegram 富消息草稿滚动策略

## 目标

本实现将长输出草稿的滚动定义为**回合边界切换**，而不是在流式刷新期间立即创建后台新草稿。系统在草稿接近富消息容量时仅标记为“待滚动”；随后等待当前模型返回及其声明的全部工具调用完成，在下一次模型请求之前同步永久化旧段并发送新草稿首帧。

> 一个“完整回合”指一次 assistant 模型返回，以及该返回中所有并行 `tool_calls` 的最终结果均已写回模型上下文、均已在界面中标记为 `done` 或 `error`。单个工具调用不是切换边界，因为同一次模型返回可能包含多个并行工具。

该设计保证同一批工具活动不会被拆到两个草稿中，并消除“后端已经开始后续模型请求、前端却迟迟没有新草稿”的异步时序问题。

## 容量预算

| 维度 | 容量预警阈值 | 常规滚动阈值 | 富消息上限 |
|---|---:|---:|---:|
| 项目内部文本预算 | 5,000 token | 6,800 token | 7,500 token |
| 富消息结构块 | 380 块 | 440 块 | 500 块 |

容量预警阈值通过 `RICH_DRAFT_INTERACTIVE_TOKEN_BUDGET` 和 `RICH_DRAFT_INTERACTIVE_BLOCKS` 配置；正常分段预算通过 `RICH_DRAFT_ROLLOVER_TOKEN_BUDGET` 配置，硬保护通过 `RICH_DRAFT_HARD_GUARD_TOKEN_BUDGET` 配置。Telegram 服务端仍有 32,768 个解析后 Unicode 字符的外部协议硬限制，因此实现同时保留字符安全阈值，但项目内部预算统一按 token 计算。

## 状态机

| 状态 | 触发条件 | 行为 |
|---|---|---|
| 正常写入 | 未接近容量预警 | 流式内容继续写入当前 `draft_id`。|
| 待滚动 | 可见文本或结构块达到容量预警 | 设置 `_rollover_pending=True`；`flush()` 仍只刷新当前草稿。|
| 回合收束 | 模型返回结束，且所有同批工具均有终态 | 关闭工具组、刷新最终状态，并在下一模型请求前调用 `rollover_at_turn_boundary()`。|
| 交接 | 旧段永久消息正在发送 | 不启动下一次模型请求；防御性 `handoff` 缓冲接收任何迟到增量。|
| 新草稿 | 旧段永久化成功 | 标死旧 draft，立即登记新的 `draft_id` 并强制发送新草稿首帧；旧预览在后台快速清理。|
| 失败恢复 | 旧段永久化失败或任务取消 | 将 handoff 缓冲恢复到旧构建器，保留 pending，以后续完整回合重试。|

## 切换顺序

```text
接近容量
  → 仅置位 rollover_pending
  → 完成当前模型返回
  → 完成该返回的全部并行工具调用
  → 关闭工具组并刷新其最终状态
  → 永久化旧段
  → 标死旧 draft
  → 分配并登记新 draft_id
  → 合并 remainder + handoff 缓冲
  → 强制发送新草稿首帧
  → 后台快速删除旧预览
  → 发起下一次模型请求
```

滚动时只在完整最外层结构边界处分段，不会正常切开 `details`、表格、列表、代码块、引用块或媒体块。若没有合法完整边界且接近硬上限，系统提取可见文本、转义并按自然空白或句末切分，以保证内容可见且不会提交超限富消息。

## 无损保证

`rollover_at_turn_boundary()` 在永久化旧段前冻结用于切分的快照，并启用 `_handoff_text` 缓冲。理论上该阶段不会有后续模型流，因为调用点位于回合边界；若发生迟到回调或未来调用方违反这一前提，新增文本仍会被写入 handoff 缓冲，并在新草稿建立时与 remainder 一并合并。

若永久消息发送失败，系统不会切换 `draft_id`，而是把 handoff 缓冲恢复到旧草稿并保留 `_rollover_pending`。因此不会出现“旧永久消息缺内容、新草稿也没有内容”的丢失路径。

## 实现位置

| 组件 | 位置 | 职责 |
|---|---|---|
| 容量预警 | `RichMessageBuilder._arm_rollover_if_needed` | 在常规 `flush()` 中只置位 pending。|
| 回合边界切换 | `RichMessageBuilder.rollover_at_turn_boundary` | 在完整模型/工具批次结束后同步执行无损切换。|
| 工具批次收束 | `_run_tool_calls_and_append` | 确保每个工具均有终态，关闭工具组。|
| 适配器调用点 | OpenAI/Gemini Agentic Loop | 在工具批次完成后、下一模型请求前调用边界切换。|
| 旧预览清理 | `delete_message_fast` | 在新草稿首帧后后台运行，不阻塞新草稿可见性。|

## 配置

```bash
RICH_MESSAGE_TOKEN_BUDGET=7500
RICH_DRAFT_INTERACTIVE_TOKEN_BUDGET=5000
RICH_DRAFT_ROLLOVER_TOKEN_BUDGET=6800
RICH_DRAFT_HARD_GUARD_TOKEN_BUDGET=7372
RICH_MESSAGE_BLOCKS_MAX=80
RICH_DRAFT_INTERACTIVE_BLOCKS=45
RICH_DRAFT_ROLLOVER_BLOCKS=70
```

一般不应将 token 预警阈值与硬预算设置得过近。当前 5,000 / 6,800 / 7,500 token 分层为工具批次收尾、永久消息处理和新草稿首帧预留了余量；另外保留 32,768 Unicode 字符的协议安全阈值，避免低 token 密度的英文文本触发 Telegram 服务端限制。

## 回归验证

`tests/validate_draft_rollover.py` 覆盖以下属性：容量预警不会立即新建草稿；只有完整回合边界才会永久化旧段；交接期间新增 delta 会进入新草稿；永久化失败会将 handoff 恢复到旧草稿；慢旧预览删除不会延迟新草稿首帧；测试还验证旧后台滚动实现已被移除。

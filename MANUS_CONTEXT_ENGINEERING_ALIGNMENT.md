# 对齐 Manus 上下文工程策略的实现说明

本文记录本项目在阅读 Manus 官方文章《[AI代理的上下文工程：构建Manus的经验教训][1]》后完成的增强。该文章明确将其方法称为团队工程实验收敛出的“局部最优”，并非对所有模型、工具协议和产品形态都通用的规则。[1] 因此，本实现保留其可迁移原则，同时适配本项目的 Telegram、OpenAI 兼容 API、Gemini 兼容 API、工作区与现有工具执行器。

## 已对齐的机制

| 官方文章原则 | 本项目实现 | 代码位置 | 验证方式 |
|---|---|---|---|
| **围绕 KV-cache 设计**：稳定前缀、上下文只追加、确定性序列化。 | 将每秒变化的时间从系统提示主体移到系统消息后的运行时消息；主循环保持工具定义不在每轮变更；尾部状态通过追加的任务复述呈现。 | `ai_handlers.py`：`build_system_prompt()`、`_build_runtime_context_message()`；`agent_context.py`。 | 静态检查确认系统前缀不再嵌入实时值；运行时可使用已有 prompt-cache 标记。 |
| **遮蔽而非移除工具**。 | 长任务迭代中不动态按阶段增删工具 schema；原有完整工具清单保持稳定。状态约束通过 checkpoint、任务复述与 `next_action` 传达。 | `ai_handlers.py` 的两条主 Agent 循环与 `subagent_tool.py`。 | 检查点切段不改 `tools` 参数。 |
| **文件系统作为上下文**。 | 每次执行段和每次任务复述都会将结构化状态落为工作区 `.agent_context_ledger.json`；checkpoint 保存该路径、URL 和工具输出中识别出的可重读文件路径。 | `agent_context.py`：`persist_task_ledger()`、`restorable_references`。 | 回归测试验证 URL、路径和 ledger 均进入 checkpoint。 |
| **可恢复压缩**。 | 压缩时不只保留短摘要；同时保留 URL、工作区路径、任务 ledger 路径和下一动作，模型可以通过已有工具重新读取原始资料。 | `agent_context.py`：`compact_active_agent_context()`。 | 长轨迹压缩测试。 |
| **通过复述操控注意力**。 | 每个未触发压缩的工具步骤后，追加一个受限长度的 `TASK_RECITATION`，将目标、近期完成、失败证据、工件和下一步移到上下文末尾。 | `agent_context.py`：`task_recitation_message()`；主/子 Agent 循环。 | 回归测试验证复述含错误证据与任务状态。 |
| **保留错误内容**。 | checkpoint 显式记录近期失败/超时/异常工具输出；后续 segment 会看到失败证据及“禁止重复已完成操作”的约束。 | `agent_context.py`：`failure_evidence`。 | 回归测试向工具轨迹注入失败输出。 |
| **不按轮数裁剪**。 | 上下文切段由模型预算的 68% token 阈值触发；历史清理按 token 预算，只清理旧完成回合，绝不按 30 条协议消息删除正在运行的任务。 | `agent_context.py`、`app.py`、`ai_handlers.py`、`subagent_tool.py`。 | 长工具轨迹和当前回合保护测试。 |

## 当前 token 策略

输入硬预算按以下保守公式计算，预留模型输出、协议开销和 provider tokenizer 差异：

```text
input_hard_limit = (max_context - max_output_tokens - 1024) × 0.85
checkpoint_trigger = input_hard_limit × 0.68
```

达到触发点后，主 Agent 或子 Agent 自动写入 workspace ledger、生成包含可恢复引用和错误证据的 checkpoint，然后用稳定系统前缀、原始目标和 checkpoint 启动新的执行段。它不会要求用户手动发送“继续”。

## 与原始文章的刻意差异与限制

官方文章中的 **logit masking / response prefill** 需要模型提供商或自托管推理栈暴露 token 级约束能力。当前项目面向 OpenAI 兼容与 Gemini 兼容接口，无法可靠获得这项底层能力。因此，本版本坚持“工具定义不在迭代中移除”，并用结构化任务状态约束行动；它**不宣称**已实现真正的 token-logit 掩码。

同样，项目可以稳定提示前缀并保留 `cache_control` 标记，但实际 KV-cache 命中率、缓存有效期和计费折扣取决于各 provider 的实现与路由。本版本完成了必要的上下文形状优化，但需要接入调用级 telemetry 后才能量化命中率、TTFT 与缓存输入 token 占比。

## 验证结果

在打包前已执行：

```bash
python3 validate_syntax.py
PYTHONPATH=src python3 validate_token_context.py
```

验证覆盖源文件语法、长工具轨迹压缩、当前任务保护、失败证据、URL/工件路径恢复引用、任务复述和工作区 ledger。最近一次行为测试的估算结果为：原始轨迹 `7763` tokens，压缩后 `3805` tokens，受保护聊天历史 `4587` tokens。

## 建议的下一步

下一阶段应增加 provider 级指标：`prompt_cache_hit_tokens`、`uncached_input_tokens`、TTFT、每段 token、checkpoint 恢复成功率与失败工具重复率。之后可依据真实数据调整 68% 阈值和复述频率。若未来统一到支持 response prefill 或 constrained decoding 的推理端，再实现真正的状态机工具 token 掩码。

[1]: https://manus.im/zh-cn/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus

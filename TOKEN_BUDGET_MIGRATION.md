# Token 预算迁移说明

本次修改将项目中用于**模型上下文、工具输出、网页抓取、子代理输入输出、持久化文本字段和界面文本裁剪**的字符长度限制，统一迁移为基于 `tiktoken` 的 token 预算。新增的 `apitelegramchat.token_budget` 是唯一的计数与截断实现；默认使用 `o200k_base` 编码，并可通过 `TOKEN_BUDGET_ENCODING` 覆盖。所有截断函数都将截断提示本身计入预算，因此返回文本不会超过指定 token 数。

## 关键预算与数值复核

| 场景 | 新配置 / 常量 | 默认值 | 复核结论 |
|---|---|---:|---|
| 全局工具返回 | `TOOL_RESPONSE_TOKEN_BUDGET` | 20,000 tokens | 满足“20,000 指 token 而非字符”的要求；全局模型上下文入口统一执行硬截断。 |
| `fetch_url` 总输出 | `FETCH_RESPONSE_TOKEN_BUDGET` | 20,000 tokens | 与全局工具预算一致；最终保护确保不超过 20,000 tokens。 |
| `fetch_url` 正文 | `FETCH_BODY_TOKEN_BUDGET` | 19,000 tokens | 为标题、来源链接和截断提示预留约 1,000 tokens，并在完整 HTML 块边界截断。 |
| 对话上下文 | `CONTEXT_MAX_TOKENS` | 50,000 tokens | 对应长会话的宽松输入上限；单条超长纯文本消息也会被裁剪，使选择结果严格满足预算。 |
| 子代理单次工具结果 | `SUBAGENT_TOOL_RESULT_TOKEN_BUDGET` | 20,000 tokens | 与父代理工具返回的全局上限一致，避免子代理路径绕开保护。 |
| 子代理任务 / 上下文 / 答案 | `SUBAGENT_*_TOKEN_BUDGET` | 2,000 / 4,000 / 4,000 tokens | 为任务描述、附加背景和最终答案分别设置了独立且足够的预算。 |
| 富消息草稿 | `RICH_DRAFT_*_TOKEN_BUDGET` | 3,000 / 5,400 / 6,000 tokens | 使用 token 作为滚动和交互阈值，减少中英文内容密度差异导致的误判。 |
| 记忆、待办、人工问答字段 | 各模块 `*_TOKEN_BUDGET` | 见源码 | 所有原先按字符裁剪的用户内容字段已改为 token 裁剪。 |

> Telegram 富消息的 `RICH_MESSAGE_TEXT_PROTOCOL_LIMIT=32768` 是服务端协议的解析后 Unicode 文本安全边界，不是模型内容预算；草稿的滚动决策已改为 token 计数。

## 实施范围

| 模块 | 修改内容 |
|---|---|
| `token_budget.py` | 新增精确 token 计数、预算判断、Unicode 安全截断及 JSON 计数函数。 |
| `context_manager.py` | `CONTEXT_MAX_TOKENS` 和 `estimated_tokens` 取代旧的字符统计；支持单条超限文本的硬裁剪。 |
| `tool_executors.py` | 所有模型可见工具输出通过 20,000 token 全局预算；界面字段展示同样改用 token 裁剪。 |
| `fetch_rich_content.py`、`search_engine.py` | 网页、百科抓取、标题、图注、链接文本、回退正文均使用 token 预算；富 HTML 保持完整块截断。 |
| `subagent_tool.py` | 输入、上下文、工具结果、答案和 HTML 卡片预览全部改为 token 预算；HTML 预览仍会补齐标签。 |
| `memory_tool.py`、`todo_tool.py`、`ask_user_tool.py` | 持久化文本字段、卡片预览和问答输入的字符裁剪改为 token 裁剪。 |
| `rich_message_builder.py` | 草稿滚动从可见字符阈值改为可见 token 阈值；协议级安全检查单独保留。 |
| `pyproject.toml`、`requirements.txt` | 固定新增 `tiktoken==0.11.0`。 |
| 文档与测试 | 更新旧环境变量说明；新增 `tests/test_token_budget.py`。 |

## 不兼容配置变更

所有旧式字符长度环境变量与标识符均已移除，不再保留兼容别名。部署时请改用对应的 token 预算环境变量或 `CONTEXT_MAX_TOKENS` 配置。

## 验证结果

执行了以下检查，均通过：

```bash
PYTHONPATH=src python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest -v tests.test_token_budget
```

测试覆盖精确 token 截断、Unicode 安全性、上下文硬预算、全局工具 20,000 token 限额、富抓取 20,000 token 限额，以及全项目不再存在废弃的长度标识符。交付包已清理 `__pycache__` 和 `.pyc` 文件。

## 后续修复：富消息草稿刷新异常

部署日志显示草稿刷新阶段出现 `ValueError: too many values to unpack (expected 3)`。根因是富消息边界扫描器在 token 迁移后新增了“可见 Unicode 单位”这一第四返回值，而 `RichMessageBuilder.flush()` 仍按旧的三项结构解包。

现已将 `flush()` 更新为接收四项返回值，并使用其中的 `frame_tokens` 记录帧级日志。新增回归测试会实际调用 `RichMessageBuilder.flush()`，以模拟草稿发送并验证四项返回值不会再触发解包异常。

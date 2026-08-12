# 文件化记忆与按需工具搜索升级说明

本次升级将原有的“单条 JSON 记忆 CRUD”改造为**文件化、按需读取、跨会话持久化**的记忆系统，同时在 OpenAI 兼容调用链中实现了与 Claude 工具搜索相同的核心工作流：首轮仅暴露核心工具，模型先搜索目录，命中的工具在下一轮才成为可调用定义。官方设计的重点是让记忆与工具定义都按需进入上下文，而不是在会话开始时全部预加载。[1] [2]

> 本项目并不直接调用 Anthropic 的 `memory_20250818` 或服务器端工具搜索类型，因为当前代理链路使用 OpenAI 风格函数调用。实现采用**客户端等价方案**：服务器持有完整工具目录，`tool_search` 返回经过验证的工具引用信息，编排循环在下一轮传入相应的完整函数定义。

## 记忆工具

新版 `memory` 工具将逻辑目录 `/memories` 映射到每个用户命名空间的私有状态目录，并将每个记忆文件单独同步到对象存储。每项操作都首先将路径解码、规范化并验证其仍位于该根目录内；路径遍历、隐藏路径、反斜杠路径、符号链接逃逸及对根目录本身的删除/重命名都会被拒绝。

| 命令 | 关键参数 | 行为与返回 |
| --- | --- | --- |
| `view` | `path`、可选 `view_range` | 列出目录至两层深，或以 1-based、6 字符右对齐行号读取 UTF-8 文件。长视图在 16,000 字符处截断。 |
| `create` | `path`、`file_text` | 仅创建新文件；目标已存在时明确返回错误，避免静默覆盖。 |
| `str_replace` | `path`、`old_str`、可选 `new_str` | 仅允许唯一精确匹配，重复或未命中时返回可操作错误；成功后返回带行号的编辑片段。 |
| `insert` | `path`、`insert_line`、`insert_text` | 在指定 1-based 行之后插入，`0` 表示文件开头。 |
| `delete` | `path` | 删除文件或目录树，但拒绝删除 `/memories` 根目录。 |
| `rename` | `old_path`、`new_path` | 重命名或移动文件/目录；不覆盖目标，不允许把目录移入自身。 |

记忆目录总容量限制为 **4 MiB**，单文件文本输入限制为 **1,000,000 个字符**。旧版 `memories.json` 在首次使用新系统时会被保留并导入为 `/memories/legacy-import.md`，从而避免既有记忆丢失。旧结构化 API 会返回明确的迁移提示，而不是产生隐式不兼容行为。

## 工具搜索

`tool_search` 维护一个不可变的服务端 `ToolCatalog`，检索字段包括工具名、说明、参数名、参数说明和示例。它支持两种策略：`bm25` 适合自然语言任务意图，`regex` 使用长度不超过 200 字符且不区分大小写的 Python 正则表达式。两种模式均最多返回 5 个结果；无结果、无效策略和无效正则均有结构化错误返回。

| 阶段 | 实现行为 |
| --- | --- |
| 初始请求 | 仅传递 `tool_search` 和高频核心工具：`web_search`、`fetch_url`、`text_editor`、`bash`、`ask_user`、`todo`、`memory`。 |
| 搜索 | 模型调用 `tool_search`，结果携带 `tool_references`、相关度、命中字段及 `loaded_tool_names`。搜索不执行实际任务。 |
| 校验 | 编排器仅接受目录中存在的名称，拒绝模型可见结果中伪造的工具名。 |
| 下一轮 | 通过校验的完整工具定义被追加到当轮可调用工具集，已加载工具可在后续轮次复用。 |
| 用户呈现 | 工具气泡展示查询、检索方式、候选总数、加载数、命中字段和简短说明，而非原始 JSON。 |

主 OpenAI 兼容循环和 Gemini 兼容循环均实现了相同的加载状态管理；MCP 的 `memory.manage` 也已切换到文件化命令模式，资源索引显示新版记忆文件树。

## 验证

项目新增了以下测试文件：

| 文件 | 覆盖范围 |
| --- | --- |
| `tests/test_memory_and_tool_search.py` | 文件生命周期、行号视图、唯一替换约束、路径遍历防护、目录深度限制、BM25、正则错误与工具名校验。 |
| `tests/runtime_smoke.py` | 实际工具目录的初始集、天气工具发现、记忆创建/编辑/读取及路径阻断。 |

在项目根目录执行：

```bash
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest -v tests/test_memory_and_tool_search.py
PYTHONPATH=src python3 tests/runtime_smoke.py
```

## 参考资料

[1] [Claude Platform Docs：记忆工具](https://platform.claude.com/docs/zh-CN/agents-and-tools/tool-use/memory-tool)

[2] [Claude Platform Docs：工具搜索工具](https://platform.claude.com/docs/zh-CN/agents-and-tools/tool-use/tool-search-tool)

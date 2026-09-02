# text_editor 工具审查与修复（TEXT_EDITOR_FIXES）

对 `src/apitelegramchat/search_engine.py` 中 `execute_text_editor`
（含 `_editor_safe_path` / `_resolve_editor_path` / 持久化辅助函数）做了一轮
专项审查：四命令（view / str_replace / create / insert）的核心语义、
路径安全、行尾保真、并发持久化、输出上限与错误约定。结论：**架构是
健康的**（workspace 锁、原子写、唯一匹配严格性、路径逃逸防护、
`view`→`str_replace` 自纠闭环设计都不错），但审查发现并修复了以下缺陷。

## 修复清单

### 1.（高）符号链接逃逸时抛未捕获异常，绕过全部错误处理

`_resolve_editor_path` 检出逃逸 workspace 的符号链接时抛
`ValueError`，`workspace_workdir` 在 workspace 根被符号链接化时抛
`RuntimeError`——两者都发生在主 `try:` 内，但 except 链只接
`FileNotFoundError / PermissionError / IsADirectoryError / OSError` 家族，
**ValueError / RuntimeError 直接冒泡**：

- 主 agent 循环：被 `run_one` 兜底成 `Exception: tool text_editor
  failed - Invalid path: ...`——丢失 `Error:` 前缀约定（不参与失败
  判定与连击熔断签名归一化）、丢失恢复指引；
- MCP 入口（`mcp/registry.py` 的 `workspace.view` / `workspace.edit`，
  `invoke` 无兜底）：异常直接打崩该次 MCP 调用。

修复：except 链末尾增加 `except (ValueError, RuntimeError) as exc:
return f"Error: {exc}"`（同时覆盖写入时的 `UnicodeEncodeError`——
它也是 ValueError 子类，此前同样会裸冒泡）；
`_ensure_runtime_workspace` 段新增 `except RuntimeError`。

### 2.（高）CRLF 行尾被任何一次编辑静默整体改写为 LF

旧实现 `local_path.read_text(encoding="utf-8")` 走 universal newlines
读取：CRLF/CR 在**读入时**就被翻译成 LF，`_write_text_editor_file` 把
归一化后的内容写回。后果：对 CRLF 文件做一次只改一行的
`str_replace`，**全文件行尾被重写**（git diff 全文件变更；.bat / .reg /
部分 CSV 等行尾敏感格式被破坏）；混合行尾文件同样被整体归一化。

修复（行尾保真策略，与 `view` 所见保持一致）：

- 读写全部走**原始字节**（`read_bytes().decode()` / 二进制写），
  彻底绕过文本模式的平台换行翻译；
- **纯 CRLF 文件**（每个 `\n` 都属于 `\r\n`）：在 LF 空间匹配
  （`old_str` / `new_str` 一并归一化，所以无论模型给 LF 还是 CRLF 版
  `old_str` 都能命中——`view` 输出本来就显示不出 `\r`），写回时统一
  还原成 CRLF，编辑后行尾风格**保持不变**；
- **纯 LF 文件**：按原始字节精确匹配，行为与旧版完全一致；
- **混合行尾文件**：先精确匹配；失败且文件含 CR 时按 LF 归一化重试
  一次（等价于旧版能力），命中则写入归一化结果并在**成功消息里明说**
  （`file had mixed line endings; normalized to LF`），不再静默改写；
- `insert` 对纯 CRLF 文件同样在 LF 空间处理后写回 CRLF；
  `create` 的 `file_text` 逐字节落盘。

### 3.（高）成功消息泄漏服务器绝对路径

`Successfully created file in /tmp/apitelegramchat_data/workspaces/<ns>/a.txt`
——把服务器目录结构暴露给模型与最终用户（UI 直接渲染工具结果），且
违背项目自己反复强调的「一切路径相对 workspace 根目录」约定（模型看到
绝对路径后会开始在 bash 里使用绝对路径，破坏路径风格一致性）。
修复：成功消息统一改用 workspace 相对路径（`Successfully created
file: a.txt`）。UI 摘要本来就按 `fn_args.path` 渲染，无下游解析依赖。

### 4.（中）后台持久化任务：无强引用可能被 GC + 重读文件的竞态

三处 `asyncio.create_task(_persist_edited_file(...))` 丢弃返回值。
事件循环对 Task 只持**弱引用**，任务可能在执行中途被垃圾回收
（Python 官方文档明确要求保存引用）→ R2 镜像随机丢失。且任务自行从
磁盘重读文件，与后续并发编辑存在时序竞态（旧内容可能覆盖新内容）。

修复：

- 新增 `_editor_persist_tasks` 引用集合 + done 回调移除
  （`_spawn_persist_task`），任务可被测试 / 关停等待；
- `persist_workspace_file` 新增可选 `content_bytes`：直传本次编辑
  **实际写入**的字节，消除竞态并省一次 IO（本地 workspace 仍是
  source of truth，R2 只是镜像，语义不变）。

### 5.（中）bool 穿透 int 检查

`isinstance(True, int)` 为真：`insert_line=True` 被当作第 1 行插入、
`view_range=[True, -1]` 被当作 `[1, -1]`。主链路的 L2 schema 校验
能拦（integer 检查显式排除 bool），但 MCP 入口与防御深度要求执行器
自守。修复：`_is_plain_int`（排除 bool）用于 `insert_line` 与
`view_range` 元素校验；`insert` 的错误消息同时改为准确描述类型要求。

### 6.（中）view 输出无预算内截断 / 单行无上限 / 读入无体积上限

- 旧 `view` 全量返回：超大文件先被外层通用截断器**一刀切截断**
  （截在半行/半个词，且无总行数信息），模型只能盲目重试；
- 一行 minified JS / base64（几 MB 单行）即可吃光预算；
- 任意大小文件都会被 `read_text` 整个读进内存——`bash` 可以轻易造出
  GB 级文件（`fallocate` / `dd`），view 会 OOM 整个进程，**影响所有
  用户的会话**（外层截断只保护输出，不保护读入）。

修复：

- `view` 在工具内部按 **token 预算（默认 20000，与全局
  TOOL_RESPONSE_TOKEN_BUDGET 同口径）沿完整行截断**，尾注给出
  「总行数 + 截断行号 + `view_range=[N, -1]` 续读指引」，模型一轮
  即可精确续读（外层截断器自动成为 no-op）；
- 单行截断到 2000 字符（`…[line truncated]` 标记，Claude Code 同款
  口径），`Latest file snapshot` 同样受益；
- 读入体积上限：view 16MB / edit 64MB（env 可覆盖：
  `TEXT_EDITOR_MAX_VIEW_BYTES` / `TEXT_EDITOR_MAX_EDIT_BYTES` /
  `TEXT_EDITOR_VIEW_TOKEN_BUDGET` / `TEXT_EDITOR_MAX_LINE_CHARS`），
  超限返回可操作错误并指引 bash head/tail/grep。

## 未改动（确认无问题的部分）

- 四命令严格性（`str_replace` 唯一匹配、`create` 拒绝覆盖、错误消息
  的恢复指引闭环）——与 Claude 官方 str_replace_editor 语义一致；
- 路径安全：`_editor_safe_path`（normpath + 遍历/绝对路径/null 字节
  拒绝）与 `_resolve_editor_path`（符号链接逃逸拒绝，现在异常也被
  正确兜底成 `Error:`）本身逻辑正确；
- workspace 锁与原子写（mkstemp + os.replace，保权限）；
- 工具 schema（SEARCH_TOOLS 中的 text_editor 定义）与 MCP
  `workspace.view` / `workspace.edit` spec 一致，无需变更；
- `insert` 的行语义（0 = 文件头、无尾换行补 `\n`、空文件）经
  边界测试全部正确。

## 验证

```
PYTHONPATH=src python scripts/test_text_editor.py        # 新增 56 项断言
PYTHONPATH=src python scripts/test_tool_args_pipeline.py # 既有 64 项，回归通过
PYTHONPATH=src python scripts/test_run_one_gate.py       # 既有 12 项，回归通过
```

新增脚本覆盖：四命令语义与行号 / view_range；CRLF 保真（B 段 9 项，
含 LF 版与 CRLF 版 old_str 双向匹配、insert / create 字节级断言）；
混合行尾透明归一化；路径遍历 / 绝对路径 / 空路径 / null 字节 /
符号链接逃逸不崩溃；bool / 浮点 / None 类型防御；超大文件按行截断 +
续读指引闭环（按尾注的行号回读校验）；单行截断；体积上限；
后台持久化（Task 可等待、content_bytes 直传、R2 key 用户隔离）；
相对路径消息约定；非 UTF-8 拒绝。

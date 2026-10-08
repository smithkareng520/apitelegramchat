# 工具与 MCP 工程审计 — 2026-10-09

## 检查范围

- host 内建工具：`message_user`、`subagent`、`generate_image`、`generate_video`、`present_files`，以及按回合追加的 `deliver_reply`
- 内部 MCP catalogue：`web_search`、`fetch_url`、`wikipedia`、`exchange_rate`、`weather`、`todo`、`memory`、`text_editor`、`bash`
- 外部 MCP：`gaode_mcp`
- 工具装配：`tool_registry.py` / `tool_assembly.py`
- MCP 发现、缓存、调用：`mcp_manager.py` / `mcpserver/catalogue.py`

## 本轮确认并修复

### 1. 可变工具 schema 缓存泄漏
问题：工具定义是深层 dict。旧实现直接暴露缓存对象；任何协议适配器或过滤层改一层嵌套字段，都会影响后续回合。

处理：缓存保留一份内部基准，`get_model_tools()` 与 `list_server_tools()` 对外返回深拷贝。

### 2. MCP disabled_tools 的模块全局状态
问题：`policy.disabled_tools` 原先写入 `_DYNAMIC_DISABLED` 模块全局。重复加载配置后可能残留上一份配置的禁用项。

处理：把禁用集合提升为 `MCPServerConfig.disabled_tools`，状态跟配置实例走。

### 3. 并发上限环境变量读取失效风险
问题：`config.py` 会 scrub 敏感环境变量，MCP 管理器此前从 `os.environ` 直接读取 `EXTERNAL_MCP_MAX_CONCURRENCY`，导致启动时配置读取顺序敏感。

处理：统一使用 `RUNTIME_ENV` 快照。

### 4. Schema 规范化吞异常
问题：`normalize_tool_schema()` 用 blanket `except Exception` 包住整个函数体，结构变化或真实 bug 都可能变成静默 no-op。

处理：显式检查嵌套结构，只有不匹配的 schema 形状才直接返回；真实异常不再被吞掉。

### 5. Internal MCP schema 引用污染
问题：`ToolSpec.as_mcp_tool()` 直接把 catalogue 里的 `inputSchema` 放入 MCP Tool 对象。

处理：在 MCP 边界做深拷贝。

## 设计层面仍值得关注，但本轮未做大规模重构

- `ai_handlers.py`、`responses_bridge.py`、`agentic_loops.py` 等仍存在超长 orchestration 函数。它们更适合单独做阶段化重构与时序回归，不适合在本轮“工具面”优化中机械拆分。
- `mcp_manager.py` 的外部 HTTP 错误分类与重试逻辑已经较完整，但仍依赖不同上游网关真实错误形状；后续可增加脱敏错误分类的 provider fixture 集合。
- 完整 pytest 需要仓库声明的运行时依赖。当前沙箱缺失 `mcp` / `tiktoken` 等包，因此本轮以编译检查 + 关键路径直接回归为主。

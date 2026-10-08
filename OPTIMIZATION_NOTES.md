# Optimization notes — 2026-10-08

本次直接在仓库源码与测试源码上完成优化，没有引入额外运行时依赖，也没有委派子代理。

## 生产代码优化

1. `src/context_manager.py`
   - `_fit_message_to_token_budget()` 从逐 token 递减改为二分搜索。
   - 超大文本的预算拟合从 `O(token_budget)` 次候选检查降为 `O(log token_budget)`。
   - 保持最终 token budget 严格约束及原有纯文本单块截断语义。

2. `src/ai_handlers.py`
   - TIMER 工具面删除“完整工具面先 prioritize、再立刻 restrict”的重复遍历与深拷贝。
   - `restrict_tool_defs()` 保持源工具定义的稳定相对顺序，因此结果行为不变。

3. `src/mcp_manager.py`
   - MCP `list_tools` 增加按 server 的异步刷新锁和双检。
   - 冷启动或 TTL 到期时，同一服务器的并发发现请求只执行一次，其余协程复用刷新结果。

## 单元测试优化

本轮完整检查了 `tests/unit/` 下全部 66 个测试文件（约 1.6 万行、1,400+ 处注释）。

1. 注释 / 文档
   - 为没有模块级说明的测试文件补齐简洁 module docstring。
   - 将原先过长的文件头横幅说明收敛为一句“测试这个什么”，详细约束继续留在对应测试/section 附近。
   - 清理明显过时的“独立脚本运行”说明和无效主程序入口。

2. pytest 可发现性
   - `tests/unit/test_present_files_scope.py` 原本是自定义 PASS/FAIL 计数器脚本，现已接入 pytest。
   - `tests/unit/test_whitelist_r2.py` 原本同样绕过 pytest，现已接入 pytest；测试失败会真正让 pytest 失败，而不是只打印失败计数。
   - 移除了 `test_strip_tool_traces.py` / `test_unified_pipeline.py` 的多余 `__main__` 调试入口。

3. 测试隔离
   - `test_present_files_scope.py` 改为 pytest `monkeypatch` 恢复 HTTP/chat-action patch，并显式使用测试 namespace，避免依赖进程级环境变量。
   - `test_whitelist_r2.py` 改为临时 whitelist 文件，并在测试结束后恢复 admin / whitelist set / R2 IO 函数，避免污染后续测试。
   - `test_mcp_manager_env_snapshot.py` 的并发测试改为原生 async pytest 测试，不再在同步测试里嵌套 `asyncio.run()`。

4. 代码卫生
   - 全量清理 18 个测试文件中的多余 import；当前 AST 检查未发现未使用 import。
   - 编译检查覆盖 `src` + `tests`，当前通过。
   - 保留 3 个较长的真实时序集成式单测，没有为了降低行数进行机械拆分，因为其核心价值就是验证一整条取消/回写/渲染时序。

## 验证

- `python -m compileall -q src tests`：通过。
- 定向测试集：`94 passed, 2 skipped`。
- 2 个 skip 为仓库已有的真实 `tiktoken` 编码表依赖，在离线环境按现有规则跳过。
- 当前执行环境仍缺少仓库声明的部分依赖（例如 `httpx2`），因此无法诚实地宣称完整测试套件全量通过。

# 第二轮工具面与 MCP 稳定性优化 — 2026-10-09

本轮重点检查 `tool_registry`、MCP 工具发现/缓存、内部 MCP schema 与工具 schema 规范化。

## 生产代码优化

1. `src/tool_registry.py`
   - 保留工具总表缓存，但不再把缓存中的可变 dict 直接返回给调用方。
   - `get_model_tools()` 每次返回深拷贝，避免协议转换、工具过滤或测试修改嵌套 schema 后污染后续请求。
   - 增加 `invalidate_model_tools_cache()`，为运行时模型能力/工具配置发生变化时提供明确失效入口。

2. `src/mcp_manager.py`
   - `MCPServerConfig` 新增 `disabled_tools`，将 `policy.disabled_tools` 从模块级全局状态改为服务器实例状态。
   - 修复重复加载不同 `mcp.json` 时旧禁用名单可能串到新配置的问题。
   - `list_server_tools()` 对 TTL 缓存做深拷贝保护，调用方无法反向修改 manager 内部缓存。
   - `EXTERNAL_MCP_MAX_CONCURRENCY` 改为从 `RUNTIME_ENV` 启动快照读取，修复 secret scrub 后从 `os.environ` 读取导致配置可能失效的问题。
   - 修正 stdio keeper 异常日志前缀中的明显笔误。

3. `src/mcpserver/catalogue.py`
   - `ToolSpec.as_mcp_tool()` 对 `inputSchema` / `input_examples` 做深拷贝，避免 MCP SDK 调用方修改 catalogue 中的单例 schema。
   - 将原先过宽的 `except Exception` 收紧为 SDK 属性/类型兼容性相关异常。

4. `src/tool_assembly.py`
   - `normalize_tool_schema()` 去除会吞掉真实编程错误的 blanket `except Exception`。
   - 改为逐层检查工具 / function / parameters / properties 的运行时形状；格式异常时保持原 schema，不再静默掩盖其它 bug。

## 新增回归覆盖

- MCP `disabled_tools` 配置隔离。
- MCP 工具 TTL 缓存返回值隔离。
- `EXTERNAL_MCP_MAX_CONCURRENCY` 使用启动环境快照及 1..8 边界。
- host 工具总表深拷贝隔离。
- host 工具总表缓存显式失效。

## 当前验证

- `python -m compileall -q src tests`：通过。
- 对关键变更执行独立直接回归脚本：通过。
- 当前执行环境未安装仓库声明的完整依赖（包括 `mcp`、`tiktoken` 等），因此没有把无法在本环境完成的 pytest 全量结果冒充为通过。

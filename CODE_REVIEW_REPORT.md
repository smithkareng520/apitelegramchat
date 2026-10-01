# Code Review 报告（第二轮）：AI 响应处理 / 代码质量 / 工程规范

项目规模：`src/` 约 200 个 Python 文件、7.6 万行；`src/ai/` 约 1.8 万行。上一轮已修复 Responses bridge 的 function-call 终态参数覆盖问题并加入 `ai/streaming.py`。
本轮先用真实依赖跑通完整测试（707 个用例），再逐个 bridge 核对上一轮遗漏的部分。

> **验证口径**：沙箱无法下载 tiktoken 编码文件，用近似编码器替代后，有 6 个依赖精确 token 数的用例失败（`test_token_budget` ×3、`test_draft_manager` ×2、`test_fetch_rich_content` ×1）。
> 修改前后失败集合完全相同，无新增失败。这 6 个用例请在能联网的 CI 上确认。真实 provider 网关与 Telegram 线上链路未做端到端验证。

---

## 一、AI 响应处理与健壮性

| # | 问题 | 原因 | 处理 |
|---|---|---|---|
| 1 | **应用层流超时只覆盖 Responses**，openai-compat、Anthropic、Gemini 三条主流及 4 处“总结流”均无 idle/total 闸门 | 上一轮只改了 `responses_bridge`。底层 read timeout 是“字节间隔”，网关持续发 SSE 心跳注释行即可使其永不触发，回合会一直占用连接和 chat 锁 | 5 处主流 / 合成流统一改为 `iter_async_stream(...)`，默认 idle 300s / total 1800s，环境变量可调，`0` 关闭 |
| 2 | **Gemini 流被 `total=300s` 误杀** | `aiohttp.ClientTimeout(total=300)` 作用于整个响应体，长思考 / 长输出在第 300 秒必然中断，即使事件一直在流动 | 流式请求改为 `total=None`，总时长交由应用层 deadline；`sock_read=180` 保留作“连接静默”兜底 |
| 3 | **Gemini SSE 单行超 128 KiB 直接崩溃** | aiohttp 按行读取，行长上限为 `2×read_bufsize`，默认 128 KiB。大参数 `functionCall`（如 text_editor 写整文件）会抛 `ValueError: Chunk too big`。已用本地 SSE 服务复现 | `read_bufsize` 提到 2 MiB（行上限 4 MiB），并加回归测试 |
| 4 | **超时后底层连接未关闭** | 旧 helper 只对迭代器调用 `aclose()`；OpenAI/Anthropic SDK 的流对象需要 `close()` 才会释放 HTTP 连接，超时后连接滞留在池里 | `_close_quietly` 依次关闭迭代器与底层流，兼容同步 / 异步 `close`，且对同一对象不重复关闭 |
| 5 | **清理阶段吞掉 `CancelledError`** | 旧 `finally` 里 `except (Exception, asyncio.CancelledError): pass`，流正常结束时若恰好收到取消，取消请求会丢失，任务继续跑 | 只吞 `Exception`，取消信号放行 |
| 6 | **超时类型误判** | 旧代码把任何 `asyncio.TimeoutError` 都标成 idle/total；流自身抛出的 `TimeoutError`（如 aiohttp `sock_read`）会被错误归类；idle 与 total 同时逼近时标签也可能错 | 只有实际等待时间达到闸门值才视为应用层超时，其余原样上抛；标签由触发它的那个闸门决定 |
| 7 | **超时后的重试语义缺失** | compat 循环只对 `httpx.ReadTimeout` 做“零输出补试一次”，Anthropic 的 `_is_retryable_stream_error` 不认识新超时 | 二者都纳入 `AIStreamTimeoutError`；仍严格限定零输出阶段，已有增量则直接抛出，不重放半个回合 |
| 8 | **非流式回退直接 `resp.choices[0]`** | 网关内容审核 / 上游故障常返回 200 + `choices: null`，只会得到无信息的 `IndexError`/`TypeError`，上游错误信息丢失 | 新增 `ai/errors.py::first_choice`，抛带上游 `error` 的 `AIResponseParseError`；compat 回退与图片生成两处接入（subagent 已有保护） |

新增领域异常集中在 `ai/errors.py`（`AIStreamTimeoutError`、`AIResponseParseError`）。

## 二、代码质量与冗余清理

- **已清理**：`agentic_loops.py` 中无人使用的 `TIMEOUT` 导入；`_constants.TIMEOUT` 改为语义明确的 `STREAM_CLIENT_TIMEOUT` / `STREAM_READ_BUFSIZE`；`responses_bridge` 中冗余的超时参数透传；两处长达 6–8 行的叙事性注释压缩为一到两行结论。
- **构建产物混入源码**：`__pycache__/*.pyc`、`src/apitelegramchat.egg-info/` 已删除，新增 `.gitignore` 与 `.dockerignore`。
- **`pyproject.toml` 的 `py-modules` 清单失真**：残留不存在的 `mcp_client`，同时漏列 `bash_background`、`mcp_manager`、`tool_names`、`tool_registry`。已按磁盘重新生成，并加 `test_packaging_manifest.py` 防止再次漂移。
- **依赖双写**：`requirements.txt` 与 `pyproject.toml` 各自维护同一份依赖。建议以 `pyproject.toml` 为唯一来源，`requirements.txt` 由工具生成（本轮未动，属流程变更）。

### 评估后**有意保留**的内容

- `ai/json_repair.py` 的自研修复状态机：虽然 `json-repair` 已是必装依赖，但这段扫描器同时承担“截断安全预检”（拒绝执行被截断的命令参数，如 `rm -rf /tmp/ju`），不是单纯的兜底。若要删除，应先用 stdlib `json.JSONDecoder.raw_decode` 重写截断判定并补齐用例，再去掉库缺失分支。
- 各 provider 的 fallback 路径：上一轮报告已指出，删除顺序应由线上命中率决定，本轮无日志数据，不做删除。

## 三、架构与工程规范（仅评估，未重写）

| 文件 | 函数 | 约行数 |
|---|---|---:|
| `ai_handlers.py` | `get_ai_response` | 878 |
| `app.py` | `process_update` | 699 |
| `ai/agentic_loops.py` | `_agentic_loop_openai_compat` | 697 |
| `ai/tool_call_loop.py` | `_run_tool_calls_and_append` | 652 |
| `ai/responses_bridge.py` | `_agentic_loop_openai_responses_impl` | 502 |

1. **四条 agentic 循环同构重复**。流式累积、live slot 同步、think 标签分区、tool-call 累积在 compat / Anthropic / Gemini / Responses 中各写一遍。本轮的超时接入需要改 5 处，就是这种重复的直接代价。建议抽出“事件归一化 → 统一累积器”，各 bridge 只保留协议差异。
2. **顶层目录扁平**：`src/` 下 60+ 个顶层模块（`app_*`、`tool_*`、`bash_*`、`file_*`），与已分包的 `ai/`、`core/`、`protocols/` 风格不一致，建议按 `app/`、`tools/`、`sandbox/` 归组。这会改动所有 import 及 `pyproject`，需单独一轮并配合 mypy。
3. **异常口径**：全仓大量 `except Exception`。建议按“必须上抛（provider 失败 / 超时 / canonical history 写入失败 / 取消）”与“可降级（telemetry / 缓存诊断 / UI flush）”两类整理，并逐步用 `ai/errors.py` 的领域异常替换对 SDK 异常类的直接依赖。
4. **注释密度**：核心循环里存在大量“事故复盘式”长注释（改动点编号、v2.x 版本标记）。这类背景应进 `docs/` 或提交信息，代码里只留不变量与约束。本轮只处理了改动区域，全量整理需要逐文件审阅。

## 四、本轮变更清单

**新增**：`src/ai/errors.py`、`tests/unit/test_stream_robustness.py`、`tests/unit/test_packaging_manifest.py`、`.gitignore`、`.dockerignore`
**修改**：`src/ai/streaming.py`、`src/ai/agentic_loops.py`、`src/ai/anthropic_bridge.py`、`src/ai/gemini_bridge.py`、`src/ai/responses_bridge.py`、`src/ai/media_generation.py`、`src/ai/_constants.py`、`tests/unit/test_ai_streaming.py`、`pyproject.toml`、`render.yaml`
**删除**：所有 `__pycache__`、`src/apitelegramchat.egg-info/`

## 五、建议的下一步（按收益排序）

1. 在能联网的 CI 中确认 6 个 tiktoken 相关用例，并把 `pytest` + `mypy` 设为必过。
2. 抽出统一的流式累积器，消除四条循环的重复（风险最高、收益最大，需分 provider 逐条迁移并保留现有 `test_interrupt_preservation` 等用例）。
3. 补 Responses 的边界用例：流结束但无 `response.completed`；`response.failed` 且已有部分正文；工具参数在 token 上限处截断。
4. 线上采集 fallback 命中率后，再决定历史兼容分支的删除清单。

## 六、2026-10 Responses 网关兼容性修复

线上日志对应的问题是：Responses 主回合成功返回 `function_call` 后，
工具执行完成，bridge 已生成合法的 `function_call_output`；兼容网关随后
在自身 `/responses` 转换层将该 item 处理为空，并返回
``400 input must be non-empty``。这不是本地发送 `input=[]`。

本轮在 `responses_bridge.py` 增加受限恢复：

- SDK 调用前仍严格禁止空 `input`；
- 仅针对 `function_call_output` continuation + 已有
  `previous_response_id` + HTTP 400 且错误明确为 `input must be non-empty`
  触发；
- 每个被拒绝的工具 continuation 只重试一次；恢复成功后立即回到正常 chain；
- 清除 pending continuation 与 server-managed chain，改走 Responses
  canonical-history bootstrap；
- bootstrap 输入包含原始 assistant `function_call` 快照及工具结果
  `function_call_output`，不伪造文本；
- 成功后重新提交新的 response id 为链头；
- 普通 400、stale response、同一 continuation 的重复失败不无限重试；后续独立 continuation 可各自获得一次恢复。

该策略保持官方 Responses 的正常路径不变，只把兼容网关错误作为异常恢复。
OpenAI 官方 Responses API 文档入口： https://platform.openai.com/docs/


## 七、2026-10-01 Responses 链状态机再修复：新轮次/多工具轮次掉链

线上日志暴露出上一版兼容补丁还有两个状态机错误：

1. **兼容恢复是整轮级别，而不是单个 continuation 级别。**
   `tool_continuation_retried` 挂在 `_TurnSyncContext` 上。第一条
   `function_call_output` 被网关以 `input must be non-empty` 拒绝后，该标记永久为
   true；同一 agent turn 后面的第二个工具续轮即使再次被同一网关拒绝，也不会再恢复。
2. **canonical bootstrap 成功后没有重新进入 chain mode。**
   恢复分支把 `sync_ctx.mode` 设成 `bootstrap`，但收到新的
   `response.completed` 后只更新了 `response_id`，没有把 mode 切回 `chain`。
   因而后续工具轮次会继续走全量 replay；而原兼容分支又要求
   `mode == chain` 才允许恢复，形成“第一次能救、第二次必掉”的状态死路。

### 修复

- 删除 turn-wide `tool_continuation_retried` 状态。
- 每个 `for _round` 的实际工具 continuation 请求建立独立的
  `tool_continuation_retried = False`，因此**每个被拒绝的 continuation 最多
  恢复一次**，多个工具调用不会共享恢复额度。
- 任意成功的 `response.completed`（包括 bootstrap/recovery response）拿到有效
  `response.id` 后，立即把当前回合状态提升为：
  `mode="chain", response_id=<completed response id>`。
- 因此恢复仍然只使用 `/v1/responses`，不是切换到 Chat Completions 或其他协议；
  recovery 成功后下一次 continuation 又回到官方
  `previous_response_id + function_call_output` 形状。
- 新增回归测试覆盖：
  **function_call → 400 → bootstrap function_call → 400 → bootstrap final**，
  验证两个 continuation 都各自得到一次恢复，且最终链头正确提交。

### 验证口径

本环境已通过：

- `python -m compileall -q src tests`
- `tests/unit/test_responses_state.py`
- `tests/unit/test_responses_tool_continuation.py`
- `tests/unit/test_response_protocol.py`

完整 `test_responses_chain_bridge.py` 无法在当前沙箱直接导入，因为镜像缺少
生产依赖 `tiktoken` / `httpx2`；不是代码测试失败。该套件的现有测试在源码包内仍保留，
并新增了上述多 continuation 回归测试，建议在部署 CI/运行环境中执行完整套件。

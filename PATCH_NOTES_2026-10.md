# Responses 网关兼容性补丁

## 修复
针对线上 `400 Error from provider (Console): input must be non-empty`：
1. 正常 Responses 工具续轮继续使用 `previous_response_id` +
   `function_call_output`。
2. SDK 调用前禁止 `input=[]`。
3. 如果兼容网关明确以 HTTP 400 报告 `input must be non-empty`，且本次请求
   是合法的工具续轮，则只恢复一次。
4. 恢复时清掉 `previous_response_id` 和 pending continuation，完整重放
   canonical history（assistant function_call + tool result）到 Responses。
5. 成功后新的 response id 重新成为链头。

## 为什么这样修
日志显示本地 bridge 已经识别到 1 个 `function_call_output`，但 provider
仍报告 input 为空。因此单纯增加“空 input 检查”无法解决问题；需要兼容
网关内部对原生 Responses tool continuation 的错误转换。

## 验证
- `python -m compileall -q src tests`：通过。
- 新增回归测试：provider 在合法 `function_call_output` 续轮返回该 400 时，
  只 bootstrap 一次，并验证重放包含 function_call 与 function_call_output。
- 本沙箱缺少项目运行依赖 `tiktoken`、`httpx2`，因此无法在此环境完成
  完整 pytest 导入链；这不代表测试失败，属于测试环境依赖缺失。


## 512MB cgroup 内存压力修复（2026-10-01）

生产日志显示主进程 RSS 约 228MB，但 cgroup `memory.current` 从 370MB
持续升到 473MB、511.9MB/512MB，随后服务在 09:17:50 重启并恢复到约
129MB。结合 MCP 调用轨迹，根因是每个内部 MCP 模块都按 chat 常驻一个
独立 Python stdio 子进程：`search / todo / memory / workspace / bash`
会重复加载 Python + MCP + 模块依赖，主进程与这些子进程的内存共同计入
同一个 cgroup。

修复：
- 新增受信 `in_process` MCP transport；
- 内置五个模块改为在 host asyncio 进程内调用同一份
  `mcpserver.catalogue.ToolRegistry`；
- mutating 工具通过显式 `allow_mutations=True` 授权，不依赖修改宿主
  `os.environ`；
- `bash` 工具仍通过现有 Landlock + 独立进程组沙箱执行实际命令，命令隔离
  没有取消；
- 外部 `streamable_http` MCP 和未标记为 `in_process` 的 stdio MCP 行为不变；
- 工具 schema 装配同时支持 `in_process`，避免重复 transport 探测。

验证：
- `python -m compileall -q src`：通过；
- `mcp.json` 已确认五个内部 MCP 均切换到 `in_process`；
- 完整 pytest 无法在当前构建环境收集，原因是缺少 `tiktoken` 运行依赖。

## 第三轮：AI 响应健壮性 + 死代码清理 + 工程规范（2026-10-01）

### 一、AI 响应处理与健壮性

1. **`ai/streaming.py` 超时闸门关闭连接对 openai SDK 流对象失效**
   `_close_quietly` 旧逻辑对迭代器只尝试 `aclose()`，而 openai SDK 的
   流对象 `__aiter__` 返回 `self`、只有同步 `close()`——同一对象因
   `stream is iterator` 又不追加 stream target，导致超时后连接滞留
   连接池。修复：迭代器同样依次尝试 `("aclose", "close")`，awaitable
   结果照常等待；不同对象两边各自清理，同一对象不重复。

2. **`ai/agentic_loops.py` 首增量零输出重试从未生效**
   openai SDK 3.x 的传输层是 httpx2（api_client 构造客户端时已传
   `httpx2.Timeout`），流式迭代抛出的读超时是 `httpx2.ReadTimeout`，
   与 `httpx.ReadTimeout` 互不为子类——旧 `except (httpx.ReadTimeout,
   AIStreamTimeoutError)` 永远捕不到。新增模块级
   `_STREAM_READ_TIMEOUT_ERRORS = (httpx2.ReadTimeout, httpx.ReadTimeout,
   AIStreamTimeoutError)`，语义不变（仅零输出阶段补试一次）。

3. **`ai/gemini_bridge.py` 流中 error / 安全拦截被静默吞掉 + 无零输出重试**
   - `_iter_gemini_stream_events` 现在产出 `kind="error"` 事件：
     `{"error": {...}}` chunk 与 `promptFeedback.blockReason` 安全拦截
     （两者都表现为"无 candidates"，旧逻辑直接 continue，调用方只见
     空流）。主循环将其转成 `AIResponseParseError` 上抛（安全拦截是
     确定性结果，不做重试）。
   - 主流轮增加零输出瞬态重试：连接类异常（`aiohttp.ClientError` /
     `asyncio.TimeoutError`）且尚未收到任何事件时 1s 后补试一次；
     已有增量绝不重放半个回合。与 openai_compat 循环同语义。

4. **`ai/responses_bridge.py` 合成总结流丢弃 dict 形状事件**
   `_synth_stream` 改用 `event_type()` / `event_field()` 读取事件，
   与主流消费循环同一协议；修复前 `getattr` 会把 dict 形状事件整体
   丢弃，超限总结流静默变空。

5. **`ai/agentic_loops.py` 非流式回退不重置 `reasoning_acc`**
   死流（只产出思考、无正文/工具调用）触发非流式回退时，已累积的
   半截思考会随 `live_slot.finalize` 与回退正文合并成同一条历史消息。
   修复：进入回退分支时清空 `reasoning_acc` 并复位 `in_reasoning`。

6. **`ai/media_generation.py` chat modalities 图片下载无体积上限**
   新增共用 `_read_remote_image_capped()`（Content-Length 预检 +
   `readany()` 循环限读，25MB 上限），三处生成结果图片下载统一接入；
   修复前 chat modalities 提取路径（原 :2537）裸 `resp.read()`，
   恶意 upstream 可用超大响应拖垮进程。

### 二、代码质量（全部经全仓零引用验证后删除/收敛）

- 删除 `ai/tool_summary.py` 的 bash-127 死代码簇（约 80 行：
  `_bash_cmdnotfound_is_partial` / `_bash_leading_command` 及 4 个
  配套正则/常量）。
- 删除 `tool_registry.py` 三个死函数（`invalidate_tools_cache` /
  `restrict_to` / `tool_def_names`）与 `NAMES` 别名，README 同步更新。
- 删除 `skills.py` 死函数 `_iter_skill_files` / `catalog_text`；
  `workspace_utils.py` 死函数 `_restore_skills_snapshot_from_r2`。
- 删除死常量：`SAFE_BOUNDARY_EVENTS`（draft_manager）、
  `DELIVER_REPLY_TOOL`（tool_schemas）、`ASK_USER_TOOL`
  （message_user_tool）、`PROACTIVE_DAILY_MESSAGE_LIMIT`（proactive，
  已废弃无消费方）、`_SMART_AMP_PATTERN`（text_utils，连同 rich_media
  的死导入）、`_CACHE_MAX_EXPLICIT_MARKS`（attachment_content）、
  `_REMOVED_TOOL_HINTS`（file_delivery）。
- `ai/schema_validation.py`：jsonschema 通过后不再双跑内置校验器——
  `_iter_jsonschema_errors` 以 `None` 区分"内部异常需兜底"与"真实
  通过"，单一判定标准。
- `_Simple*` 五个模拟响应类从 anthropic_bridge / responses_bridge
  逐字重复的两份收敛为 `bridge_common.py` 的 `Simple*` 单份实现。
- `ai/cache_usage.py`：`_num` 与缓存命中字段提取逻辑（dict/pydantic
  两形状）收敛为 `_extract_cached_tokens()` 单一实现，`_cached_from_usage`
  变为薄别名。

### 三、工程规范

- `pyproject.toml` `py-modules` 补上 `runtime_diagnostics`（打包清单
  测试 `test_packaging_manifest` 此前因此失败——基线唯一红）。
- `tests/integration/conftest.py` 移除对 `APITELEGRAMCHAT_WORKSPACES_DIR`
  的重复 `setdefault`（根 conftest 按目录字母序先执行，那里的
  setdefault 永不生效，属静默冲突）；工作空间隔离只认根 conftest
  一个来源。
- 四个位于 `tests/` 根的测试文件归位 `tests/unit/`（test_cjk_font /
  test_emoji_font / test_workspace_prompt_namespace / test_whitelist_r2），
  并修正归位后的项目根定位（`parents[1]` → `parents[2]`）。

### 四、验证

- 全量 pytest：**723 passed**（基线 711 passed + 1 failed → 现全绿，
  含新增 `tests/unit/test_round_fixes_regression.py` 10 个回归用例：
  self-iterator 流清理 ×3、httpx2 重试判定、gemini error/安全拦截 ×2、
  dict 事件读取、25MB 上限 ×3）。
- `python -m mypy`：错误集合与基线完全一致（仅行号平移），零新增。
- 独立自检脚本 `python3 tests/unit/test_whitelist_r2.py`：89 通过 0 失败。


## 工具结果「模型视图」去 JSON 化 + 折叠块状态补齐（2026-10-02）

### 一、工具返回不再给模型发 JSON

工具的原始返回同时服务两个消费者：UI 卡片渲染（依赖结构化 JSON）与
模型上下文（role=tool 消息）。此前模型视图只是「过滤后的 JSON」——
结构开销大、无关字段多，模型真正需要的答案信息被稀释。本次把模型视图
升级为按工具定制的紧凑纯文本（JSON 只属于 UI）：

- `weather`：当前实况一行 + 逐时（hours 参数控制，默认 6 条，含省略
  计数）+ 逐日，只保留温度/天气/降水/湿度/风力/UV 等高价值字段；
- `todo`：清单逐条 `[ ]/[x] [优先级] 标题（id=…，截止…，#标签）`+
  备注 + 统计；写动作返回「已添加/已完成/已删除…（id=…）+ 统计」——
  id 保留（后续 done/delete/edit 的句柄），created_at/completed_at/
  changed/ok 等内部字段不再发给模型；
- `memory`：检索/保存/更新/删除同样一行化，只保留 id、内容、分类、
  重要度、标签；
- `subagent`：「子 agent 完成（模型 x · n 轮 · m 次工具调用 · 用时
  ts）+ 最终答复正文」；任务回声 task_preview、展示名 model_name、
  内部码 ok/code 不再重复给模型；
- `message_user`：用户回答转一句话（选了什么/自定义回答/取消/超时），
  不再回 JSON 信封；
- `present_files`：「已发送 n 个文件：名字」+ 失败清单；
- 高德 maps_*：POI 列表/地理编码/路线步骤/距离表/IP 归属地全部转
  可读文本——polyline/tmcs/photos/行政区划内部编码（adcode/pcode/
  citycode/gridcode 等）/typecode 先清洗再转文本，坐标压缩到 4 位
  小数；POI 保留 id（maps_search_detail 需要）；status=0 返回
  「失败：…（info 原因）」。

安全性约定不变：错误/超时文本逐字保留（熔断前缀匹配依赖）；JSON
错误信封 `{"error": …}` 转写为「失败：…」；任何解析失败原样透传；
精简失败退回完整内容。

### 二、源头瘦身：工具不再生产无价值字段

`execute_weather`（wttr.in）此前逐时复制 25+ 字段 ×24h（DewPoint/
HeatIndex/WindChill/shortRad/diffRad、十项 chance_* 分类、气压/阵风/
云量/能见度/UV …），绝大多数既不出现在用户卡片、也早被模型视图丢弃。
现在工具只生产 UI 卡片与模型视图真正消费的字段（
`_HOURLY_FIELDS` / `_DAILY_FIELDS` 契约），UI 卡片同步移除「天文 &
其他概率」「逐时额外数据」两张嵌套表（逐时主表补「降水概率」列）。

### 三、折叠块/折叠组状态文案补齐（对标 Claude Code）

进行态（此前笼统或缺失）：
- `weather` → "Fetching weather for 北京"；`exchange_rate` →
  "Checking exchange rate: USD → CNY"；`wikipedia` → "Looking up
  可塑性记忆 on Wikipedia"；`subagent` → "Running a subagent: 任务
  摘要"；`generate_video` → "Generating a video: 提示词"；
- `bash` 后台任务此前与前台命令共用 "Running command"：现在区分
  "Starting background: …" / "Checking background task" /
  "Listing background tasks" / "Stopping background task"；
- `maps_ip_location` 此前落到通用 "Running..."：现在 "Locating IP
  origin"；路线/距离工具标题带起讫点（坐标压缩 4 位小数）；
- `present_files` → "Presenting n files: 文件名"。

完成态（此前只有笼统 "Fetched weather" 等）：
- `weather` → "Fetched weather: 北京 25°C 多云"；`exchange_rate` →
  "Checked exchange rate USD → CNY: 7.2400"；
- `subagent` → "Ran a subagent (3 rounds · 5 tool calls · 42s)"，
  失败 → "Ran a subagent (failed)"；
- 路线/距离 → "Planned a driving route · 12.3 km · 25 min"（公交
  只有时长）；`maps_search_detail` → "Fetched POI details: 名称"；
- `present_files` → "Sent n files (名字…), m failed"；
- `bash` → "Checked a background task" / "Started a background
  command" 等。

工具组（折叠组）：
- 组标题与单条目同规范：weather/exchange_rate/wikipedia/subagent/
  present_files 的组标题直接带查询对象；
- 单条目组（1 成功 0 失败）外层标题复用条目详情摘要，覆盖面从 4 种
  地图工具扩大到 weather / exchange_rate / fetch_url / wikipedia /
  subagent / maps_direction_* / maps_distance / maps_search_detail /
  maps_ip_location；
- `_GROUP_SUMMARY_TEMPLATES` 补 `maps_ip_location` 模板。

### 四、验证

- 全量 pytest：**783 passed, 2 skipped**（基线 757 → 现 +26：重写
  `test_tool_result_condense.py` 34 例（纯文本视图）、新增
  `test_model_view_and_tool_status.py` 12 例（状态文案回归 + weather
  载荷契约 + UI 卡片兼容））；
- 冒烟验证：weather/todo/memory/subagent/message_user/present_files/
  maps_geo/text_search/direction 的模型视图输出逐字段核对。

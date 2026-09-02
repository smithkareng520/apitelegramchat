# 工具参数处理的主流四层管线（Mainstream Tool-Args Pipeline）

## 背景

生产日志（agnes 网关）连续多轮出现：

```
WARNING 第 1 轮检测到 1 个无法自动修复的工具参数 JSON，已写入带诊断的可恢复错误并阻止其污染下一轮请求
INFO    第 2 轮模型原始返回: tool_calls=1, names=['bash'], content_len=0
WARNING 第 2 轮检测到 1 个无法自动修复的工具参数 JSON …
```

旧方案（v2.3 Self-Correction 增强）是**单点自动修复**：自研 700 行语法
状态机修不动就把诊断信封回传。两层结构性问题：

1. **自研修复器覆盖面有限**——任何自研状态机都追不上真实模型产出的
   畸形 JSON 长尾分布（这正是日志里连续多轮"无法自动修复"的原因）；
2. **修复成功 ≠ 参数正确**——`json.loads` 只保证"语法是 JSON"，
   缺必填字段、类型错误、枚举外取值要到执行器里才暴露，模型拿到的
   失败反馈与"参数写坏了"完全脱钩；
3. **没有预防层**——从未尝试从解码源头约束模型产出合法 JSON。

## 什么是"主流做法"

各主流 agent 框架（OpenAI Agents SDK / LangChain StructuredTool /
LlamaIndex / Anthropic 官方 tool-error 处理指引）在"模型工具参数不可
靠"这件事上的收敛模式是**四层防御纵深**，而不是单点修复：

| 层 | 职责 | 主流实现 |
| --- | --- | --- |
| **L0 预防** | 从源头让坏 JSON 几乎不可能产生 | OpenAI Structured Outputs：工具 schema 注入 `strict: true`（约束解码），网关不支持时自动降级 |
| **L1 语法修复** | 坏 JSON 修得回来就直接用，省一轮重试 | `json-repair` 社区标准库（百万级月下载，LangChain/LlamaIndex 同款路线），自研只做兜底 |
| **L2 语义校验** | 修回来的 JSON 必须符合工具 schema | `jsonschema` 按真实 schema 校验（必填/类型/枚举），失败不执行、错误回传 |
| **L3 自纠反馈** | 所有失败以可操作错误回传模型自纠，且有熔断 | v2.3 的诊断信封 + 连击熔断保留并扩展到 L2 错误 |

四层全部可独立降级：strict 被网关拒 → 原始 schema；库未安装 → 自研
兜底引擎；jsonschema 缺失 → 内置轻量校验器。任何一层异常都不阻塞
主流程。

## 处理链全貌

```
模型返回 tool_calls
        │
        ▼
_normalize_tool_arguments（tool_summary.py，三条循环共用）
    1. json.loads 成功且为 object ──────────────► 直接重序列化 ✚
    2. 语法坏 → repair_json_arguments（json_repair.py）
         ├─ 截断安全预检（执行路径绝不猜测补全）──► 拒绝，诊断信封
         ├─ 引擎1：json-repair 社区标准库 ────────► 修复 dict ✚（附透明提示）
         └─ 引擎2：自研保守状态机（兜底）─────────► 修复 dict ✚ / 失败 → 信封
        │
        ▼
_run_tool_calls_and_append → run_one（tool_call_loop.py，执行咽喉）
    3. L2 normalize_and_validate（schema_validation.py）
         ├─ strip_null_arguments：strict 的 null = 未提供 → 剥掉
         ├─ coerce_common_slops：字符串布尔/数字 → 按 schema 无损矫正
         ├─ jsonschema 校验（宽容未知额外键）
         │    失败 ──► 不执行，可操作错误作为 tool 结果回传模型自纠 ✚
         └─ 通过 ──► dispatch_tool_call 正常执行
    4. 诊断信封 → invalid_arguments_message 渲染（L3，原有能力保留）
    5. 同签名错误连续 3 次 → 熔断注入"换策略"指令（TOOL_ERROR_STREAK_LIMIT）
```

请求侧（L0，agentic_loops.py）：

```
tools ──► strict_tools_for_request(api_label, tools)   [strict_tools.py]
            ├─ 逐工具判定能否安全规范化（union type / 根 anyOf 等保守跳过）
            ├─ 能：深拷贝 + strict:true + 递归规范化
            │     · 每层 object: additionalProperties=false
            │     · required 覆盖全部属性（strict 硬性要求）
            │     · 原可选属性 → 可空 type:[T,"null"]（模型发 null=未提供）
            │     · 剥除 default / input_examples（strict 不支持的字段）
            ├─ 不能：原样透传（strict 是 per-tool 的，可混用）
            └─ 网关 400 且报错指向 schema/strict → mark_strict_tools_rejected
               （进程内记忆）→ 摘除 strict 用原始 schema 立即重试一次
```

## 新增/修改文件

| 文件 | 改动 |
| --- | --- |
| `src/apitelegramchat/ai/strict_tools.py` | **新增**：L0——strict 注入、递归 schema 规范化（深拷贝，绝不修改原始列表）、网关拒绝运行时降级、`DISABLE_STRICT_TOOL_SCHEMA` / `ENABLE_STRICT_TOOL_SCHEMA_GEMINI` 开关 |
| `src/apitelegramchat/ai/schema_validation.py` | **新增**：L2——null 剥离、字符串布尔/数字容错、jsonschema 校验（anyOf 子错误钻取成字段级提示）+ 内置轻量兜底校验器、可操作错误消息（`Error:` 开头、参与熔断签名） |
| `src/apitelegramchat/ai/json_repair.py` | **修改**：L1 双引擎——截断安全预检 → json-repair 社区库优先 → 自研状态机兜底；信封/消息渲染（L3）不变 |
| `src/apitelegramchat/ai/agentic_loops.py` | **修改**：OpenAI 兼容循环注入 strict（流式 + 非流式回退均带 BadRequest 自动降级重试）；Gemini 循环默认不注入（env 实验）；两条循环把 tools 透传给执行层供 L2 校验 |
| `src/apitelegramchat/ai/tool_call_loop.py` | **修改**：`_run_tool_calls_and_append` 新增 `tools` 参数（默认 None 向后兼容）；`run_one` 分发前接 L2 闸门；旁路路径复用主规范化链 |
| `src/apitelegramchat/ai/anthropic_bridge.py` | **修改**：把 tools 透传给执行层（L2 对 Anthropic 路径同样生效；strict 不适用原生 Messages API） |
| `src/apitelegramchat/ai/tool_summary.py` | **修改**：摘要生成器类型防御（修复 `command: 42` 这类合法 JSON 错类型参数在 L2 拦截**之前**就打崩 UI 摘要的既有 bug） |
| `src/apitelegramchat/search_engine.py` | **修改**：bash 工具显式声明 `required: ["command"]`（功能必填，让 L0/L2 都能正确约束） |
| `requirements.txt` / `pyproject.toml` | **修改**：新增 `json-repair>=0.30,<1`、`jsonschema>=4.23,<5`（均为可选依赖：缺失时自动退回内置兜底，不阻塞启动） |
| `scripts/test_tool_args_pipeline.py` | **新增**：管线全量验证（64 项断言） |
| `scripts/test_run_one_gate.py` | **新增**：执行闸门集成验证（12 项断言，模拟完整工具批次） |

## 关键设计决策（为什么这样是"主流"）

1. **修复引擎用社区标准库而不是继续自研**——`json-repair` 是这个
   问题上事实上的社区标准（被 LangChain / LlamaIndex / 各家 agent
   框架采用）。自研状态机保留为兜底引擎，两者共享同一套截断安全
   闸门与透明提示机制。实测：日志同款失败（字符串内未转义双引号）
   现在由库引擎稳定修复。

2. **截断安全约束保留且置于库之前**——json-repair 默认会补全截断
   的 JSON（`{"command": "rm -rf /tmp/ju` → 闭合字符串），但"猜补全
   再执行"对命令类参数是危险的。安全预检（现有扫描器）先判定截断，
   命中即拒绝进入执行，无论后续引擎是谁。展示性路径
   （`allow_close_truncated=True`，UI 预览）不受限。

3. **strict 是 per-tool 的 best-effort**——bash / text_editor 等结构
   简单的核心工具获得 strict；web_search（union type `["string",
   "array"]` + 根 `anyOf` 二选一）无法在不改变语义的前提下规范化，
   原样发送。OpenAI 允许一个请求混用 strict 与非 strict 工具。

4. **可选字段的 strict 表达 = 可空类型**——strict 硬性要求 required
   覆盖全部属性，原本可选的属性改为 `type: ["T", "null"]`；模型发
   null 表示"未提供"，执行层 `strip_null_arguments` 剥掉后走 executor
   的 `.get(k, default)` 默认值，语义零损失（键存在但为 None 时
   `.get` 不会取默认值，必须先剥——这正是 strict 模式落地的关键
   细节）。

5. **网关兼容靠运行时降级而不是配置猜测**——聚合网关（agnes /
   openrouter / modelscope / glm…）对 strict 支持参差不齐。首请求
   带 strict 被 4xx 拒且报错指向 schema/strict 时：标记该 api_label
   （进程内记忆）→ 摘除 strict → 原始 schema 立即重试一次 → 之后
   所有轮次不再尝试。误判代价 = 一次额外请求（报错不指向 schema 则
   原样抛出，不降级）。

6. **未知额外键宽容**——项目约定模型可在参数里带 `_description` /
   `_summary` 等展示键，主流实践也只校验"声明的字段是否符合声明"，
   不惩罚额外字段。jsonschema 校验跳过 `additionalProperties` 类错误。

7. **历史容忍写法先矫正再校验**——字符串布尔（`"true"`）与无歧义
   数字字符串（`"30"`）按 schema 声明无损矫正后直接分发（与
   deliver_reply 原有的字符串布尔容错语义一致），省一轮模型重试；
   其余类型错误才拦截回传。

8. **Gemini 默认不注入 strict**——Google 的 OpenAI-compat 层未在
   官方文档承诺 strict 支持，且现有 schema 在该端点稳定工作；默认
   零风险，实验入口 `ENABLE_STRICT_TOOL_SCHEMA_GEMINI=1`（带同样的
   自动降级）。Anthropic 原生 Messages API 无 strict 概念，但 L2
   校验层对其同样生效。

## 配置开关

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `DISABLE_STRICT_TOOL_SCHEMA=1` | 全局关闭 strict 注入（L0 总开关） | 未设置=开启 |
| `ENABLE_STRICT_TOOL_SCHEMA_GEMINI=1` | 对 Gemini OpenAI-compat 循环实验性开启 strict | 未设置=关闭 |
| `STRICT_*`（运行时降级） | 无需配置：网关拒绝自动摘除并记忆 | 自动 |

## 顺带修复的两个既有 bug

1. **UI 摘要生成器崩溃**：`_generate_initial_tool_summary` 对
   `command` / `query` / `url` 等字段直接 `.strip()`、把 `command`
   当 dict 键查表——模型给出合法 JSON 但类型错误（如 `command: 42`）
   时，在 L2 拦截**之前**就 `AttributeError` 崩掉整批工具执行。已全部
   加 `str()` 防御（L2 会拦截回传，UI 只需不崩）。
2. **旁路路径行为分叉**：`_run_tool_calls_and_append` 的兜底解析
   （未经 `_normalize_tool_call_arguments` 的路径）只写信封不尝试
   修复，与主路径行为分叉。已改为复用同一套规范化链（双引擎修复 →
   信封），任何入口行为一致。

## 验证

```
PYTHONPATH=src python scripts/test_tool_args_pipeline.py   # 64 项断言
PYTHONPATH=src python scripts/test_run_one_gate.py         # 12 项断言
```

覆盖：strict 递归规范化与不变量（原始列表零修改）、网关拒绝降级、
环境变量开关、双引擎修复、截断安全、null 剥离、写法容错、必填/类型/
枚举/二选一校验、错误消息可操作性、端到端规范化回归、三条循环的
tools 透传与签名兼容、执行闸门真实批次模拟（缺必填不执行、字符串
布尔矫正后执行、null 剥离后执行、畸形 JSON 修复后执行、截断拒绝、
类型错误拦截回传）。

两个脚本在 json-repair / jsonschema 未安装时自动退化为内置引擎断言
（与生产行为一致：可选依赖缺失 → 兜底引擎）。

## 升级注意

新增两个**可选**依赖（已写入 requirements.txt / pyproject.toml；
不安装也不影响启动，仅退回兜底引擎）：

```
pip install "json-repair>=0.30,<1" "jsonschema>=4.23,<5"
```

上线后建议观察两行日志确认各层生效情况：

- `[api] 第 N 轮自动修复了 X 个畸形工具参数 JSON` → L1 生效（含
  `repaired with the json-repair library` 引擎标记时即社区库路径）；
- `工具 bash 参数未通过 schema 校验（已拦截并回传可操作错误）` → L2
  生效；
- `网关拒绝了 strict 工具 schema…已自动降级` → L0 自动降级触发
  （该网关不支持 strict，属预期行为而非故障）。

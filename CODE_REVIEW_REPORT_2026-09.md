# Code Review 报告（2026-09）

审查范围：AI 响应处理与健壮性 / 代码质量与冗余 / 架构与工程规范。
项目规模：约 190 个 Python 文件，72,000+ 行代码（不含测试）。

## 结论先行

这是一个已经历过多轮生产事故修复、工程纪律很高的代码库：无裸露
`except:`，客户端超时按"连接短、流读取放宽到 300s"精细配置，重试
逻辑严格限定在"零输出阶段"避免重复内容，四条 provider 循环刻意抽出
公共骨架（`bridge_common.py`）以保证修一处、四处生效。大部分注释不是
装饰性说明，而是记录了具体故障背景和取舍理由。

因此本次审查没有做"推倒重来"式重构——那样风险收益比很差，容易在
没有完整回归环境的情况下引入新问题。而是按你圈定的优先级（AI 响应
处理健壮性）做了实际代码级验证，只对**验证到的真实问题**动手修复。

---

## 一、AI 响应处理与健壮性

### 1.1【已修复】纯文本终局回答被输出长度上限截断时，用户和模型都不知情

**问题**：`finish_reason`/`stop_reason` 在四条 agentic 循环
（openai_compat / anthropic / gemini / responses）中都被正确捕获和
记录，但**只在"本轮解析出了工具调用、且参数 JSON 非法"时才会被查阅**
（`json_repair.build_invalid_arguments_envelope`），用于诊断参数是否被
截断。当模型本轮**没有调用任何工具**，只是输出了一段被
`max_tokens`/`length` 提前切断的纯文本终局回答时：

- 截断的回答会被当成完整回答直接展示给用户、写入历史；
- 用户看不出结尾是被截断的；
- 模型自己在下一轮也不知道——它会以为自己已经把话说完了，
  不会主动续写。

**根因**：`_finish_reason_cut_info`（`json_repair.py`）这套分类逻辑
本身没问题，只是消费方单一，没有覆盖"纯文本终局"这条路径。

**修复**：新增 `ai/bridge_common.append_truncation_notice_if_needed`，
复用同一套 `_finish_reason_cut_info` 分类（不引入第二套判定标准），
在四条循环各自的"本轮无工具调用"分支里统一调用：确认是
`length`/`max_tokens`/`max_output_tokens` 导致的截断时，在回复末尾
追加一条简短提示；`content_filter`（更适合走既有空响应兜底）与断流
证据（空字符串，连接层问题而非内容长度问题）不追加，避免误导。

关键实现细节：提示必须在 `live_slot.finalize(...)` **之前**算出并
写入 `content_acc`，否则历史/`loop_messages`里定稿的仍是未追加提示
的原文——这是本次修复中最容易出错的一步（四条循环都有这个时序
要求）。openai_compat 循环额外排除了"文本伪工具调用"分支（模型把
工具调用写成 XML 文本的另一类问题），因为它已经有自己的
`final_content` 语义，与"输出长度不够"无关，不应叠加。

改动文件：`ai/bridge_common.py`（新增函数）、`ai/anthropic_bridge.py`、
`ai/gemini_bridge.py`、`ai/responses_bridge.py`、`ai/agentic_loops.py`。

### 1.2【已修复】OpenAI Responses API 桥接丢失真实截断原因

**问题**：`responses_bridge.py` 在处理 `response.incomplete` /
`response.failed` 事件时，把**事件名**（`"incomplete"`）当作
`response_status` 记录下来，传给下游诊断逻辑。但 OpenAI Responses API
的真实截断原因在 `response.incomplete_details.reason` 字段里，取值是
`"max_output_tokens"` / `"content_filter"`——与事件名完全不是一回事。

这意味着即使是**改动前就存在**的"工具参数被截断"诊断路径
（`build_invalid_arguments_envelope(..., stream_finish_reason=response_status)`），
在 Responses API 协议下也从未真正生效过：`"incomplete"` 这个值不在
`_finish_reason_cut_info` 认识的任何取值里，永远被判定为"未截断"。

**修复**：改为优先提取 `incomplete_details.reason`，取不到时才退回
事件名（保持旧行为兜底，不引入新的空值风险）。同时在
`_finish_reason_cut_info` 里补充识别 `"max_output_tokens"` 这个
Responses API 专用拼写（与 `"length"` / `"max_tokens"` 并列），这样
1.1 的截断提示和已有的工具参数诊断在这条协议上才能真正生效。

改动文件：`ai/responses_bridge.py`、`ai/json_repair.py`。

### 1.3【设计已确认合理，未改动】空响应兜底与失败标记的边界

`ai/bridge_common.ensure_final_content` 在"轮次耗尽且从未产出任何
内容"时写入固定的兜底总结文案；`agentic_loops.py`/`gemini_bridge.py`
在"本轮确有响应但正文为空"时写入 `"（模型未返回任何内容）"`。这两条
路径都会写入一条 assistant 消息，因此不会触发
`turn_recovery.mark_failed_unanswered_user`（该函数只在"完全没有写入
assistant 消息"的路径上调用，例如顶层异常、IMAGE_ERROR/VIDEO_ERROR）。

核实后认为这是**当前实现下的合理选择**而非 bug：空文本本身不代表
请求失败（模型可能就是在纯执行工具、无话可说），把它也标记为失败会
让"下一条用户消息替换而非追加"的语义在一些正常场景里被误触发。
如果你观察到生产上"模型正常出空文本"和"模型请求确实失败但被当成
成功"混淆的具体案例，值得针对具体场景加更细的判定，而不是笼统改动
这条边界——这类判定的误伤成本比截断提示缺失更高，不建议无实测场景
下改动。列在此处供你知情，未做修改。

### 1.4 客户端超时 / 流式重试：抽查未发现问题

`api_client.py` 对 `AsyncAnthropic` / `AsyncOpenAI` 均显式配置了分项
超时（`connect=10s, read=300s, write=60s, pool=60s`），并关闭了 OpenAI
SDK 的隐式自动重试（避免和应用层重试叠加导致长时间无感等待）。
`anthropic_bridge._is_retryable_stream_error` /
`_extract_retry_after_seconds` 对状态码、连接层异常类名、错误体
`error.type`、`Retry-After` 响应头做了多层兜底判定，且严格限定在
"零输出阶段"才重试——这一层没有发现设计缺陷。

### 1.5 异常处理扫描

全项目 `src/` 下无裸露 `except:`；`except Exception: pass`
仅 1 处发生在纯粹的图片二进制头部嗅探（`media_generation.py`，有
安全的默认返回值兜底），其余"内部忽略"路径均带 `logger.debug(...,
exc_info=True)`，不属于静默吞异常。未发现需要修复的异常处理问题。

---

## 二、代码质量与冗余清理

- 未发现明显的"重复造轮子"（如自制 JSON/HTTP/重试库）。项目已经在用
  `json-repair`、`jsonschema`、`aiohttp`、`httpx`/`httpx2` 等标准/成熟
  库，`json_repair.py` 里的自定义修复逻辑是在库函数之上叠加的领域
  特定诊断（截断归因、错误定位），不是重复实现同一功能。
- 测试覆盖有明显缺口：`json_repair.py`（本次修复涉及的核心分类函数
  `_finish_reason_cut_info`）与 `ai/bridge_common.py` 此前**完全没有
  独立单元测试**，只能通过四条循环的集成路径间接覆盖。本次已补上
  `tests/unit/test_truncation_notice.py`（23 个用例，覆盖三种协议的
  截断拼写、大小写、正常结束、内容过滤、断流证据、空内容等边界），
  但这只是这次改动涉及的部分；建议后续给 `json_repair.py` 剩余的
  修复/诊断函数（`repair_json_arguments`、`invalid_arguments_message`
  等）也补上直接单测，而不是完全依赖集成测试兜底。
- 大文件较多（`media_generation.py` 2552 行、`rich_message_builder.py`
  1979 行、`agentic_loops.py` 1877 行），但抽查后内容内聚，没有看到
  "大杂烩"式的无关逻辑堆砌，拆分收益不确定；不建议在没有具体维护
  痛点的情况下单纯为了"文件更小"而拆分，容易增加跨文件跳转成本却
  不改善可读性。

## 三、架构与工程规范

- 协议驱动的多厂商适配（`config.PROVIDERS` + `protocol` 字段 +
  `api_client._build_client` 按协议分流）是合理的扩展点设计，新增
  厂商基本不需要碰调用方代码。
- `ai/bridge_common.py` 把四条循环的公共骨架（打断保全、草稿流切换、
  超限总结、终局收束）收敛成一份实现，这是本次能够"一处修复、四处
  生效"的直接原因，值得作为项目内其他重复逻辑的参考模式。
- 模块划分总体清晰（`ai/` 处理模型交互、`search/` 处理工具实现、
  `protocols/` 处理线上协议适配、`core/` 放跨模块共享的消息模型），
  抽查未发现明显的循环依赖或职责错位。

---

## 本次实际改动清单

| 文件 | 改动 |
|---|---|
| `src/ai/bridge_common.py` | 新增 `append_truncation_notice_if_needed`：四条循环共用的截断提示追加逻辑 |
| `src/ai/json_repair.py` | `_finish_reason_cut_info` 补充识别 `max_output_tokens`（Responses API 拼写） |
| `src/ai/anthropic_bridge.py` | 纯文本终局分支接入截断提示，时序在 `live_slot.finalize` 之前 |
| `src/ai/gemini_bridge.py` | 同上 |
| `src/ai/responses_bridge.py` | 同上；另修复 `incomplete_details.reason` 提取（此前只记事件名） |
| `src/ai/agentic_loops.py` | 同上；排除"文本伪工具调用"分支避免与其自身逻辑冲突 |
| `tests/unit/test_truncation_notice.py` | 新增：23 个用例覆盖分类与提示追加逻辑 |

所有改动均已通过语法检查（`ast.parse`）与独立运行的单测验证
（不依赖完整应用启动的最小化测试），修改范围严格限定在上述文件，
未触及其余 180+ 个文件。

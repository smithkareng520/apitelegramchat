# 缓存逻辑分析与优化（Cache Optimization Notes）

本文档是对本项目全部缓存逻辑的完整分析，以及为"让缓存命中更好、更多"所做改动的说明。
所有结论均基于对源码的逐行阅读，并对照了各厂商当前（2026-08）的官方文档：

- Anthropic Prompt Caching：<https://platform.claude.com/docs/en/build-with-claude/prompt-caching>
- OpenRouter Prompt Caching / Provider Sticky Routing：<https://openrouter.ai/docs/guides/best-practices/prompt-caching>
- DeepSeek Context Caching：<https://api-docs.deepseek.com/guides/kv_cache>
- 智谱上下文缓存：<https://docs.bigmodel.cn/cn/guide/capabilities/cache>
- Gemini Context Caching：<https://ai.google.dev/gemini-api/docs/caching>

---

## 一、现状盘点：项目里的全部缓存

| # | 缓存 | 位置 | 类型 | 作用 |
|---|------|------|------|------|
| 1 | LLM 前缀缓存（Anthropic 显式） | `ai/attachment_content.py::_apply_cache_control` | content block 上的 `cache_control` 断点 | 省输入 token 费用（命中约 0.1x）|
| 2 | LLM 前缀缓存（隐式） | DeepSeek / GLM / Gemini / OpenAI 服务端自动 | 无需标记 | 同上，靠前缀字节一致命中 |
| 3 | 附件字节缓存 | `ai/attachment_content.py`（`_image_cache` 等 4 个 TTLCache） | 内存，`CACHE_TTL`=300s | 免重复下载 Telegram 文件 |
| 4 | fetch_url 页面缓存 | `search_engine.py::_fetch_cache` | TTLCache 200 条 / 3600s | 免重复抓取网页 |
| 5 | web_search 结果缓存 | **原缺失，本次新增** | TTLCache 200 条 / `SEARCH_CACHE_TTL`=300s | 免重复消耗 Serper 配额 |
| 6 | R2 预签名 URL | `s3_utils.py::generate_presigned_url` | **原每次重签，本次新增记忆化** | 前缀稳定性 + 免重复签名 |
| 7 | 技能目录 | `skills.py`（`lru_cache`） | 进程级 | 免重复扫描 skills 目录 |
| 8 | 沙箱运行时缓存 | `sandbox.py` / `runtime_cache_root`（pip/ccache/npm/HF…） | 磁盘 | Bash 会话间复用包与编译缓存 |
| 9 | 草稿去重缓存 | `utils.py::_last_sent_draft_cache` | 内存 | Telegram 草稿幂等 |

**关键认知**：对一个 Agent 应用来说，#1/#2（LLM 前缀缓存）是费用大头——每轮请求
都要重发"系统提示 + 工具 schema + 全部历史"，动辄几万 token；命中后按约 0.1x 计费。
其余缓存主要省时延与外部配额。本次优化以 #1/#2 为主、#5/#6 为辅。

### 原有设计中已经做对的事

1. **系统提示时间戳放末尾**（`build_system_prompt`）：`当前时间` 是唯一每天必变的
   内容，放在 prompt 最末尾，前面的稳定主体（格式规范 + 工具通则 + 技能目录）可整天命中。
2. **`cache_control` 打在 content block 上而非消息顶层**：OpenRouter/OpenAI 兼容网关
   会忽略消息顶层的标记（原注释已写明）。
3. **断点在"全部消息就位后"打**（含本轮新 user 消息）：断点越靠后覆盖前缀越长。
4. **子 agent 工具白名单排序**（`DEFAULT_ALLOWED_TOOLS`）：set 迭代顺序不稳定会导致
   tools schema 顺序变化 → 前缀失效，原代码已用 `sorted()` 修复。
5. **fetch 缓存键归一化**（去 fragment），且根路径回退结果也写回原 URL 的缓存。

---

## 二、问题诊断（按影响排序）

### P1. 上下文窗口逐轮滑动 → 隐式缓存历史段每轮全 miss

**现象**：旧版 `select_request_context` 从尾部回退装配"最近 token 预算内"的历史。
历史一旦超限，每轮新增几条消息，窗口起点就向后挪几条。

**根因**：DeepSeek/GLM/Gemini/OpenAI 的隐式缓存是**前缀完全匹配**。窗口起点一变，
从起点到结尾的整段历史（可能几万 token）全部按原价重算；只有 system+tools 前缀
（断点 1 之前）还能命中。

**修复（2026-09 重构，取代更早的量化淘汰方案）**：有界会话窗口 + 摊销式
自动压缩（`context_window.py` + `app.pre_flight_context_check`），对齐
Claude Code / Cline 等主流 Agent 的上下文管理形态：

- **存储历史即请求上下文**：预算内 `select_request_context` 退化为守卫，
  全量透传（浅拷贝）、一字节不改；
- **高/低水位滞后触发**：历史 + 新输入 > 触发水位（90% 预算）才进入
  压缩事件，一次压回目标水位（50%），而不是"刚好塞得下"——事件之后
  历史要重新长回 40% 预算才会再次触发，期间所有请求前缀逐字节稳定；
- **事件内两级杠杆**：L1 工具负载归档为指针（无损，text_editor 可取回）
  → L2 从最老的用户轮块开始整块淘汰（保护最近 6 轮），被淘汰轮合并进
  历史头部稳定槽位的**滚动摘要**（确定性纯函数，无时间戳，同输入同字节）；
- **请求侧守卫只做兑底**（压缩事件失败 / 会话中途切到小窗口模型时
  按块裁剪出站视图），常态零介入。

验证脚本（`scripts/verify_context_strategy.py`）模拟 40 轮对话：每一次前缀
变化都恰好对应一次压缩事件（无滑动漂移）、事件不连发（无抖动）、守卫输出
永远不超预算、被淘汰信息以摘要 + 归档指针形式保留。

### P2. OpenRouter 路由漂移 → 同一对话被路由到不同 provider

**现象**：OpenRouter 的粘性路由默认靠"首条 system + 首条非 system 消息"哈希识别会话；
本项目上下文窗口滑动 / 历史压缩都会改变这两条消息 → 哈希变 → 路由漂移到别的
provider → 那边的缓存是冷的，白写一遍（Anthropic 显式缓存写 1.25x）。

**根因**：没传 `session_id`。官方文档明确建议多轮 agentic 工作流显式传
`session_id`：粘性路由**从第一次请求就生效**（默认要等首次缓存命中后才生效），
且不随消息变化漂移；对经 OpenRouter 转发的 Z.AI/GLM 还会作为会话亲和键下发。

**修复**：所有 OpenRouter 请求（主循环 / 非流式回退 / 工具超限总结 / 子 agent）
统一携带 `session_id = tg-chat-{chat_id}`（≤256 字符）。非 OpenRouter 厂商不注入。

### P3. Agentic loop 内断点不前移 → 工具中段每轮重算（Anthropic）

**现象**：原断点固定打在 system + 本轮 user 消息上。loop 第 2..N 轮请求虽然能命中
"user 消息之前"的前缀，但每轮新增的 `assistant(tool_calls) + tool 结果` 位于断点
之后，**永远进不了缓存**——下一轮全按原价重算。

**根因**：缺少"随消息增长自动前移"的断点。

**修复**：三层组合（不超过 Anthropic 单请求 4 断点上限）：
1. system 末尾（显式，稳定段）；
2. **上一轮对话末尾**（显式，新增）：下一轮用户请求可命中到上一轮的最终 assistant
   回复，而不是只命中到上一轮的 user 消息——工具中段是每轮 token 的大头；
3. 本轮 user 消息（显式，原有）：loop 第 2..N 轮命中；
4. **顶层自动缓存**（`extra_body.cache_control = {"type": "ephemeral"}`，新增）：
   Anthropic 官方"automatic caching"，断点由服务端打在最后一个可缓存块上并随对话
   自动前移——每轮的工具结果因此能被下一轮命中；跨轮时下一轮可命中到上一轮的
   最后一条 tool 结果。

### P4. R2 预签名 URL 每轮重签 → 多模态历史打碎前缀

**现象**：`R2_PUBLIC_URL` 未配置（或指向私有 S3 端点）时，`public_url_for_existing_key`
每次调用都重新签名。预签名 URL 含 `X-Amz-Date`/`X-Amz-Expires`/签名，**每次都不同**。
历史消息里的 `image_url`/`video_url` content 块（Agnes 视觉路径、视频输入路径）逐轮
变化 → 从第一条含附件 URL 的历史消息起，其后所有内容的缓存全部失效。

**修复**：`generate_presigned_url` 记忆化（TTLCache，TTL = 1h 有效期 − 5 分钟安全
边际）。同一对象在有效期内字节级稳定，同时省掉重复签名开销。

### P5. web_search 完全没有结果缓存

**现象**：`SEARCH_CACHE_TTL` 在 `config.py` 里定义了却**无任何引用**（死配置）。
agent 循环里模型重复/微调同一查询非常常见（子 agent 与主 agent 也常搜同一个词），
每次都真打 Serper，消耗配额与时延。

**修复**：`execute_web_search` 包一层 TTLCache（200 条 / `SEARCH_CACHE_TTL` 默认 300s），
键为归一化参数（modes/query/num/page/gl/hl/tbs/image_url）。**服务错误不缓存**
（保证可重试），确定性空结果缓存（省配额）。

### P6. 历史压缩按消息条数触发 → 与 token 预算脱节（已并入压缩事件）

**现象**：旧版 `len(history) > 30` 时按消息条数触发 `compact_older_tool_calls`，
后来用 `HISTORY_COMPACTION_MIN_BATCH` 攒批缓解前缀 churn。但消息条数与
token 预算无关：大窗口模型上过早触发、小窗口模型上过晚触发，且与
pre-flight 属于两套互不知情的口径。

**修复**：整块移除按条数触发的机制（含两个环境变量）。工具负载归档只在
压缩事件内作为 L1 / L2 的前置步骤执行（见 P1）——同一个触发器、同一个
预算口径、同一个（稀疏的）时机，不再存在"回合末尾顺手压缩"的独立路径。

### P7. 缓存命中完全不可见

**现象**：usage 里的缓存字段没有任何日志，无法回答"缓存到底命中了没"。

**修复**：每轮请求结束打一行 INFO：
`[openrouter] prompt cache usage: {'prompt_tokens': 10339, 'cached': 9500, 'hit_ratio': 0.918}`。
统一解析三种字段族：OpenRouter/OpenAI（`prompt_tokens_details.cached_tokens`）、
其他兼容 provider 的 cache-read 字段（如 provider 返回）。Gemini 循环同样接入（dict 形态 usage）。

---

## 三、改动清单

### 本次（2026-09 上下文策略重构）

| 文件 | 函数/位置 | 改动 |
|------|-----------|------|
| `context_window.py`（新） | 整个模块 | 有界窗口核心：预算解析 / 双水位 / 用户轮块拆分 / 淘汰规划 / 滚动摘要（确定性纯函数，无 IO）（P1） |
| `context_manager.py` | `select_request_context` 重写 | 滑动截尾 → 守卫语义：预算内全量透传；超预算按块裁剪出站视图 + 单消息截断兑底（P1） |
| `app.py` | `pre_flight_context_check` 重写 | 双水位滞后触发 + 单一压缩事件（L1 归档 → L2 整轮淘汰 + 摘要合并）；移除逐块删到塞得下的循环（P1） |
| `app.py` | `update_conversation_and_ledger`、模块常量 | 移除按消息条数触发的压缩机制及其两个环境变量（P6） |
| `app.py` | 顺带修复 | 旧代码引用不存在的 `ToolCompactionStats.compacted_calls_count` 属性，第二遍归档路径一旦触发即 `AttributeError`（潜伏 bug，已随重写消除） |
| `ai_handlers.py` | `select_request_context` 调用点 | 传入 `model_max_output`，守卫预算与 pre-flight 压缩预算共用同一解析 |
| `ai/agentic_loops.py` | `_openrouter_session_id` 注释 | 与新策略对齐（session_id 依据不变） |
| `scripts/verify_context_strategy.py`（新） | 整个脚本 | 48 项行为断言（见「五、验证」） |
| `README.md` / `CACHE_OPTIMIZATION.md` | 缓存章节 | 与新策略同步，移除量化淘汰 / 批量压缩描述 |

### 新增环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CONTEXT_MAX_TOKENS` | 不设置 | 历史预算绝对覆盖（兼容旧语义；设了则忽略比例推导） |
| `CONTEXT_BUDGET_RATIO` | `0.8` | 历史预算占 max_context 比例（同时与 `max_context − max_output` 取更紧者） |
| `CONTEXT_COMPACT_TRIGGER_RATIO` | `0.90` | 触发压缩事件的高水位（占预算比例） |
| `CONTEXT_COMPACT_TARGET_RATIO` | `0.50` | 压缩事件要压回的目标水位（滞后区间 = 两者之差） |
| `CONTEXT_PROTECTED_TURNS` | `6` | 永不淘汰的最近用户轮数（活跃工作集） |
| `CONTEXT_DIGEST_TOKEN_BUDGET` | `1500` | 滚动摘要 token 预算（实际不超过预算的 1/4） |

移除的环境变量：`CONTEXT_EVICT_HEADROOM_MESSAGES`（量化淘汰，已随方案取代）、
`HISTORY_COMPACTION_TRIGGER`、`HISTORY_COMPACTION_MIN_BATCH`（按消息条数触发，已移除）。

### 此前批次（2026-08）

| 文件 | 函数/位置 | 改动 |
|------|-----------|------|
| `ai/agentic_loops.py` | `_openrouter_session_id`（新）、`_openrouter_extra_body`、`_merged_extra_body` | session_id 粘性路由 + 顶层自动 cache_control（P2/P3） |
| `ai/agentic_loops.py` | 主循环 / 回退 / 总结三处 create 调用 | 传入 chat_id 与 supports_prompt_cache |
| `ai/agentic_loops.py` | `_extract_cache_usage`（新）、`_log_cache_usage`（新） | 缓存命中观测（P7），两个循环出口接入 |
| `ai/attachment_content.py` | `_apply_cache_control` | 3 显式断点策略：system / 上一轮末尾 / 本轮 user（P3） |
| `s3_utils.py` | `generate_presigned_url` | 预签名 URL 记忆化 + 并发签名去重（P4） |
| `search_engine.py` | `execute_web_search` / `_execute_web_search_uncached` | 拆分为缓存壳 + 原实现（P5） |
| `subagent_tool.py` | 子 agent LLM 调用 | OpenRouter session_id + 顶层 cache_control（P2/P3） |
| `README.md` | 「缓存与 Prompt Cache」新章节 | 全部机制与配置项文档化 |

已有但 previously-unused 的 `SEARCH_CACHE_TTL`（默认 300s）被正式启用。

---

## 四、效果预估

以一个典型"工具密集"会话为例（系统提示+工具 schema 约 6k token，历史 30k token，
每轮工具中段新增 3k token，预算 0.8×128k ≈ 102k）：

| 场景 | 旧版命中 | 新版命中 |
|---|---|---|
| Anthropic：loop 第 2..N 轮 | system+工具+历史前段（≈6k+稳定段） | 同左 + 每轮工具结果跨轮命中 |
| Anthropic：下一轮用户消息 | 到上一轮 user 消息为止 | 到上一轮最后一条 tool 结果为止（多命中整轮工具中段） |
| DeepSeek/GLM（隐式）：历史远未到水位 | 基本全命中（与旧版相同） | 相同 |
| DeepSeek/GLM：历史超过预算后 | 每轮仅 system+tools 段命中，历史段（几万 token）全 miss | 历史段只在稀疏的压缩事件轮 miss，两次事件之间（几十轮）全命中 |
| OpenRouter 路由 | 首次命中后才粘性，且窗口滑动即断 | 从第一次请求即粘性，压缩事件不断 |
| web_search 重复查询 | 每次真实调用 Serper | 300s 内直接返回缓存 |
| 含附件 URL 的多模态历史 | 每轮重签 URL → 前缀全断 | 有效期内前缀稳定 |
| 被淘汰的早期对话 | 静默丢弃（旧 pre-flight 逐块删） | 滚动摘要（每轮一行 U/A/T）+ 归档指针可 text_editor 取回 |

综合：Anthropic 路径的输入费用约可再降 30-60%（取决于工具密度）；隐式缓存厂商在
长会话中的历史段命中率从"几乎总是 miss"提升到"除压缩事件轮外全部命中"
（事件频率约为每几十轮一次，取决于增速）。

---

## 五、验证

`python3 scripts/verify_context_strategy.py`（随包附带）覆盖 48 项行为断言，
全部通过：

1. 预算解析：比例 / 输出约束取 min / 绝对覆盖优先 / 兑底 50000 / 小窗口下限；
2. 结构拆分：用户轮锚定 / 前导孤立块 / 摘要槽位单独抽出 / 轮内 system 归属；
3. 淘汰规划：预算内 no-op / 从最老整轮开始 / 压到目标水位 / 保护尾 ≥ 6 轮 /
   摘要永不淘汰 / 单块超大不抛异常；
4. 滚动摘要：确定性（同输入同字节）/ 合并旧行 + 轮数累计 / 归档指针进入 T 行 /
   预算从最老行丢弃并标注 / 多模态占位；
5. 请求守卫：快路径全量透传 / 浅拷贝隔离 / 兑底按块淘汰且不超预算 /
   孤儿 tool 首条剔除 / 单消息超预算截断 / 极端小预算仍合法；
6. 多轮模拟（40 轮）：**每次前缀变化都恰好对应一次压缩事件**（无滑动漂移）/
   事件不连发（无抖动）/ 守卫输出永远不超预算 / 早期信息以摘要保留 /
   持久历史回到目标水位附近。

另对全部改动文件与全项目做了 `py_compile` / `compileall` 编译期检查，均通过。

（更早批次的 `scripts/verify_cache_changes.py` 覆盖断点 / session_id / 搜索缓存 /
预签名 URL / usage 解析，不在本次包内；对应行为未改动。）

---

## 六、本次复查记录（日志健壮性 + 缓存复核）

**缓存策略复核**：对上述 P1-P7 的核心断言逐条与源码比对（不仅是读文档），
包括确认 `_apply_cache_control` 唯一调用点位于用户消息 append 之后、确认
`_log_cache_usage` 确实在 `for _round` 循环内部（而非循环外）、确认
`execute_web_search` 的参数归一化只有一份等。结论：本文档描述与实现一致。

发现一处遗留的小问题（非缓存正确性问题，未修复，供后续处理）：
`s3_utils.py::generate_presigned_url` 用**单个全局** `asyncio.Lock()`
去重并发签名，而非按 key 加锁——不同 R2 key 的并发签名请求会被互相
串行阻塞（含一次网络往返），而不仅仅是同 key 去重。缓存本身的正确性
不受影响，只是并发场景下多花一点延迟。

**日志健壮性**：给 20 个文件里约 116 处"完全静默"的 `except Exception:`
补充了 `logger.debug(..., exc_info=True)`（不改变任何控制流，只是让
失败可观测）。过程中用 AST 扫描（而非文本替换）逐处插入，并在插入后
额外做了两轮系统性复查，排除了两类会引入新 bug 的插入位置：
  1. 模块级"可选依赖导入失败静默降级"的 `except`（在 `logger` 变量
     真正赋值*之前*就会执行，插入会导致 `NameError`）——`search_engine.py`
     的 6 处可选依赖兜底（trafilatura/curl_cffi/feedparser/qrcode/lxml）
     与 `fetch_rich_content.py` 的 lxml 兜底，均已排除，保持原样静默；
  2. `logging.Filter.filter()` 内部——从过滤器里再记一条日志有重入
     日志管线的风险，`utils.py::_MCPStreamableHTTPNoiseFilter.filter()`
     已排除，保持原样静默。
`utils.py::setup_logging()` 内部两处也因同样的"logger 尚未赋值"原因
排除（该函数在模块级于 `logger = logging.getLogger(__name__)` 之前
被调用）；文件写入失败分支保留 `print(..., file=sys.stderr)` 兜底，
新增 `traceback.print_exc()` 以保留原本会被 `exc_info=True` 记录的
完整堆栈。

所有改动后用 `py_compile` 对全部 19 个改动文件与全项目做了编译期
检查，均通过。

## 七、遗留建议（本次未做）

1. **1 小时 TTL**：Anthropic 显式断点可加 `"ttl": "1h"`（写 2x、读仍 0.1x）。适合
   低频（>5 分钟间隔）长会话；高频会话 5 分钟 TTL 每次命中都会刷新，已够用。
2. **`OPENROUTER_PROVIDER_SORT=price` 与粘性的相互作用**：粘性路由在"该 provider
   缓存读价更便宜"时才生效；若某个模型多家 provider 价格接近，仍可能偶发漂移。
   极致优化可对主力模型固定 `provider.order`（但会失去 fallback 能力，需权衡）。
3. **`_append_history_async` 每轮重解析历史附件**：图片走 base64 时字节稳定（不影响
   缓存），但 TTL 过期后会重新下载 Telegram/R2——可加大 `CACHE_TTL` 或做磁盘层。
4. **Gemini 显式 context cache**（`cachedContent`）：OpenAI 兼容层不支持，需要原生
   REST 调用才能用；当前隐式缓存已能吃到大部分收益。
5. **LLM 摘要替代确定性摘要**：当前滚动摘要每轮只保留 U/A/T 骨架行（确定性、零
   成本、零延迟）。若希望摘要质量更高，可在压缩事件中用一次 LLM 调用重写摘要
   （Claude Code 式）；代价是事件轮增加一次请求的延迟与费用，以及摘要内容的不
   确定性。接口已预留（`build_digest_text` 是纯函数，可整体替换）。
6. **摘要的历史层次**：摘要行数超预算时按"最老先丢"处理，长会话的远古信息会
   从摘要中淡出。若需要更强的长期记忆，可把摘要沉淀进 `memory_tool`（已有
   跨会话持久化），而不是无限扩大摘要预算。

---

## 八、2026-09 上下文策略重构记录（取代量化淘汰方案）

### 背景

用户反馈：量化淘汰思路"设计不够好，也很复杂"，要求按主流 Agent 上下文
策略重做。复查发现更严重的问题——**文档与代码已经脱节**：本文档此前描述
的 `_quantized_drop` 量化淘汰在当前代码里并不存在，`select_request_context`
实际是"纯 token 预算的尾部滑动选取"（对隐式缓存是最差形态：每轮起点必变）；
按消息条数（>30）触发的批量压缩与 pre-flight 的 token 口径互不知情。

### 新策略（一句话版）

**淘汰不再是历史长度的连续函数，而是离散事件**：历史在预算内时请求前缀
一字节不变；超过触发水位（90%）时一次性压回目标水位（50%），被淘汰的
轮进入滚动摘要、其工具负载先归档为可取回的指针。

### 关键设计决策

1. **确定性摘要而非 LLM 摘要**：Claude Code 的 auto-compact 用一次 LLM 调用
   生成摘要；本项目选择确定性骨架行（U/A/T，见 `build_digest_text`）。
   理由：压缩事件发生在用户回合的 pre-flight，加 LLM 调用会引入延迟、
   费用与失败模式三重代价，而确定性摘要零成本、可单测、可复现。接口
   已预留，将来可整体替换为 LLM 摘要（见遗留建议 5）。
2. **摘要无时间戳**：摘要只在压缩事件中被重写，但即便如此也不放时间戳
   等易变内容——同输入永远同字节，把"字节稳定"贯彻到每一个槽位。
3. **摘要用 role=system、置于历史下标 0**：这是持久历史中唯一的 system
   消息（业务消息只会是 user/assistant/tool），Anthropic 桥接会把它拼进
   顶层 system（缓存断点 1 覆盖），OpenAI 兼容厂商接受中段 system 消息。
4. **预算 = min(0.8×max_context, max_context−max_output)**：统一了旧版
   两套互相矛盾的口径（视图 0.8× vs safe_limit max−output）。128k/8k 输出
   的模型取 102.4k，128k/64k 输出的取 62.5k（旧版后者名义 62.5k、视图却
   允许 102k，自相矛盾）。
5. **保护尾 6 轮不淘汰**：活跃工作集（当前任务的上下文）优先于历史纵深；
   保护尾超出预算的极端场景交给请求守卫按块裁剪，宁可临时丢老轮也不
   让请求非法。
6. **守卫保留最后一个块**：兜底路径至少保留最新一轮（由尾部装配做单
   消息级截断），避免"一条超大新消息被整块丢弃"的反直觉行为。

### 常见问题：什么时候会发生上下文剪切？

- **压缩事件（唯一的常规剪切点）**：新回合 pre-flight 时，若
  `历史 token + 新输入 token > 90% × 预算`。工具密集会话通常每几十轮
  一次；事件内先归档工具负载（无损），不够再整轮淘汰进摘要。
- **请求守卫（兜底剪切，非常规）**：仅当持久历史超出预算（压缩事件
  失败 / 会话中途切到小窗口模型）时，在**出站视图**上按块裁剪，持久
  历史不动，下一轮压缩事件把它收敛回预算内。
- **单消息截断（最后防线）**：单条消息自身超预算时按 token 截断该
  消息正文。
- 其余一切时刻（即绝大多数请求）：历史全量透传，前缀字节稳定。

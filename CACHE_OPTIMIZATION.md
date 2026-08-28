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

**现象**：`select_request_context` 取"最近 50 条 / 50k token"。历史一旦超限，每轮
新增几条消息，窗口起点就向后挪几条。

**根因**：DeepSeek/GLM/Gemini/OpenAI 的隐式缓存是**前缀完全匹配**。窗口起点一变，
从起点到结尾的整段历史（可能几万 token）全部按原价重算；只有 system+tools 前缀
（断点 1 之前）还能命中。

**修复**：量化淘汰（`_quantized_drop`）。淘汰量向上取整到 `CONTEXT_EVICT_HEADROOM_MESSAGES`
（默认 10）的整数倍，窗口起点成为历史长度的**阶梯函数**：历史每增长 10 条，起点才
前进 10 条；中间约 10 轮内请求前缀字节级一致。token 上限触顶走同一量化。
验证脚本证明：起点可连续 9 轮保持不变（旧版每轮都变）。

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

### P6. 历史压缩每轮触发 → 前缀持续 churn

**现象**：`len(history) > 30` 时每轮都跑 `compact_older_tool_calls`，把约一半未归档
的旧 tool 消息重写成指针（payload → 指针文本）。**消息内容变了 = 前缀从那里断了**。
fetch_url/wikipedia/text_editor 密集的会话（本项目最典型的用法）在历史超过 30 条后，
每轮都在 churn。

**修复**：批量化——`HISTORY_COMPACTION_MIN_BATCH`（默认 8）：未归档的可压缩调用
攒够一批才压缩一次。两次压缩之间的若干轮里前缀保持稳定。压缩本身仍保留
（它是控制内存/请求体积的必要手段，只是不再每轮触发）。
另外触发阈值改为可配：`HISTORY_COMPACTION_TRIGGER`（默认 30，保持原值）。

### P7. 缓存命中完全不可见

**现象**：usage 里的缓存字段没有任何日志，无法回答"缓存到底命中了没"。

**修复**：每轮请求结束打一行 INFO：
`[openrouter] prompt cache usage: {'prompt_tokens': 10339, 'cached': 9500, 'hit_ratio': 0.918}`。
统一解析三种字段族：OpenRouter/OpenAI（`prompt_tokens_details.cached_tokens`）、
Anthropic（`cache_read_input_tokens`/`cache_creation_input_tokens`）、DeepSeek 直连
（`prompt_cache_hit_tokens`）。Gemini 循环同样接入（dict 形态 usage）。

---

## 三、改动清单

| 文件 | 函数/位置 | 改动 |
|------|-----------|------|
| `ai/agentic_loops.py` | `_openrouter_session_id`（新）、`_openrouter_extra_body`、`_merged_extra_body` | session_id 粘性路由 + 顶层自动 cache_control（P2/P3） |
| `ai/agentic_loops.py` | 主循环 / 回退 / 总结三处 create 调用 | 传入 chat_id 与 supports_prompt_cache |
| `ai/agentic_loops.py` | `_extract_cache_usage`（新）、`_log_cache_usage`（新） | 缓存命中观测（P7），两个循环出口接入 |
| `ai/attachment_content.py` | `_apply_cache_control` | 3 显式断点策略：system / 上一轮末尾 / 本轮 user（P3） |
| `context_manager.py` | `_quantized_drop`（新）、`select_request_context` | 量化淘汰，消息数与 token 双维度（P1） |
| `s3_utils.py` | `generate_presigned_url` | 预签名 URL 记忆化 + 并发签名去重（P4） |
| `search_engine.py` | `execute_web_search` / `_execute_web_search_uncached` | 拆分为缓存壳 + 原实现；`_search_cache_key` / `_is_cacheable_search_result`（新）（P5） |
| `subagent_tool.py` | 子 agent LLM 调用 | OpenRouter session_id + 顶层 cache_control（P2/P3） |
| `app.py` | `update_conversation_and_ledger`、模块常量 | 压缩批量化 + 阈值可配（P6） |
| `README.md` | 「缓存与 Prompt Cache」新章节 | 全部机制与配置项文档化 |

### 新增环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CONTEXT_EVICT_HEADROOM_MESSAGES` | `10` | 上下文窗口量化淘汰步长；`1` = 恢复旧版逐轮滑动 |
| `HISTORY_COMPACTION_TRIGGER` | `30` | 触发历史压缩的历史长度阈值（原硬编码 30） |
| `HISTORY_COMPACTION_MIN_BATCH` | `8` | 未归档工具调用攒够几条才压缩一次 |

已有但 previously-unused 的 `SEARCH_CACHE_TTL`（默认 300s）本次被正式启用。

---

## 四、效果预估

以一个典型"工具密集"会话为例（系统提示+工具 schema 约 6k token，历史 30k token，
每轮工具中段新增 3k token）：

| 场景 | 旧版命中 | 新版命中 |
|---|---|---|
| Anthropic：loop 第 2..N 轮 | system+工具+历史前段（≈6k+稳定段） | 同左 + 每轮工具结果跨轮命中 |
| Anthropic：下一轮用户消息 | 到上一轮 user 消息为止 | 到上一轮最后一条 tool 结果为止（多命中整轮工具中段） |
| DeepSeek/GLM（隐式）：窗口未滑动 | 基本全命中（与旧版相同） | 相同 |
| DeepSeek/GLM：历史>50 条后 | 每轮仅 system+tools 段命中，历史段全 miss | 约 10 轮里 9 轮历史段全命中（量化窗口） |
| OpenRouter 路由 | 首次命中后才粘性，且窗口滑动即断 | 从第一次请求即粘性，窗口滑动不断 |
| web_search 重复查询 | 每次真实调用 Serper | 300s 内直接返回缓存 |
| 含附件 URL 的多模态历史 | 每轮重签 URL → 前缀全断 | 有效期内前缀稳定 |

综合：Anthropic 路径的输入费用约可再降 30-60%（取决于工具密度）；隐式缓存厂商在
长会话中的历史段命中率从"几乎总是 miss"提升到"大多数轮全命中"。

---

## 五、验证

`python3 scripts/verify_cache_changes.py`（随包附带）覆盖 49 项行为断言，全部通过：

1. `_apply_cache_control`：三断点位置 / tool 消息不打标 / 幂等 / 多模态末块 / ≤3 显式断点；
2. extra_body：session_id 与 cache_control 的注入与隔离（非 OpenRouter 不注入）；
3. 量化淘汰：起点阶梯前进 / 连续 9 轮稳定 / step=1 恢复旧行为 / 孤立 tool 首条剔除 / token 触顶同样量化；
4. 搜索缓存：命中 / 参数归一化隔离 / 错误不缓存 / 空结果缓存；
5. 预签名 URL：同 key 字节稳定 / 不同 key 各签 / 自定义 expiry 绕过 / 并发签名一次；
6. usage 解析：三家字段族 / dict 与对象形态 / 命中率计算。

---

## 六、遗留建议（本次未做）

1. **1 小时 TTL**：Anthropic 显式断点可加 `"ttl": "1h"`（写 2x、读仍 0.1x）。适合
   低频（>5 分钟间隔）长会话；高频会话 5 分钟 TTL 每次命中都会刷新，已够用。
2. **`OPENROUTER_PROVIDER_SORT=price` 与粘性的相互作用**：粘性路由在"该 provider
   缓存读价更便宜"时才生效；若某个模型多家 provider 价格接近，仍可能偶发漂移。
   极致优化可对主力模型固定 `provider.order`（但会失去 fallback 能力，需权衡）。
3. **`_append_history_async` 每轮重解析历史附件**：图片走 base64 时字节稳定（不影响
   缓存），但 TTL 过期后会重新下载 Telegram/R2——可加大 `CACHE_TTL` 或做磁盘层。
4. **Gemini 显式 context cache**（`cachedContent`）：OpenAI 兼容层不支持，需要原生
   REST 调用才能用；当前隐式缓存已能吃到大部分收益。
5. **`_eligible_calls` 在 app.py 每轮 O(n) 扫描**：历史很长时有轻微开销，可接受；
   若将来历史上限大幅提高可加增量计数器。

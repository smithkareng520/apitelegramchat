# apitelegramchat

一个以 **Telegram 为交互入口的 Agentic AI 助手**，同时提供一个可由受信任宿主进程启动的 **stdio MCP Server**。

项目的核心不是“把一个聊天机器人接到模型 API”这么简单，而是把以下能力组合成一个可长期运行的个人/团队 AI 工作环境：

- Telegram Webhook + 富消息草稿流式输出
- 多模型 / 多厂商统一适配
- Tool Calling / Agentic Loop
- Web 搜索、网页正文抓取、Wikipedia、天气、汇率、新闻、书籍、加密货币等信息工具
- 地理编码、POI、路线、距离等地图能力
- 图片生成、参考图编辑、视频生成
- 私有 workspace、文本编辑器、文件上传/下载/发送
- 持久 Bash 会话与 Linux Landlock 沙箱
- Skills（项目内技能包）
- Subagent、Memory、Todo、Ask User 等 Agent 能力
- R2/S3 兼容对象存储
- 独立的最小权限 MCP 工具面
- 针对 Telegram 富消息容量、工具并发、上下文压缩和失败恢复的工程化处理

> **当前版本：2.2.0**
>
> 项目面向 Linux / Docker 部署。Bash 沙箱依赖 Linux Landlock（Linux 5.13+）；推荐直接使用项目提供的 Dockerfile。

---

## 目录

- [项目定位](#项目定位)
- [能力概览](#能力概览)
- [架构](#架构)
- [快速开始](#快速开始)
- [配置](#配置)
- [模型与厂商](#模型与厂商)
- [Telegram 使用方式](#telegram-使用方式)
- [Agent 工具](#agent-工具)
- [Workspace 与文件](#workspace-与文件)
- [Bash 沙箱](#bash-沙箱)
- [Skills](#skills)
- [外部 MCP](#外部-mcp)
- [MCP Server](#mcp-server)
- [数据目录与持久化](#数据目录与持久化)
- [打断保全（Turn Recovery）与统一草稿流](#打断保全turn-recovery与统一草稿流)
- [安全模型](#安全模型)
- [Docker 部署](#docker-部署)
- [Render 部署](#render-部署)
- [测试](#测试)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [开发说明](#开发说明)

---

## 项目定位

### Telegram Runtime

Telegram 是主要用户界面。收到消息后，Runtime 会：

1. 校验 Telegram 用户是否在白名单中；
2. 恢复该会话的历史、模型、角色和 workspace 状态；
3. 根据消息类型处理文本、图片、文档、音频、视频和回复引用；
4. 选择模型并建立模型上下文；
5. 调用 Agentic Loop；
6. 根据模型的 tool calls 执行工具；
7. 将工具状态和模型输出持续渲染为 Telegram 富消息草稿；
8. 在达到容量阈值时，在**完整模型回合边界**进行草稿滚动；
9. 最终将结果保留为 Telegram 消息。

### 统一事件源调度（USER / TIMER）

所有模型回合都走同一个调度入口 `get_ai_response(..., event_source=...)`，
按事件源动态分配工具面与输出通道，并共用同一份会话历史（统一上下文）：

| | USER（用户发消息） | TIMER（系统后台唤醒，`proactive.py`） |
|---|---|---|
| 触发 | 用户任意消息/命令 | 用户空闲约 20min 后进入"活动时间"，此后随机 10/20/40min 唤醒一次 |
| user 消息 | 用户输入写入历史 | 合成唤醒消息（`[系统后台唤醒] …`）只进请求上下文，**不写入历史** |
| 工具面 | `SEARCH_TOOLS`（不含 `send_message_to_user`） | 基础工具 + `send_message_to_user`（移除 `ask_user` / `present_files`） |
| 可见输出 | 富消息草稿流式 + 最终富文本推送 | **完全静默**：无草稿、无工具进度、最终文本不推送 |
| 打断 | 打断旧 USER 草稿（发"已停止"） | 用户消息打断 TIMER 回合：取消后台任务 + **静默撤回**该回合已发消息，不提示 |
| 休息节奏 | — | 用户连续 3h 无消息 → 停止高频触发，休息 1h 再触发一次 |

TIMER 回合里用户唯一可见的输出来自模型显式调用 `send_message_to_user`
（单工具 + `action` 参数）：`send` 发送、`edit` 编辑、`delete` 撤回；内容按
**普通纯文本**（不带任何格式）经 `sendMessage` 发送，像人随手发消息。
回合的 assistant/tool 消息正常沉淀进历史，保证用户下次回复时模型仍知道
后台做过什么；回合进行中被用户打断时，已发出的普通消息会被静默撤回
（含在途请求：`asyncio.shield` 保证发送完成注册后统一撤回，不留残留）。

### MCP Runtime

项目还提供：

```text
apitelegramchat-mcp
```

这是一个本地 stdio MCP Server。

MCP Server 与 Telegram Runtime 共用业务能力，但**不是把 Telegram Runtime 的所有能力原样暴露给 MCP**。它采用显式工具注册表和最小权限策略：

- 默认只暴露读取型工具；
- 高影响能力默认不进入工具列表；
- mutation 工具必须显式开启；
- 每个 MCP 进程必须拥有 host 生成的唯一 `scope`；
- workspace / state 以 scope 隔离；
- 工具参数使用显式 JSON Schema；
- 拒绝未声明字段。

这意味着 MCP Server 更适合作为 Claude Desktop、IDE Agent 或其他受信任本地 Agent Host 的后端能力提供者。

---

## 能力概览

| 能力 | 说明 |
|---|---|
| Telegram Bot | Webhook 接收消息，支持私聊/白名单控制 |
| 流式输出 | 模型输出持续更新 Telegram 富消息草稿 |
| 富消息 | 标题、段落、列表、表格、代码、引用、链接、图片、视频等 |
| 草稿滚动 | 在完整模型/工具回合边界安全切段，避免内容丢失 |
| 多模型 | OpenRouter、ModelScope、Gemini、Grok、DeepSeek、GLM、Agnes |
| Tool Calling | 并行工具调用、工具状态、结果压缩、错误恢复 |
| Web | 搜索 + 深度抓取 + Wikipedia |
| 信息工具 | 天气、汇率、新闻、书籍、Crypto、IP Geo、二维码 |
| 地图 | 地理编码、POI、路线、距离 |
| 文件 | Telegram 上传、workspace 编辑、文件发送 |
| Bash | 持久 Shell + Landlock + rlimit + fork bomb watchdog |
| 生成 | 图片生成、参考图编辑、视频生成 |
| Skills | 从 `.claude/skills` 等位置加载项目技能 |
| Subagent | 将复杂任务拆给独立 Agent Loop |
| 主动唤醒 | TIMER 事件源：空闲后后台"活动"，必要时用 send_message_to_user 主动发普通消息（可编辑/撤回），被打断时静默撤回 |
| Memory / Todo | 持久化私有记忆和任务 |
| 对象存储 | S3/R2 兼容的文件持久化与公开资源 URL |
| MCP | stdio Server + 外部 Streamable HTTP MCP Client |

---

## 架构

整体可以理解为四层：

```text
                         ┌──────────────────────────┐
                         │        Telegram          │
                         │  message / media / reply │
                         └────────────┬─────────────┘
                                      │ Webhook
                                      ▼
┌──────────────────────────────────────────────────────────────────┐
│                         Telegram Runtime                          │
│                                                                  │
│  app.py                                                          │
│    │                                                             │
│    ├── auth / whitelist                                          │
│    ├── media handling                                             │
│    ├── context / state                                            │
│    └── rich draft lifecycle                                       │
│             │                                                    │
│             ▼                                                    │
│  ai_handlers.py / ai/agentic_loops.py                            │
│             │                                                    │
│             ├── model provider                                   │
│             ├── tool calling                                     │
│             ├── context compaction                                │
│             └── subagent / ask-user                              │
│             │                                                    │
│             ▼                                                    │
│  tool_executors.py / search_engine.py / file handlers             │
│             │                                                    │
│      ┌──────┼──────────┬──────────┬───────────┐                  │
│      ▼      ▼          ▼          ▼           ▼                  │
│    Web    Maps       Files      Bash       Generation            │
└──────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                             ┌─────────────────┐
                             │ Workspace / R2  │
                             └─────────────────┘


                 ┌───────────────────────────────┐
                 │       Trusted MCP Host        │
                 └───────────────┬───────────────┘
                                 │ stdio
                                 ▼
                      apitelegramchat-mcp
                                 │
                    ┌────────────┴────────────┐
                    │ ToolRegistry / Context │
                    └────────────┬────────────┘
                                 │
                  ┌──────────────┼──────────────┐
                  ▼              ▼              ▼
               Search          Maps       Workspace
```

### 为什么 Telegram Runtime 与 MCP 要分开？

Telegram Agent 具有很多高影响能力，例如：

- Bash；
- 文件写入；
- 文件发送；
- 图片/视频生成；
- 子 Agent；
- memory/todo mutation。

这些能力适合在 Telegram Runtime 中结合用户确认、会话状态和业务策略使用，但不适合无条件进入 MCP 工具列表。

因此 MCP 采用独立的 `ToolRegistry`：

- `READ_ONLY_SPECS`：默认暴露；
- `MUTATION_SPECS`：只有显式 opt-in 才暴露。

---

## 快速开始

### 1. 环境要求

推荐：

- Linux
- Python >= 3.10
- Node.js >= 22.13
- npm >= 10
- Docker（生产部署推荐）
- Linux 5.13+，如果需要 Bash 沙箱

安装 Python 依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate

pip install -e .
```

项目同时包含文档/PDF Skill 所需的 Node 依赖：

```bash
npm install
```

### 2. 配置最小环境

至少需要：

```bash
export TELEGRAM_BOT_TOKEN="你的 Telegram Bot Token"
export WEBHOOK_TOKEN="随机且不可预测的 Webhook Token"
export WEBHOOK_URL="https://example.com/webhook"
export OPENROUTER_API_KEY="你的 OpenRouter API Key"
```

建议将所有 secret 放入部署平台的 Secret/Environment 管理，不要提交到 Git。

### 3. 启动

开发/本地：

```bash
PYTHONPATH=src \
python -m quart \
  --app apitelegramchat.app:app \
  run \
  --host 0.0.0.0 \
  --port 5000
```

健康检查：

```bash
curl http://127.0.0.1:5000/health
```

正常返回：

```json
{"status":"ok"}
```

### 4. Webhook

应用启动后会使用：

```text
WEBHOOK_URL?token=WEBHOOK_TOKEN
```

注册 Telegram Webhook。

生产环境请确保：

- `WEBHOOK_URL` 是 HTTPS；
- `WEBHOOK_TOKEN` 足够随机；
- 反向代理正确转发请求；
- `/health` 可供平台健康检查；
- 不把 MCP stdio 端口暴露到公网。

---

## 配置

### 核心环境变量

| 变量 | 必需 | 用途 |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | 是 | Telegram Bot API |
| `WEBHOOK_URL` | 是 | Telegram Webhook URL |
| `WEBHOOK_TOKEN` | 是 | Webhook 鉴权 token |
| `OPENROUTER_API_KEY` | 是（严格模式） | 默认模型厂商/主要模型入口 |
| `MODELSCOPE_API_KEY` | 可选 | ModelScope 模型 |
| `GEMINI_API_KEY` | 可选 | Gemini |
| `XAI_API_KEY` | 可选 | Grok |
| `DEEPSEEK_API_KEY` | 可选 | DeepSeek |
| `GLM_API_KEY` | 可选 | GLM |
| `AGNES_API_KEY` | 可选 | Agnes |
| `GROQ_API_KEY` | 可选 | 音频转写等能力 |
| `R2_ENDPOINT` | 可选 | S3/R2 Endpoint |
| `R2_ACCESS_KEY` | 可选 | R2 Access Key |
| `R2_SECRET_KEY` | 可选 | R2 Secret Key |
| `R2_BUCKET_NAME` | 可选 | Bucket |
| `R2_PUBLIC_URL` | 可选 | 对外资源 URL 前缀 |
| `R2_REGION` | 可选 | 默认 `auto` |
| `SERPER_API_KEY` | 可选 | Serper API key（搜索 / 搜图 / 搜视频 / 以图搜图 四合一，https://serper.dev） |
| `SERPER_API_TIMEOUT` | 可选 | Serper 单次请求超时秒数，默认 `12` |
| `APITELEGRAMCHAT_DATA_DIR` | 可选 | 数据根目录 |
| `APITELEGRAMCHAT_WHITELIST_FILE` | 可选 | 白名单文件 |
| `LOG_LEVEL` | 可选 | 日志级别，默认 `INFO` |

#### 主动唤醒（TIMER 事件源）

| 变量 | 默认 | 用途 |
|---|---|---|
| `PROACTIVE_ENABLED` | `true` | 主动唤醒总开关 |
| `PROACTIVE_INTERVAL_MIN_SECONDS` | `300` | 两次唤醒间隔下限（秒） |
| `PROACTIVE_INTERVAL_MAX_SECONDS` | `1200` | 两次唤醒间隔上限（秒，与下限构成随机 5~20min） |
| `PROACTIVE_MAX_IDLE_SECONDS` | `7200` | 用户连续多久没发消息 → 进入慢节奏（2h） |
| `PROACTIVE_REST_SECONDS` | `3600` | 慢节奏暂停时长：暂停 1h 再触发一次，用户仍无回应则继续（每 1h 看一眼） |
| `PROACTIVE_WATCH_DELAY` | `2` | 用户事件后的观望窗口（秒）：纯命令/按钮输入在窗口后没有 agent 回合接管时直接布置下一次唤醒 |
| `TOOL_VISIBILITY_FILTER` | `true` | 按事件源的工具可见性过滤器总开关。USER 回合把历史中 `send_message_to_user` 的调用痕迹折叠成普通文本摘要（保留语义、消除调用形状），从根源上防止模型在用户主动发消息时模仿调用该工具；TIMER 回合不受影响。设为 `false` 恢复旧行为。规则注册表见 `src/apitelegramchat/tool_visibility.py` |

说明：仅**私聊**参与主动唤醒；bot 被用户屏蔽（sendMessage 403）时会自动停用
该会话的调度；进程重启后调度重新开始（chat 在首次用户活动时重新被跟踪，
一开始就随机 5~20min 布置第一次唤醒）。

### 缓存与 Prompt Cache

项目里有两类缓存：**LLM 前缀缓存**（省钱的大头）与**工具/附件缓存**（省时延与配额）。

#### LLM 前缀缓存（prompt cache）

各厂商的启用方式不同，但共同前提都是：**请求前缀字节级一致**。

| 厂商 | 机制 | 本项目的处理 |
|---|---|---|
| Anthropic（经 OpenRouter） | 显式 `cache_control` 断点（上限 4 个）+ 顶层自动缓存 | system 末尾 1 个 + 尾部 2 个显式断点（覆盖本轮新输入 / 最新 tool 结果 / 上一轮末尾，**agentic loop 每轮请求前重打**，见 `attachment_content._apply_cache_control`）；`extra_body.cache_control` 开启自动缓存（断点随对话自动前移），叠加后不超 4 断点上限 |
| OpenRouter（全部模型） | Provider 粘性路由 | 每个请求携带 `session_id`（`tg-chat-{chat_id}-{纪元token}`，见下文"会话亲和键"），粘性路由从第一次请求就生效，不随压缩事件漂移 |
| DeepSeek / GLM / 智谱 | 服务端隐式缓存，无需标记 | 依赖前缀稳定：有界窗口 + 摊销式自动压缩 + 预签名 URL 记忆化 |
| Gemini（直连） | 隐式缓存（2.5+ 自动）+ 显式 `cachedContent` | 系统提示时间戳放在末尾，保证主体前缀逐字节稳定；另有显式缓存管理器（`gemini_cache.py`） |
| OpenAI 系 | 自动（>1024 token） | 同上，靠前缀稳定 |

##### 会话亲和键（session_id）

OpenAI 兼容网关的会话亲和键统一由 `state.get_llm_session_key(chat_id)` 生成，
语义如下：

- **同一个对话窗口 / 同一个任务共用一个 session_id**：主 agent 循环（含
  loop 内全部轮次与合成兜底请求）、子 agent、TIMER 主动唤醒回合，都使用
  同一个键（键在整个 loop 运行期内只解析一次，中途不漂移）。
- **用户点击"清空对话"（/clear）= 新建会话**：`safe_clear_history` 在清空
  历史的同一把锁内轮换会话纪元 token，生成全新的 session_id，旧会话的路由
  亲和性（sticky session / 副本粘性）与旧前缀缓存不再干扰新对话。进行中的
  旧请求用旧键自然完成，不受影响。
- 键格式：`tg-chat-{chat_id}-{12位hex纪元token}`（≤256 字符）；进程重启时
  历史同样清零，token 重新生成，语义上等同新会话。
- **下发范围**：
  | 路径 | 形式 |
  |---|---|
  | OpenRouter（主循环 / 子 agent） | `extra_body.session_id`（官方粘性路由） |
  | Agnes 聚合网关（多副本缓存隔离缓解） | `extra_body.session_id` + `X-Session-Id` 请求头（best-effort，双保险） |
  | Gemini 原生 / Anthropic 原生 / DeepSeek / GLM / ModelScope / Grok | 不使用 session_id：Gemini 走显式 `cachedContent` + 隐式缓存；Anthropic 走显式 `cache_control` 断点（单端点直连，无多副本路由问题）；其余为服务端隐式缓存，无会话概念 |

为保持前缀稳定，系统做了四件事：

1. **系统提示时间戳放末尾**（原有设计）：`当前时间` 是唯一每天必变的内容，放在 prompt 最后，前面的巨型稳定段（格式规范 + 工具通则 + 技能目录）每天都能命中缓存。
2. **有界会话窗口 + 摊销式自动压缩**（对齐 Claude Code / Cline 等主流 Agent 的上下文管理）：历史在预算内时**逐字节透传**（不是每轮滑动截尾）；当 `历史 + 新输入 > 90% 预算`（`CONTEXT_COMPACT_TRIGGER_RATIO`）时触发一次压缩事件——先无损归档较老的工具负载（payload → 指针，`text_editor` 可取回），不够再从最老的用户轮开始整轮淘汰（保护最近 `CONTEXT_PROTECTED_TURNS` 轮，默认 6），被淘汰轮合并进历史头部稳定槽位的滚动摘要（每轮一行 U/A/T 骨架，确定性生成无时间戳）——一次压回 50% 预算（`CONTEXT_COMPACT_TARGET_RATIO`），之后几十轮内不再触发，期间所有请求前缀完全一致。详见 `CACHE_OPTIMIZATION.md`。
3. **R2 预签名 URL 记忆化**：预签名 URL 每次重签字节都不同，会让历史消息中的多模态块（图片/视频 URL）打碎前缀缓存。现在同一对象在过期前 5 分钟内复用同一 URL。
4. **压缩只在事件内发生**：工具负载归档、轮次淘汰、摘要重写全部收敛到同一个压缩事件（`pre_flight_context_check`），同一个触发器、同一个预算口径；旧版"按消息条数 >30 攒批压缩"的独立触发路径已移除。

缓存命中观测：每轮请求结束后日志会输出一行 `prompt cache usage: {'Input_tokens': N, 'Output_tokens': N, 'Cached': N, 'Hit_ratio': xx.x%}`，命中率为 `Cached / Input_tokens`（OpenAI 兼容口径中 `prompt_tokens` 即纯输入 token 数，输出单独记录在 `completion_tokens`，分母不含输出；`cached_tokens` 是 `prompt_tokens` 的子集）；兼容 OpenRouter/OpenAI（`cached_tokens`）、Anthropic（`cache_read_input_tokens`）与 DeepSeek（`prompt_cache_hit_tokens`）三种字段。

#### 上下文窗口与自动压缩

历史预算 = `min(CONTEXT_BUDGET_RATIO × max_context, max_context − max_output)`；
请求侧守卫（`context_manager.py`）与压缩事件（`app.py::pre_flight_context_check`）
共用同一解析（`context_window.py::resolve_history_budget`）：

| 变量 | 默认 | 用途 |
|---|---|---|
| `CONTEXT_MAX_TOKENS` | 不设置 | 历史预算绝对覆盖（兼容旧语义） |
| `CONTEXT_BUDGET_RATIO` | `0.8` | 历史预算占 max_context 比例 |
| `CONTEXT_COMPACT_TRIGGER_RATIO` | `0.90` | 触发压缩事件的高水位（历史+新输入超过它即触发） |
| `CONTEXT_COMPACT_TARGET_RATIO` | `0.50` | 压缩事件要压回的目标水位（滞后区间，决定两次事件间隔） |
| `CONTEXT_PROTECTED_TURNS` | `6` | 永不淘汰的最近用户轮数 |
| `CONTEXT_DIGEST_TOKEN_BUDGET` | `1500` | 滚动摘要 token 预算（实际不超过预算的 1/4） |

行为验证：`python3 scripts/verify_context_strategy.py`（48 项断言：预算 / 分块 /
淘汰规划 / 摘要 / 守卫 / 多轮前缀稳定性模拟）。

#### 工具与附件缓存

| 缓存 | TTL / 容量 | 说明 |
|---|---|---|
| `fetch_url` 页面缓存 | `FETCH_CACHE_TTL`，默认 3600s，200 条 | 归一化 URL（去 fragment）；根路径回退结果也会写回缓存 |
| `web_search` 结果缓存 | `SEARCH_CACHE_TTL`，默认 300s，200 条 | 按归一化参数（mode/query/num/page/gl/hl/tbs）缓存格式化结果；服务错误不缓存，空结果缓存 |
| 图片/音频/文档/视频字节 | `CACHE_TTL`，默认 300s | 内存 TTLCache；R2 冷启动恢复 |
| R2 预签名 URL | 3300s（1h 有效期 - 5min 安全边际） | 见上文"前缀缓存" |
| 技能目录 | 进程生命周期 | `lru_cache`，`refresh_skill_cache()` 可清 |

### OpenRouter 路由

```bash
export OPENROUTER_PROVIDER_SORT=price
export OPENROUTER_ALLOW_FALLBACKS=true
export OPENROUTER_REQUIRE_PARAMETERS=false
```

其中：

- `OPENROUTER_PROVIDER_SORT`：Provider 路由排序；
- `OPENROUTER_ALLOW_FALLBACKS`：是否允许上游 fallback；
- `OPENROUTER_REQUIRE_PARAMETERS`：是否要求 provider 满足请求参数。

### 严格配置检查

默认情况下，模块可以在没有 Telegram 配置的环境中被导入，这对测试和 MCP 很有用。

如果部署环境希望启动时直接拒绝缺失配置：

```bash
export APITELEGRAMCHAT_REQUIRE_STRICT_CONFIG=true
```

严格模式会检查：

- `TELEGRAM_BOT_TOKEN`
- `WEBHOOK_TOKEN`
- `WEBHOOK_URL`
- `OPENROUTER_API_KEY`

---

## 模型与厂商

模型配置集中在：

```text
src/apitelegramchat/config.py
```

项目将“厂商能力”和“模型能力”分离。

支持的 Provider 包括：

| Provider | 环境变量 | 说明 |
|---|---|---|
| OpenRouter | `OPENROUTER_API_KEY` | 主要的 OpenAI-compatible 聚合入口 |
| ModelScope | `MODELSCOPE_API_KEY` | ModelScope inference |
| Gemini | `GEMINI_API_KEY` | Google Gemini |
| Grok | `XAI_API_KEY` | xAI |
| DeepSeek | `DEEPSEEK_API_KEY` | DeepSeek |
| GLM | `GLM_API_KEY` | 智谱 |
| Agnes | `AGNES_API_KEY` | Agnes |

模型配置可声明：

- vision
- audio
- video input
- tools
- native image
- native document
- native video
- search
- sampling
- prompt cache
- max output tokens
- max context

默认模型：

```text
agnes-2.5-flash
```

项目还支持根据：

```text
provider/model-id
```

自动发现部分未预先登记的模型，并使用该 Provider 的默认能力。

---

## Telegram 使用方式

目前 Runtime 支持的主要控制指令包括：

```text
/model
/role
/clear
/show
```

### `/model`

打开模型选择列表。

模型切换只允许在**私聊**中进行。

### `/role`

选择系统角色。

当前代码中提供：

```text
china
think
neko_catgirl
succubus
isla
```

角色是系统提示词的一部分，不是模型本身。

### `/clear`

清空当前会话历史，并同时清理当前 active skill 状态。

### `/show`

草稿预览开关（详见[打断保全与统一草稿流](#打断保全turn-recovery与统一草稿流)）：

```text
/show on    # 开启草稿预览（默认）：USER 与 TIMER 回合都实时展示富文本草稿
/show off   # 静默模式：过程不展示。交付分事件源：用户主动发消息时默认交付（send 缺省 true，收尾有兜底——兜底发送最后一条非空助手消息正文，与 deliver_reply 同源；显式 send=false 才静默）；TIMER 巡检时 send 缺省 false，模型经 deliver_reply(send=true) 自主交付
/show       # 查看当前状态
```

### 回复消息

用户可以直接回复 Telegram 中已有消息。

Runtime 会提取：

- 引用文本；
- 被引用图片；
- 文档；
- 音频；
- Voice；
- 视频；
- Video Note。

然后把引用内容作为当前 Agent 输入的一部分。

---

## Agent 工具

Telegram Runtime 的工具面比 MCP 更完整。

主要工具包括：

### Web / 信息

```text
web_search    # 4-in-1: search / images / videos / lens（mode 参数选择）
fetch_url
wikipedia
exchange_rate
book_lookup
weather
news
crypto_price
qr_code
```

### 地图

```text
geocode
route
distance
poi_keyword_search
poi_nearby_search
poi_details
```

地图坐标统一使用：

```text
longitude,latitude
```

例如：

```text
116.397128,39.916527
```

### 工具返回的「模型视图」精简

工具的原始返回同时服务两个消费者：Telegram 工具卡片的 UI 渲染
（`tool_executors.format_tool_result`）与 LLM 上下文（`role=tool` 消息）。
两者对字段的诉求不同——UI 要展示完整信息（月相、露点、逐时表格等），
而模型只需要回答问题所需的高价值字段。把上游 API 响应一股脑塞进上下文
会浪费 token、挤占截断预算、降低模型定位关键信息的效率。

`tool_result_condense.py` 统一承载这层「按价值筛选」：

| 工具 | 处理 |
|---|---|
| `weather` | 逐时条数按 `hours` 参数生效（默认 6，1-24）；逐时/逐日只保留高价值字段（温度/天气/降水/风力/日出日落等），删除月相、露点、热指数、风寒、短波辐射与低频概率字段 |
| `subagent` | 删除 `task_preview`（父 agent 任务回声）与 `model_name`（与 `model` 重复） |
| 地图 6 工具 | 高德 MCP 输出在源头清洗（`_call_amap_mcp` 返回前）：删除 `polyline` 坐标串 / `tmcs` 分段路况 / 全部空值字段，POI `photos` 只保留第一张 URL |

关键设计：

- **双视图**：UI 草稿从完整返回渲染（展示不变），发给模型与写入历史的
  是精简视图（`tool_call_loop.run_one` 中生成 `llm_content`）；
- **先精简、后截断**：token 截断预算只花在有价值的字段上，避免 24h 低价值
  逐时数据 / 路线坐标串把 POI 名称、导航步骤挤出模型视野；
- **零误伤保底**：错误文本（`Error:` / `❌` / `失败：` 前缀）、非 JSON 内容、
  解析失败或 schema 变化时一律原样透传，错误连击熔断与失败判定不受影响；
- **子 agent 同源**：`subagent_tool._execute_tool_for_subagent` 的工具结果
  走同一套精简（子 agent 的 20k token 预算更紧张）；
- 地图清洗对 UI 逐字节等价（`scripts/verify_tool_condense.py` 内含
  UI 等价性断言），其余工具（web_search / bash / memory / todo /
  text_editor / 生成类等）返回本身就是精简过的，不做二次处理。

### Workspace / 文件

```text
text_editor
bash
present_files
```

说明：每个会话只有一个 workspace 根目录，bash、text_editor 和文件展示工具都以它解析相对路径。`upload/` 与 `download/` 是该根目录的两个子目录，bash / text_editor 可直接通过相对路径读写（`cat download/x.pdf`、`cp out.txt upload/out.txt`、`ls -la upload/`）。发送文件时，`present_files` 也使用 workspace-relative 路径，例如 `present_files(["upload/out.txt"])`。

### 生成

```text
generate_image_from_text
edit_image_with_reference
generate_video
```

此外，Agent Runtime 还包含：

- Memory；
- Todo；
- Subagent；
- Message User（原 Ask User，提问 / 通知双用途）；
- Deliver Reply（静默模式下的最终回复交付）；
- Skills；
- 上下文压缩；
- 工具结果摘要。

这些能力并不全部作为 MCP 工具暴露。

---

## Workspace 与文件

每个聊天会话拥有独立 workspace。

逻辑上类似：

```text
<data-root>/
├── workspaces/
│   └── <chat-or-scope>/
│       ├── upload/
│       ├── download/
│       ├── runtime/
│       └── ...
└── state/
```

具体路径由：

```text
src/apitelegramchat/workspace_paths.py
```

统一管理。

### `upload/` 与 `download/`

它们是 workspace 根目录下的两个特殊子目录，而不是普通工作目录。workspace 根目录是 Shell 的固定起始目录，也是所有相对路径的唯一解析根；`present_files` 的参数同样相对于此根目录。

- `download/`：Telegram 上传但尚未进入 workspace 的文件；
- `upload/`：准备发送给用户的文件。

Bash **禁止把这两个目录作为工作目录执行命令**，避免：

- 模型把依赖安装到待发送文件目录；
- 用户上传文件被命令意外污染；
- 产物 staging 区被运行时文件污染。

正确流程通常是：
```text
Telegram 上传
    ↓
download/（bash / text_editor 直接读取）
    ↓
Agent/Bash 编辑
    ↓
upload/（bash `cp 产物 upload/xxx`）
    ↓
present_files(["upload/xxx"])
    ↓
Telegram
```

---

## Bash 沙箱

Bash 是本项目风险最高的工具之一，因此它不是普通的：

```python
subprocess.run(...)
```

而是一个持久的、受限制的 Bash Session。

### 安全边界

每个聊天拥有自己的 workspace。

Bash 子进程启动前会：

1. 设置 `PR_SET_NO_NEW_PRIVS`；
2. 安装 Landlock；
3. 限制文件系统访问；
4. 设置 CPU、文件大小、打开文件数等 `rlimit`；
5. 使用独立 process group；
6. 启动 fork bomb watchdog；
7. 不继承应用层 secret 环境变量。

Landlock 的核心原则：

```text
workspace      → 可读写
/usr /bin ...  → 只读 + 执行
其他应用私有目录 → 默认拒绝
```

因此 Bash 可以使用镜像中已经存在的 Python、gcc、cmake 等工具，但不能随意读取：

- Bot Token；
- Provider API Key；
- 应用源码之外的私有状态；
- 其他用户 workspace。

### 资源限制

默认值：

```bash
SANDBOX_MAX_PROCS=50
SANDBOX_MAX_CPU_SEC=300
SANDBOX_MAX_FILE_SIZE=104857600
SANDBOX_MAX_OPEN_FILES=256
SANDBOX_TIMEOUT_SEC=300
SANDBOX_OUTPUT_MAX_CHARS=80000
```

`SANDBOX_OUTPUT_MAX_CHARS` 控制单条 Bash 命令返回内容的字符上限。超限时**保留开头与结尾、只省略中间**（编译错误、traceback 几乎总在末尾，纯头部截断会把失败原因默默丢掉），并在结果中插入明确的省略说明。设为 `0` 可关闭限制（不建议：狂刷输出的命令会撑爆内存与模型上下文）。返回给模型的最终 token 预算仍由 `TOOL_RESPONSE_TOKEN_BUDGET`（默认 20000，bash 同样走头尾保留）兜底。

### 网络与 curl/wget

沙箱（Landlock）只限制文件系统，**不拦截出站网络**。`curl`/`wget`/`git`/`jq`/`zip` 已直接加入 Dockerfile；对暂未重建镜像的存量部署，应用会在 bash 会话启动时自动向 workspace 的 runtime bin（PATH 首位）安装纯 Python 标准库实现的 `curl`/`wget` 兜底 shim——镜像里一旦出现真二进制，shim 自动让位。工具描述中也已声明这些能力，避免模型先撞一次 `command not found` 再换写法、浪费工具调用。

### 为什么不用 bubblewrap？

项目刻意不依赖 `bwrap`。

目标部署环境可能禁止 unprivileged user namespace，而 Landlock 可以在不需要 privileged container / `CAP_SYS_ADMIN` 的情况下工作。

---

## Skills

项目支持从 `.claude/skills` 等位置发现 Skill。

Skill 可以包含：

```text
SKILL.md
scripts/
assets/
references/
```

Skill runtime 会：

1. 扫描候选 Skill 根目录；
2. 读取 frontmatter；
3. 建立 Skill catalog；
4. 按需读取完整 Skill；
5. 必要时将 Skill assets 同步到 workspace。

当前仓库已经包含一套文档相关 Skill，例如 DOCX 处理脚本和 Office XML 工具。

Skill 不应被当作普通 Python import。它更接近 Agent 可以按需加载的“操作手册 + 工具资源”。

---

## 外部 MCP

项目使用 Streamable HTTP MCP Client 调用外部服务（仅保留高德地图）。

### 搜索（直连 Serper 官方 REST API）

网页搜索已从第三方 ModelScope MCP 迁移到 Serper 官方 REST API
（`https://google.serper.dev`），通过 `X-API-KEY` 头鉴权，避开上游
MCP 网关经常出现的"响应体中途被截断 / SSE 流被立即关闭 / 调用挂死"
等不稳定行为。

一个 `SERPER_API_KEY` 即可同时支持 4 种搜索模式：

| mode    | 端点                          | 用途             |
|---------|-------------------------------|------------------|
| search  | `POST /search`                | 普通网页搜索     |
| images  | `POST /images`                | 文字搜图         |
| videos  | `POST /videos`                | 视频搜索         |
| lens    | `POST /lens`                  | 以图搜图（反向） |

```bash
# Key 从 https://serper.dev 注册并获取，配置在 Render Environment 中作为 secret
export SERPER_API_KEY="..."
# 可选：单次请求超时（秒），默认 12s
export SERPER_API_TIMEOUT=12
```

调用方式：通过 `web_search` 工具的 `mode` 参数选择搜索类型，可传单个
字符串或字符串数组（多模式并发执行）。

```text
web_search({query: "特斯拉 model y", mode: ["search", "images", "videos"], num_results: 5})
web_search({image_url: "https://example.com/photo.jpg", mode: "lens"})
```

### 地图

高德 MCP：

```bash
export GAODE_MCP_ENABLED=true
export GAODE_MCP_URL="https://mcp.api-inference.modelscope.net/<deployment-id>/mcp"
export GAODE_MCP_TOKEN="..."
```

如需限制允许的主机：

```bash
export GAODE_MCP_ALLOWED_HOSTS="mcp.api-inference.modelscope.net"
```

### Serper API 错误诊断

直连 serper.dev 会区分：

| 状态 | 含义 |
|---|---|
| 401 / 403 | `SERPER_API_KEY` 错误、过期或未配置 |
| 404 | endpoint 不存在/失效（serper.dev 改了路径段） |
| 429 / quota / rate limit | 上游限流或额度限制（serper.dev 免费版 2,500 次/月） |
| 502 / 503 / 504 | serper.dev 上游临时不可用 |
| 其他 4xx | 参数或工具配置被拒绝 |
| timeout | 本地到 serper.dev 网络不通或响应过慢 |

特别注意：

> `502/503/504` 不能证明额度用完。serper.dev 的限流会明确返回 429。

---

## Web 搜索域名过滤

配置文件：

```text
src/apitelegramchat/web_search_settings.py
```

黑名单规则支持三种形式：

```text
example.com
[*.]example.com
*.example.com
```

语义分别是：

| 规则 | 匹配 |
|---|---|
| `example.com` | 只匹配根主机 |
| `[*.]example.com` | 根域名 + 所有子域名 |
| `*.example.com` | 只匹配子域名 |

相关配置：

```text
WEB_SEARCH_DOMAIN_FILTER_ENABLED
WEB_SEARCH_UPSTREAM_DOMAIN_EXCLUDE_ENABLED
WEB_SEARCH_DEFAULT_RESULTS
WEB_SEARCH_MAX_RESULTS
WEB_SEARCH_CANDIDATE_MULTIPLIER
WEB_SEARCH_MAX_CANDIDATES
WEB_SEARCH_REGION
WEB_SEARCH_LANGUAGE
```

最终 URL 仍会经过本地过滤。

因此，上游搜索引擎的 exclude 只是优化，**本地最终过滤才是安全边界**。

---

## URL 根路径回退

部分网站：

```text
https://example.com/
```

抓取失败，但：

```text
https://example.com/index/
```

可以读取。

因此 `fetch_url` 支持根路径回退。

配置：

```text
FETCH_URL_ROOT_FALLBACK_ENABLED
FETCH_URL_ROOT_FALLBACK_PATHS
```

默认候选路径包含：

```text
/index/
```

回退只适用于：

- HTTP(S)；
- 根路径；
- 不带 query；
- 不带 fragment；
- 同一个 host。

不会把深层 URL、跨站 URL 或带参数 URL 改写成其他地址。

---

## MCP Server

### 启动

安装项目后：

```bash
apitelegramchat-mcp
```

但启动前必须设置：

```bash
export APITELEGRAMCHAT_MCP_SCOPE="opaque-host-session-xxxxxxxx"
export APITELEGRAMCHAT_DATA_DIR="/var/lib/apitelegramchat"
```

`scope` 必须由受信任的 host 生成，建议使用随机、不可预测的 opaque identifier。

### 为什么强制 scope？

MCP Server 不接受“没有 scope 就使用默认 workspace”的设计。

否则多个 MCP session 很容易共享：

```text
同一个 workspace
同一个 memory
同一个 todo
同一个 state
```

这会造成严重的数据串扰。

因此：

> 没有合法 `APITELEGRAMCHAT_MCP_SCOPE`，MCP Server 应拒绝启动。

### 默认 MCP 工具

默认只开放读取能力：

```text
search.web
search.fetch
search.wikipedia
search.exchange_rate
search.book_lookup
search.weather
search.news
search.crypto_price

geo.geocode
geo.route
geo.distance
geo.poi_keyword_search
geo.poi_nearby_search
geo.poi_details

workspace.view
```

### Mutation 工具

以下能力默认关闭：

```text
memory.manage
todo.manage
workspace.edit
workspace.present
```

显式开启：

```bash
export APITELEGRAMCHAT_MCP_ENABLE_MUTATIONS=true
```

即使开启，也建议 MCP Host 在：

- 删除；
- 覆盖；
- 上传；
- 外部发送；

之前自行向用户展示参数并取得确认。

### Bash 为什么不在 MCP？

这是刻意的权限边界。

Telegram Runtime 的 Bash 受到：

- chat isolation；
- 用户会话；
- sandbox；
- quota；
- Agent runtime；

等约束。

MCP Server 不把它直接暴露给任意 MCP Host。

---

## 数据目录与持久化

建议为生产环境设置：

```bash
export APITELEGRAMCHAT_DATA_DIR=/var/lib/apitelegramchat
```

并确保该目录只允许应用用户访问。

目录权限由项目按 `0700` 方向创建私有运行目录。

### 不建议

不要把：

```text
/app
```

或：

```text
/home/app
```

直接作为 workspace。

也不要把：

- Bot Token；
- API Keys；
- Render 配置；
- Docker secret；
- MCP token；

放进 workspace。

### R2 / S3

R2 用于保存需要长期保留或生成公开 URL 的资源。

典型配置：

```bash
export R2_ENDPOINT="https://<account>.r2.cloudflarestorage.com"
export R2_ACCESS_KEY="..."
export R2_SECRET_KEY="..."
export R2_BUCKET_NAME="..."
export R2_PUBLIC_URL="https://cdn.example.com"
export R2_REGION="auto"
```

Bash sandbox 不直接拥有这些 credentials。

---

## 富消息与草稿滚动

Telegram 的富消息不是简单的“不断 edit 一条消息”。

项目实现了一个完整的 Draft Lifecycle。

核心规则：

```text
接近容量
    ↓
只标记 rollover_pending
    ↓
完成当前模型返回
    ↓
等待该返回中的全部并行工具完成
    ↓
关闭工具组并刷新最终状态
    ↓
永久化旧段
    ↓
创建新 draft_id
    ↓
发送新草稿首帧
    ↓
再开始下一次模型请求
```

### 为什么必须在回合边界滚动？

假设一个模型返回：

```text
assistant
  ├── tool_call A
  ├── tool_call B
  └── tool_call C
```

如果在 A 完成时就滚动，B/C 可能被放到另一个草稿。

用户看到的工具活动就会被人为拆散。

因此项目定义：

> 一个完整回合 = 一次 assistant 模型返回 + 该返回产生的全部并行工具调用均进入终态。

只有到这里，才允许 rollover。

### 无损交接

滚动阶段使用 handoff buffer。

如果旧段永久化失败：

```text
旧 draft_id 保留
handoff 恢复
pending 保留
下一完整回合再次尝试
```

因此不会出现：

```text
旧消息少了一截
+
新草稿也没有这一截
```

的双重丢失。

详细设计见：

```text
Telegram#U5bcc#U6d88#U606f#U8349#U7a3f#U6eda#U52a8#U7b56#U7565.md
```

---

## 打断保全（Turn Recovery）与统一草稿流

本轮重构引入两个核心机制：**轮次打断保全**（agent 进度不再因打断丢失）
与 **/show 统一草稿流**（USER 与 TIMER 事件源走同一套草稿与交付流程）。

### 1. 打断保全：旧草稿正常关闭，新草稿从断点继续

旧行为：用户发新消息（或 TIMER 唤醒）会**取消并丢弃**进行中的 agent
轮次——已完成的 assistant 消息、工具调用与结果全部作废。

新行为（`turn_recovery.py`）：`get_ai_response` 开始时登记一份**轮次日志
（turn journal）**，agentic 循环把已完成的消息持续追加进去：

- **正常收尾**：`update_conversation_and_ledger` 在写历史的同一把 chat 锁
  内注销登记（`note_turn_persisted` 与消息 append 连成原子区间，取消
  竞态下既不双写也不漏写）；
- **被用户消息 / TIMER 打断**：打断方（`_interrupt_active_generation` /
  `proactive.interrupt_proactive_flow`）在旧任务完全停止后调用
  `finalize_interrupted_turn`，把 journal 沉淀进持久历史；
- **异常中断（额度不足 / 网关错误等）**：`get_ai_response` 的异常路径调用
  `persist_salvaged_journal` 保全进度，下一轮从断点继续。

补齐结构（保证历史合法，OpenAI 格式）：

- assistant 里的每个 `tool_calls[i].id` 都必须有配对的 `role=tool` 消息；
  打断时未执行 / 被取消的调用统一补占位结果：`"用户打断，未执行"`；
- `tool_call_loop` 在取消路径上先把**已执行完**的工具回填真实结果，
  只剩真正未完成的才落占位——最大化保留进度。

新 user 消息落位规则（`persist_user_message_entry`，轮次开始时提前持久化）：

| 打断时机 | 历史末尾 | 新消息处理 |
|---|---|---|
| 任何 assistant 输出之前 | user | **合并**进上一条 user（content 拼接、附件数组拼接），避免连续两条 user |
| tool_call 已发出、工具未执行 | assistant(tool_calls) | 补占位 tool 消息 → 新 user 消息**追加** |
| 工具执行中 | assistant + 部分 tool | 已完成的回填真实结果、其余占位 → 新 user 消息**追加** |
| 工具结果已回填、模型生成中 | tool | 无需补结构，新 user 消息**追加**（OpenAI 允许 tool 后跟 user） |

多模态打断：用户打断时发的图片 / 文档，附件元数据（file_ids / attachments）
与文本一起合并或追加，不丢内容。

同时移除了打断时的「⏹️ 已停止输出」提示消息——进度已保全在历史中，
该提示不再有价值；旧草稿的内容则由旧任务在取消路径上经 sendRichMessage
**固定为永久消息**（`RichMessageBuilder.finalize_interrupted_draft`，与
正常最终交付、滚动永久化同源同法：同样的内容构建、同样的交付通道、
送达后清理草稿气泡；取消发生在固定化进行中时由 `asyncio.shield` 保证
后台完成）。固定失败或尚无可见内容时，才退回旧行为——旧草稿冻结
（mark dead + 保留标记）作为进度现场。

### 2. /show 指令：统一草稿显示开关

```text
/show        # 查看当前状态
/show on     # 开启草稿预览（默认）
/show off    # 关闭草稿预览（静默模式）
```

开关对 **USER 回合与 TIMER 主动巡检回合统一生效**——后台随机事件与
用户主动消息走相同流程：

| 模式 | USER 回合 | TIMER 回合 |
|---|---|---|
| /show on | 富文本草稿实时展示，最终回复自动送达 | 同左（统一流程） |
| /show off | 静默运行；`deliver_reply` 的 send **缺省 true**：不填 / send=true 均交付（不调用时收尾由系统兜底发送最后一条非空助手消息正文，与工具交付同源），只有显式 send=false 才本轮不发送任何内容；`message_user` 提问/留言 | 静默运行；send **缺省 false**（旧行为）：模型经 `deliver_reply`（显式 send=true）自主交付最终内容，不填 / false / 不调用均不发送，无兜底；`message_user` 提问/留言 |

### 3. 工具变更：send_message_to_user 移除，ask_user → message_user

- **send_message_to_user 已整体移除**（工具定义、executor、消息撤回注册表、
  tool_visibility 折叠规则等全部清理）。若模型带着旧历史幻觉调用，返回
  迁移指引（改用 message_user / deliver_reply）。
- **ask_user 更名为 message_user**，意图扩展为「向用户发消息并等待回复」：

  - 带选项：按钮提问卡（与原 ask_user 一致）；
  - 不带选项：**给用户发消息模式**——像现实中给同学发一条消息：发送后
    等待用户自由回复，用户任意文本回复即回填工具结果，原轮次继续
    （提问卡同样支持直接打字回答，无需先点「自定义回答」）；
  - 超时（默认 2 分钟，`ASK_USER_TIMEOUT` 可配）：返回
    `{"type":"expired"}`——含义是**用户当前不在**（就像发消息等了两分钟
    没人回），不是错误；模型据此自然收尾，用户回来后下一条消息会重新
    触发对话。超时后**发消息模式的消息卡片会被编辑成纯文本正文本身**
    （去掉「📨 助手消息」标题与「会自动过期」提示，也不显示「用户未
    回复」状态），安静地留在聊天记录里；提问卡则显示「用户未回复」。

- **deliver_reply 工具**（仅 /show off 静默模式可用）：模型通过 **send 布尔
  参数**做「发 / 不发」的决策，发送的是 **agent 轮次最后一条助手消息的
  content 字段本身**（不含 reasoning 等其他字段），不必把正文重复写进工具
  参数。正确用法：先把完整、自包含的最终回复直接写成消息正文，再在同一条
  消息里调用 `deliver_reply`，系统即把该正文经 sendRichMessage 作为永久
  富文本消息交付（不经过草稿）。send 的**缺省值（不填）按事件源区分**，
  且每轮 agent 开始时重置（上一轮交付或抑制与否不影响本轮）：

  - **USER 回合**（用户主动发消息）：缺省 **true**——不填 / send=true 均发送；
    模型整轮不调用时，收尾由系统按默认 true 兜底发送最终回复（用户主动
    提问理应收到回答）——兜底与工具交付**同源**，发送的都是 agent 轮次
    最后一条非空 assistant 消息的 content 本身（经 sendRichMessage 直发，
    不使用整轮草稿累积，也不附带中间轮次的过程文本与工具卡片）；只有
    显式 `send=false` 才本轮完全静默（无兜底）。
  - **TIMER 回合**（后台主动巡检）：缺省 **false**——不填 / false / 不调用
    均不发送（与旧行为一致），必须显式 `send=true` 才交付，收尾无兜底。

  因为 /show on 时该工具不进入工具面、模型看不到也就不会调用，除了草稿
  外不会产生单独 content。静默模式下模型的流式输出不会实时送达用户；
  USER 回合有默认交付兜底（显式 send=false 除外），TIMER 回合则完全
  没有兜底——不显式 send=true，本轮就对用户完全静默。交付成功后系统会
  明确告知模型不要再调用、也不要输出「已发送/已确认」之类的确认正文，
  避免冗余回执消息链。
- **deliver_reply 工具插拔**：该工具只在静默回合的工具面中暴露；非静默
  回合（/show on）出站历史副本中已有的 deliver_reply 调用痕迹（assistant
  的 tool_calls 与配对的 tool 消息）会被成对拔除，避免模型模仿调用一个
  当前不可用的工具；回到静默回合时痕迹在原位置原样插回。持久历史本身
  从不被改动（见 `tool_visibility.SILENT_ONLY_TOOLS`）。

### 4. TIMER 主动巡检（proactive）适配

- 唤醒节奏重构为**事件驱动单 timer 模型**（无轮询状态机、无复杂计算）：
  - 一开始（首次跟踪 chat）就随机 **5~20min** 布置第一次唤醒；
  - timer 到点触发一次 TIMER 回合，**回合结束后**再随机 5~20min 布置下一次；
  - 用户发送任何消息：先取消挂起的 timer（不提前触发），等当前 agent 回合
    完整结束后再随机 5~20min 布置下一次。用户消息打断 TIMER 回合的情形
    同理：打断 → turn_recovery 保全 → 新 USER 回合正常续上 → 该回合结束时
    再随机 5~20min 下一次（打断只重算下一次，不丢节奏）；
  - **2 小时**内用户都没有主动发消息：暂停 **1 小时**再触发，之后保持
    "每 1h 看一眼"的慢节奏（用户一回来立即恢复正常 5~20min）；
  - `/clear` 后 timer 重置为随机 5~20min 下一次；
  - 纯命令 / 按钮输入（不产生 agent 回合）：短暂观望窗口后直接布置下一次。
- WAKEUP_PROMPT 重写：不再依赖 send_message_to_user，改为 message_user
  （交互）+ deliver_reply（静默交付）；
- TIMER 回合被用户消息打断时，取消任务并经 turn_recovery 保全该回合
  已完成的进度（不再是整轮丢弃），全程静默。

### 5. 富文本原生按钮

系统提示词已加入 Telegram Bot API 10.3 的原生 Rich Message 按钮结构
（RichBlockButtons / RichTextButton / RichMessageButton）：

```html
<tg-button-row>…</tg-button-row>
```

按钮行必须作为独立块级元素输出，不得嵌套在段落 / 表格 / 列表 / 引用内。

---

## Docker 部署

直接构建：

```bash
docker build -t apitelegramchat .
```

运行：

```bash
docker run --rm \
  --env-file .env \
  -p 5000:5000 \
  apitelegramchat
```

Docker 镜像包含：

- Python 3
- Node.js 22
- gcc/g++
- cmake
- ccache
- LibreOffice
- Pandoc
- ImageMagick
- Tesseract
- Poppler
- qpdf
- PDF/DOCX Skill 所需依赖

应用以非 root 用户运行：

```text
app:app
UID/GID 2000
```

容器默认：

```text
PORT=5000
APITELEGRAMCHAT_DATA_DIR=/tmp/apitelegramchat_data
```

生产环境如果需要持久化状态，请将 data directory 映射到持久化磁盘或使用对象存储承载需要长期保存的文件。

---

## Render 部署

仓库提供：

```text
render.yaml
```

可以通过 Render Blueprint 部署。

主要步骤：

1. 将仓库推送到 GitHub/GitLab；
2. 在 Render 创建 Blueprint；
3. 选择仓库；
4. Render 读取 `render.yaml`；
5. 在 Environment 中填写 Secret；
6. 等待 Docker build；
7. 检查 `/health`；
8. 确认 Telegram Webhook 正常。

生产环境建议把以下变量设置为 Secret：

```text
TELEGRAM_BOT_TOKEN
WEBHOOK_TOKEN
OPENROUTER_API_KEY
DEEPSEEK_API_KEY
GEMINI_API_KEY
XAI_API_KEY
GLM_API_KEY
GROQ_API_KEY
MODELSCOPE_API_KEY
AGNES_API_KEY
R2_ACCESS_KEY
R2_SECRET_KEY
SERPER_API_KEY
GAODE_MCP_TOKEN
```

不要把 token 放在 `render.yaml` 的明文配置中。

---

## 测试

运行完整单元测试：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

当前测试覆盖包括：

- MCP scope 强制；
- 私有目录权限；
- MCP 默认最小权限工具表；
- workspace 符号链接边界；
- 资源不泄露绝对 workspace 路径；
- 外部 MCP endpoint allowlist；
- MCP SDK 请求处理器注册；
- Web 搜索黑名单域名匹配；
- 搜索结果最终过滤；
- URL 根路径回退。

脚本级回归测试（无额外依赖，直接运行）：

```bash
PYTHONPATH=src python scripts/test_text_editor.py         # text_editor 回归（含 CRLF 行尾保真 / 路径安全 / 输出上限，见 TEXT_EDITOR_FIXES.md）
PYTHONPATH=src python scripts/test_tool_args_pipeline.py  # 工具参数四层管线（strict/repair/validate/信封）
PYTHONPATH=src python scripts/test_run_one_gate.py        # 执行闸门集成（缺必填不执行、类型错误回传等）
```

---

## 项目结构

```text
.
├── src/
│   └── apitelegramchat/
│       ├── app.py                    # Quart Webhook / Telegram Runtime
│       ├── config.py                 # Provider / Model / runtime config
│       ├── state.py                  # 会话状态
│       ├── context_manager.py        # 请求侧上下文守卫（预算内全量透传，超预算按块裁剪出站视图）
│       ├── context_window.py         # 上下文窗口核心：预算解析/双水位/淘汰规划/滚动摘要
│       ├── token_budget.py            # token 预算
│       ├── workspace_paths.py        # workspace 路径边界
│       ├── workspace_utils.py        # workspace 操作
│       ├── sandbox.py                 # Landlock + rlimit + watchdog
│       ├── tool_executors.py         # 工具执行与结果渲染
│       ├── search_engine.py          # Web / 信息 / 地图 / 生成能力
│       ├── fetch_rich_content.py     # HTML → Telegram Rich HTML
│       ├── fetch_url_fallback.py     # URL 回退
│       ├── file_handlers.py          # Telegram 文件处理
│       ├── s3_utils.py               # S3/R2
│       ├── skills.py                 # Skill discovery/runtime
│       ├── memory_tool.py            # Memory
│       ├── todo_tool.py              # Todo
│       ├── subagent_tool.py          # Subagent
│       ├── message_user_tool.py      # message_user 工具（提问/给用户发消息）
│       ├── ai/
│       │   ├── agentic_loops.py      # Agent loop
│       │   ├── tool_call_loop.py     # Tool call orchestration
│       │   ├── rich_message_builder.py
│       │   ├── attachment_content.py
│       │   ├── media_generation.py
│       │   ├── tool_summary.py
│       │   └── ...
│       ├── mcp/
│       │   ├── registry.py           # MCP Tool Registry
│       │   ├── context.py            # MCP scope context
│       │   ├── resources.py          # MCP resources
│       │   └── server.py              # stdio MCP server
│       ├── mcp_client.py             # 外部 MCP client
│       └── entrypoints/
│           └── mcp_server.py         # console entrypoint
│
├── tests/
├── .claude/
│   └── skills/                       # bundled Skills
├── Dockerfile
├── render.yaml
├── pyproject.toml
├── requirements.txt
├── package.json
└── README.md
```

---

## 常见问题

### 1. Bot 能启动，但用户收到“未授权访问”

检查白名单文件。

默认白名单存储由：

```text
APITELEGRAMCHAT_WHITELIST_FILE
```

决定；相对路径会挂到：

```text
APITELEGRAMCHAT_DATA_DIR
```

下。

白名单同时支持 Telegram username 和 user ID。

---

### 2. MCP Server 启动失败，提示 scope

设置：

```bash
export APITELEGRAMCHAT_MCP_SCOPE="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
```

不要让 MCP Client 把 scope 当成模型上下文参数。

scope 是 host/session 的隔离标识。

---

### 3. Bash 在本机无法运行

首先确认：

```bash
uname -r
```

Linux 内核需要支持 Landlock。

如果运行环境不支持 Landlock，项目不会把 sandbox 当成“最好有、没有也能跑”的软限制，而是倾向于拒绝启动受保护的 Bash 子进程。

这是有意设计的 fail-closed 行为。

---

### 4. 搜索返回 502 / 超时

502 或超时通常意味着 serper.dev 上游或本地到上游的网络暂时不可用。

不要直接把它解释成 quota 用尽。

检查：

```text
SERPER_API_KEY         # 是否配置、是否过期、是否拼写正确
SERPER_API_TIMEOUT     # 单次请求超时（默认 12s）
serper.dev 控制台       # 上游服务状态、API 健康检查页面
```

---

### 5. 搜索返回 429

通常表示：

- rate limit；
- quota；
- throttling。

检查 serper.dev 控制台的调用量和配额（serper.dev 免费版 2,500 次/月）。

---

### 6. 网页根地址抓不到，但首页可以

这是 `fetch_url` 的 root fallback 场景。

检查：

```text
FETCH_URL_ROOT_FALLBACK_ENABLED
FETCH_URL_ROOT_FALLBACK_PATHS
```

---

### 7. R2 不工作

至少确认：

```text
R2_ENDPOINT
R2_ACCESS_KEY
R2_SECRET_KEY
R2_BUCKET_NAME
```

如果模型需要公开 URL，还需要：

```text
R2_PUBLIC_URL
```

---

### 8. 为什么某些 MCP 工具看不到？

默认只暴露 read-only tools。

如果需要 mutation：

```bash
export APITELEGRAMCHAT_MCP_ENABLE_MUTATIONS=true
```

但 mutation 工具仍然应该由受信任 host 做用户确认。

---

## 开发说明

### 修改模型

主要修改：

```text
src/apitelegramchat/config.py
```

为模型声明正确的：

```text
provider
vision
audio
video
supports_tools
native_image
native_document
native_video
max_context
max_output_tokens
```

不要只修改显示名称而忽略模型实际能力，否则多模态输入或 tool calling 可能在运行时失败。

### 修改 Agent 工具

Telegram Runtime 工具主要位于：

```text
src/apitelegramchat/search_engine.py
src/apitelegramchat/tool_executors.py
```

如果希望 MCP 也暴露该能力，需要同时考虑：

```text
src/apitelegramchat/mcp/registry.py
```

并明确它属于：

```text
READ_ONLY_SPECS
```

还是：

```text
MUTATION_SPECS
```

不要因为 Runtime 有某个工具，就默认认为它适合 MCP。

### 修改 workspace 安全边界

优先修改：

```text
workspace_paths.py
workspace_utils.py
sandbox.py
```

不要在业务工具中自行拼接 workspace 路径。

统一路径入口可以避免：

- `../` 穿越；
- symlink escape；
- chat 间串读；
- staging 目录越界。

### 修改富消息

核心组件：

```text
src/apitelegramchat/ai/rich_message_builder.py
```

涉及草稿生命周期时，应优先保持：

```text
capacity warning
→ turn boundary
→ rollover
→ handoff
→ next draft
```

这一时序。

不要在任意 `flush()` 中直接创建后台新 draft，否则很容易重新引入：

- 工具组被拆分；
- 新模型请求先于旧段永久化；
- 旧 draft 迟到刷新；
- 消息内容丢失；
- Telegram 草稿位置跳动。

---

## 设计原则

这个项目最重要的几个工程原则是：

### 1. Fail closed

安全边界失败时宁可拒绝操作，而不是放宽限制。

### 2. Least privilege

尤其是 MCP：

```text
默认只读
高影响能力显式 opt-in
```

### 3. Explicit scope

workspace、state、memory 等私有数据必须有明确命名空间。

### 4. Tool correctness over tool count

工具不是越多越好。

工具必须：

- 有明确 schema；
- 有明确边界；
- 能返回可诊断错误；
- 不泄露内部路径/secret；
- 与 Agent context 的生命周期一致。

### 5. Turn boundary over arbitrary timing

对于 Agentic Loop，模型回合和工具批次是状态机边界。

尤其是 Telegram draft rollover，不应该仅根据某次 `flush()` 的时机做异步猜测。

### 6. UI 与执行状态分离

Telegram 富消息既是 UI，也是运行状态的可视化。

因此工具：

```text
started
running
done
error
```

和模型：

```text
streaming
turn finished
```

必须保持一致的生命周期。

---

## License

项目许可证见：

```text
LICENSE
```

## Anthropic（Claude 官方 API）支持

本项目新增了一个走 Anthropic 原生 Messages API 的厂商选项，与原有 7 个
OpenAI 兼容厂商（OpenRouter / ModelScope / Gemini / Grok / DeepSeek / GLM /
Agnes）并存，互不影响，可在对话中直接切换模型使用。

### 配置

设置环境变量：

```
ANTHROPIC_API_KEY=sk-ant-xxxxx
```

未设置该变量时，Claude 系列模型不可用，其余厂商行为不受任何影响。

### 已内置的模型

见 `config.py` 中 `SUPPORTED_MODELS` 的 "Anthropic 官方模型" 部分：

- `claude-sonnet-4-5-20250929`
- `claude-opus-4-1-20250805`
- `claude-haiku-4-5-20251001`

> 以上模型 ID 为占位值，请在正式使用前核实并按需替换为
> Anthropic 官方当前实际可用的模型字符串（见
> https://docs.claude.com/en/docs/about-claude/models ）。

### 实现说明

- `api_client.py`：`anthropic` 厂商使用原生 `AsyncAnthropic` 客户端，
  其余厂商仍使用 `AsyncOpenAI`，两套逻辑完全独立。
- `ai/anthropic_bridge.py`：新文件，负责
  - 工具 schema 转换（OpenAI `function` 格式 → Anthropic `input_schema`）
  - 消息格式转换（`role: system` → 顶层 `system` 参数；
    `role: tool` → `tool_result` 内容块）
  - 完整的原生流式 agentic 循环 `_agentic_loop_anthropic`
  - 供 `subagent_tool.py` 复用的非流式调用适配器
    `anthropic_chat_completions_create`
- 全局对话历史（跨模型共享）中持久化的消息格式**保持不变**（仍是原有的
  OpenAI 兼容形状）；协议转换只发生在"即将请求 Anthropic API 之前"和
  "刚收到 Anthropic 响应之后"这两个边界点，因此用户在同一对话中切换
  厂商（例如从 Claude 切回 OpenRouter）不会导致历史格式冲突。

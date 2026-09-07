# apitelegramchat

一个 MCP-native 的 Telegram AI 助手：多厂商模型接入、流式富消息草稿、
完整 Agent 工具面（Web / 地图 / 文件 / Bash 沙箱 / 多媒体生成）、
可观测的上下文管理，以及一个最小权限的 stdio MCP Server。

- Python >= 3.10，单进程异步架构（Quart + aiohttp）
- 模型经 OpenRouter / ModelScope / Gemini / Grok / DeepSeek / GLM / Agnes 接入
- 对 Anthropic Messages API 与 Gemini 原生协议提供专用桥接循环

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
- [Skills 与外部 MCP](#skills-与外部-mcp)
- [MCP Server](#mcp-server)
- [数据目录与持久化](#数据目录与持久化)
- [富消息与草稿滚动](#富消息与草稿滚动)
- [部署](#部署)
- [测试](#测试)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [开发说明](#开发说明)
- [设计原则](#设计原则)

---

## 项目定位

### Telegram Runtime

Telegram 是主要用户界面。收到消息后，Runtime 会：

1. 校验 Telegram 用户是否在白名单中；
2. 恢复该会话的历史、模型、角色和 workspace 状态；
3. 按消息类型处理文本、图片、文档、音频、视频和回复引用（相册自动聚合）；
4. 选择模型并建立模型上下文；
5. 调用 Agentic Loop，执行模型的 tool calls；
6. 将工具状态和模型输出持续渲染为 Telegram 富消息草稿；
7. 在达到容量阈值时，于**完整模型回合边界**滚动草稿；
8. 最终将结果保留为 Telegram 消息。

### 统一事件源调度（USER / TIMER）

所有模型回合都走同一个调度入口 `get_ai_response(..., event_source=...)`，
按事件源动态分配工具面与输出通道，共用同一份会话历史：

| | USER（用户发消息） | TIMER（系统后台唤醒，`proactive.py`） |
|---|---|---|
| 触发 | 用户任意消息/命令 | 用户空闲约 20min 后进入"活动时间"，此后随机 10/20/40min 唤醒一次 |
| user 消息 | 用户输入写入历史 | 合成唤醒消息只进请求上下文，**不写入历史** |
| 工具面 | `SEARCH_TOOLS`（不含 `send_message_to_user`） | 基础工具 + `send_message_to_user`（移除 `ask_user` / `present_files`） |
| 可见输出 | 富消息草稿流式 + 最终富文本推送 | **完全静默**：无草稿、无工具进度、最终文本不推送 |
| 打断 | 打断旧 USER 草稿 | 用户消息打断 TIMER 回合：取消后台任务 + **静默撤回**该回合已发消息 |

TIMER 回合里用户唯一可见的输出来自模型显式调用 `send_message_to_user`
（`send` 发送 / `edit` 编辑 / `delete` 撤回，普通纯文本像人随手发消息）。
回合的 assistant/tool 消息正常沉淀进历史；被用户打断时已发出的普通消息
会被静默撤回（`asyncio.shield` 保证发送完成注册后统一撤回，不留残留）。

### MCP Runtime

项目还提供 `apitelegramchat-mcp` —— 一个本地 stdio MCP Server。它与
Telegram Runtime 共用业务能力，但**不把 Runtime 能力原样暴露**，而是：

- 默认只暴露读取型工具；mutation 工具必须显式开启；
- 每个 MCP 进程必须持有 host 生成的唯一 `scope`，workspace / state 按 scope 隔离；
- 工具参数使用显式 JSON Schema，拒绝未声明字段。

适合作为 Claude Desktop、IDE Agent 等受信任本地 Agent Host 的后端能力提供者。

---

## 能力概览

| 能力 | 说明 |
|---|---|
| Telegram Bot | Webhook / getUpdates 双摄取通道，私聊 + 白名单控制 |
| 流式输出 | 模型输出持续更新 Telegram 富消息草稿 |
| 富消息 | 标题、段落、列表、表格、代码、引用、链接、图片、视频等 |
| 草稿滚动 | 在完整模型/工具回合边界安全切段，避免内容丢失 |
| 多模型 | OpenRouter、ModelScope、Gemini、Grok、DeepSeek、GLM、Agnes |
| Tool Calling | 并行工具调用、工具状态、结果压缩、错误恢复 |
| Web | 搜索（search/images/videos/lens 四合一）+ 深度抓取 + Wikipedia |
| 信息工具 | 天气、汇率、新闻、书籍、Crypto、二维码 |
| 地图 | 地理编码、POI、路线、距离（经 amap-maps MCP） |
| 文件 | Telegram 上传、workspace 编辑、文件发送 |
| Bash | 持久 Shell + Landlock + rlimit + fork bomb watchdog |
| 生成 | 图片生成、参考图编辑、视频生成 |
| Skills | 从 `.claude/skills` 等位置加载项目技能 |
| Subagent | 将复杂任务拆给独立 Agent Loop |
| 主动唤醒 | TIMER 事件源后台巡检 + 主动发普通消息（可编辑/撤回） |
| Memory / Todo | 持久化私有记忆和任务 |
| 对象存储 | S3/R2 兼容的文件持久化与公开资源 URL |
| MCP | stdio Server + 外部 Streamable HTTP MCP Client |

---

## 架构

整体分为四层：Telegram 摄取 → 回合调度 → Agent 循环 → 工具执行，
另有一个独立的受信 MCP Host 入口。

```text
                         ┌──────────────────────────┐
                         │        Telegram          │
                         │  message / media / reply │
                         └────────────┬─────────────┘
                                      │ webhook / getUpdates
                                      ▼
┌──────────────────────────────────────────────────────────────────┐
│                         Telegram Runtime                          │
│                                                                   │
│  app.py（装配层：Quart 路由 / 生命周期 / process_update 骨架）      │
│    ├── app_state.py         update 队列、后台任务句柄               │
│    ├── app_turns.py         回合任务管理 / 上下文守卫 / 消息 handler │
│    ├── app_media_groups.py  相册聚合（photo/video/document）        │
│    ├── app_commands.py      管理员命令 / 用户命令 / 按钮回调         │
│    ├── app_lists.py         role/model 列表 UI                     │
│    ├── state.py / config.py / proactive.py / turn_recovery.py     │
│    ▼                                                              │
│  ai_handlers.py / ai/agentic_loops.py（Agent 循环）                │
│    ├── ai/anthropic_bridge.py  Anthropic 原生桥接                  │
│    ├── ai/gemini_bridge.py     Gemini 原生桥接                     │
│    ├── ai/bridge_common.py     两桥共享的循环骨架                   │
│    ├── 上下文压缩 / 工具调用编排 / 草稿流 / subagent                │
│    ▼                                                              │
│  tool_dispatch.py（工具统一调度）                                   │
│    ├── search/ 包            Web / 信息 / 地图 / 生成 / 编辑器      │
│    ├── bash_session.py       持久 bash 沙箱                        │
│    ├── tool_result_format.py 结果 → UI 渲染分发                    │
│    └── todo / memory / subagent / message_user / skills            │
│    ▼                                                              │
│      Web      Maps      Files      Bash      Generation           │
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

底层公共设施位于 `core/` 包（自 `utils.py` 拆分）：日志与请求 ID、
全局 HTTP 会话、HTML 转义等文本工具、富消息媒体兜底清理、Telegram
消息发送/删除与草稿状态机、供应商余额查询、消息文本提取与语音转录。

### 为什么 Telegram Runtime 与 MCP 要分开？

Telegram Agent 拥有 Bash、文件写入、文件发送、多媒体生成、子 Agent、
memory/todo mutation 等高影响能力。它们适合在 Runtime 中结合用户确认、
会话状态和业务策略使用，但不适合无条件进入 MCP 工具列表。

因此 MCP 采用独立的 `ToolRegistry`：

- `READ_ONLY_SPECS`：默认暴露；
- `MUTATION_SPECS`：只有显式 opt-in 才暴露。

---

## 快速开始

### 1. 环境要求

- Linux（Bash 沙箱需要内核 5.13+ 支持 Landlock）
- Python >= 3.10；Node.js >= 22.13（文档/PDF Skill 依赖）
- Docker（生产部署推荐）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
npm install        # 文档/PDF Skill 所需 Node 依赖
```

### 2. 配置最小环境

```bash
export TELEGRAM_BOT_TOKEN="你的 Telegram Bot Token"
export WEBHOOK_TOKEN="随机且不可预测的 Webhook Token"
export WEBHOOK_URL="https://example.com/webhook"
export OPENROUTER_API_KEY="你的 OpenRouter API Key"
```

建议将所有 secret 放入部署平台的 Secret/Environment 管理，不要提交到 Git。

### 3. 启动

```bash
PYTHONPATH=src python -m quart --app app:app run --host 0.0.0.0 --port 5000
curl http://127.0.0.1:5000/health   # → {"status":"ok"}
```

### 4. Webhook 与摄取通道

摄取通道由 `INGEST_MODE` 决定（默认 `polling`，推荐）：

- `polling`：启动 `getUpdates` 长轮询。Render 等托管平台的边缘 WAF 会
  拦截含 shell 注入特征的 webhook 请求体并堵死投递队列；getUpdates 走
  出站响应体，从根上消除该类误杀。
- `webhook`：应用启动时自动进行"自愈注册"（幂等）：对齐 `WEBHOOK_URL`
  与 `WEBHOOK_TOKEN`、声明 `allowed_updates`、满足字符集时附带
  `secret_token` 注册（此后 Telegram 每次投递携带
  `X-Telegram-Bot-Api-Secret-Token` 头，与 query token 双路径鉴权）、
  拉取 `getWebhookInfo` 打观测日志。

管理员可在聊天里发送 `/webhookinfo` 随时查看积压数、最近投递错误与
注册 URL（token 已脱敏）。`DROP_PENDING_ON_STARTUP=true` 可在重启时
丢弃 Telegram 侧积压 update（默认保留并由健康实例重放排干）。

生产环境请确保：`WEBHOOK_URL` 为 HTTPS（端口 443/80/88/8443）、
`WEBHOOK_TOKEN` 足够随机、反向代理正确转发请求与 secret 头、
`/health` 可供平台健康检查、MCP stdio 端口不暴露公网。

---

## 配置

### 核心环境变量

| 变量 | 必需 | 用途 |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | 是 | Telegram Bot API |
| `WEBHOOK_URL` / `WEBHOOK_TOKEN` | 是 | Webhook 注册与鉴权 |
| `INGEST_MODE` | 可选 | `polling`（默认，getUpdates 长轮询）/ `webhook` |
| `DROP_PENDING_ON_STARTUP` | 可选 | `true` 时启动丢弃 Telegram 侧积压 update，默认 `false` |
| `OPENROUTER_API_KEY` | 是（严格模式） | 默认模型厂商/主要模型入口 |
| `MODELSCOPE_API_KEY` / `GEMINI_API_KEY` / `XAI_API_KEY` / `DEEPSEEK_API_KEY` / `GLM_API_KEY` / `AGNES_API_KEY` | 可选 | 各模型厂商 |
| `GROQ_API_KEY` | 可选 | 音频转写 |
| `SERPER_API_KEY` / `SERPER_API_TIMEOUT` | 可选 | Serper 搜索（默认 12s 超时） |
| `R2_ENDPOINT` / `R2_ACCESS_KEY` / `R2_SECRET_KEY` / `R2_BUCKET_NAME` / `R2_PUBLIC_URL` / `R2_REGION` | 可选 | S3/R2 对象存储 |
| `APITELEGRAMCHAT_DATA_DIR` | 可选 | 数据根目录 |
| `APITELEGRAMCHAT_WHITELIST_FILE` | 可选 | 白名单文件 |
| `APITELEGRAMCHAT_REQUIRE_STRICT_CONFIG=true` | 可选 | 启动时强校验 Telegram 四项核心配置 |
| `LOG_LEVEL` / `LOG_FILE` | 可选 | 日志级别（默认 INFO）与日志文件路径 |

#### 主动唤醒（TIMER 事件源）

| 变量 | 默认 | 用途 |
|---|---|---|
| `PROACTIVE_ENABLED` | `true` | 主动唤醒总开关 |
| `PROACTIVE_INTERVAL_MIN/MAX_SECONDS` | `300` / `1200` | 两次唤醒间隔的随机区间 |
| `PROACTIVE_MAX_IDLE_SECONDS` | `7200` | 用户连续多久没发消息 → 进入慢节奏 |
| `PROACTIVE_REST_SECONDS` | `3600` | 慢节奏暂停时长 |
| `TOOL_VISIBILITY_FILTER` | `true` | 按事件源折叠历史中 TIMER 专属工具的调用痕迹 |

仅**私聊**参与主动唤醒；bot 被屏蔽（sendMessage 403）时自动熔断该会话调度。

#### 上下文窗口与自动压缩

历史预算 = `min(CONTEXT_BUDGET_RATIO × max_context, max_context − max_output)`。
请求侧守卫（`context_manager.py`）与压缩事件（`app_turns.pre_flight_context_check`）
共用同一预算解析（`context_window.py`）：

| 变量 | 默认 | 用途 |
|---|---|---|
| `CONTEXT_MAX_TOKENS` | 不设置 | 历史预算绝对覆盖 |
| `CONTEXT_BUDGET_RATIO` | `0.8` | 历史预算占 max_context 比例 |
| `CONTEXT_COMPACT_TRIGGER_RATIO` | `0.90` | 触发压缩事件的高水位 |
| `CONTEXT_COMPACT_TARGET_RATIO` | `0.50` | 压缩要压回的目标水位（滞后区间） |
| `CONTEXT_PROTECTED_TURNS` | `6` | 永不淘汰的最近用户轮数 |

压缩事件两级杠杆：L1 无损归档较老工具负载（payload → workspace 指针，
模型可经 `text_editor` 取回）；L2 从最老用户轮开始整轮淘汰并合并进
历史头部滚动摘要。预算内时历史**逐字节透传**，保证 provider 端
prompt/KV 缓存全量命中。每轮日志输出 `prompt cache usage: {...}`
命中观测（兼容 OpenAI / Anthropic / DeepSeek 三种字段口径）。

#### 工具与附件缓存

| 缓存 | TTL / 容量 | 说明 |
|---|---|---|
| `fetch_url` 页面缓存 | `FETCH_CACHE_TTL`（默认 3600s），200 条 | 归一化 URL；只缓存成功结果 |
| `web_search` 结果缓存 | `SEARCH_CACHE_TTL`（默认 300s），200 条 | 按归一化参数缓存；错误不缓存 |
| 图片/音频/文档/视频字节 | `CACHE_TTL`（默认 300s） | 内存 TTLCache；R2 冷启动恢复 |
| R2 预签名 URL | 3300s | 复用同一 URL 保护前缀缓存 |

### OpenRouter 路由

```bash
export OPENROUTER_PROVIDER_SORT=price        # Provider 路由排序
export OPENROUTER_ALLOW_FALLBACKS=true       # 是否允许上游 fallback
export OPENROUTER_REQUIRE_PARAMETERS=false   # 是否要求 provider 满足请求参数
```

每个请求还携带会话亲和键 `session_id`（`tg-chat-{chat_id}-{纪元token}`），
粘性路由从第一次请求即生效；`/clear` 清空历史时轮换纪元 token，新建会话。

---

## 模型与厂商

模型配置集中在 `src/config.py`，"厂商能力"与"模型能力"分离。

| Provider | 环境变量 | 说明 |
|---|---|---|
| OpenRouter | `OPENROUTER_API_KEY` | 主要的 OpenAI-compatible 聚合入口 |
| ModelScope | `MODELSCOPE_API_KEY` | ModelScope inference |
| Gemini | `GEMINI_API_KEY` | Google Gemini（含原生协议桥接） |
| Grok | `XAI_API_KEY` | xAI |
| DeepSeek | `DEEPSEEK_API_KEY` | DeepSeek |
| GLM | `GLM_API_KEY` | 智谱 |
| Agnes | `AGNES_API_KEY` | Agnes（默认模型 `agnes-2.5-flash`） |

模型可声明能力位：`vision` / `audio` / `video` / `supports_tools` /
`native_image` / `native_document` / `native_video` / `search` /
`sampling` / `prompt cache` / `max_output_tokens` / `max_context`。
支持按 `provider/model-id` 自动发现部分未预先登记的模型。

Anthropic（Claude）模型经 OpenRouter 接入即可使用；Anthropic 原生 API
走 `ai/anthropic_bridge.py` 专用桥接（显式 `cache_control` 多断点策略、
流式零输出应用层重试、参数 JSON 自愈信封）。

---

## Telegram 使用方式

| 命令 | 作用 |
|---|---|
| `/model` | 打开模型选择列表（仅私聊）。点击按钮切换，列表就地打 √ 并定时清理 |
| `/role` | 选择系统角色（`china` / `think` / `neko_catgirl` / `succubus` / `isla`）。角色是系统提示词的一部分，不是模型本身；再次点击取消 |
| `/clear` | 清空当前会话历史与 active skill，轮换 LLM 会话亲和键，重置主动唤醒计时 |
| `/show` | 草稿预览开关：`on`（默认，实时草稿）/ `off`（静默模式：USER 回合收尾默认交付最后一条正文，TIMER 回合由模型 `deliver_reply` 自主交付）/ 不带参数查看状态 |
| `/balance [svc]` | 查询 DeepSeek / OpenRouter 余额 |
| `/adduser` `/deluser` `/listusers` | 管理员白名单管理 |
| `/webhookinfo` | 管理员查看投递链路状态 |

**回复消息**：直接回复已有消息即可引用其文本 / 图片 / 文档 / 音频 /
语音 / 视频 / Video Note，引用内容会作为当前 Agent 输入的一部分。

**相册**：图片、视频、文档相册自动聚合为一条消息处理（混合相册按类型
分流聚合）；文档相册在模型不支持原生文档时自动下载到 `download/`。

---

## Agent 工具

### Web / 信息

```text
web_search    # 4-in-1: search / images / videos / lens（mode 参数选择）
fetch_url     # 深度抓取：编码检测 / SSRF 防护 / JS·meta 重定向追踪 / 根路径回退
wikipedia / exchange_rate / book_lookup / weather / news / crypto_price / qr_code
```

### 地图

```text
geocode / route / distance / poi_keyword_search / poi_nearby_search / poi_details
```

坐标统一使用 `longitude,latitude`（如 `116.397128,39.916527`）。全部经
amap-maps MCP 委托，不保留本地高德 API 实现。

### Workspace / 文件

```text
text_editor      # view / str_replace / create / insert / list，行尾保真 + R2 持久化
bash             # 持久沙箱会话
present_files    # 发送 workspace 文件到聊天
```

### 生成

```text
generate_image_from_text / edit_image_with_reference / generate_video
```

### 工具返回的「模型视图」精简

工具原始返回同时服务两个消费者：Telegram 工具卡片 UI（要完整信息）与
LLM 上下文（只要高价值字段）。`tool_result_condense.py` 统一承载这层
筛选：weather 按小时数裁剪并删除月相/露点等低频字段、地图输出在源头
清洗（去掉 polyline 坐标串/分段路况/空值）、subagent 去除任务回声。
**双视图**设计保证 UI 展示不变，模型只收精简视图；错误文本一律原样
透传，不影响失败判定与熔断。

此外 Runtime 还内置 Memory / Todo / Subagent / message_user（提问 /
通知双用途）/ deliver_reply（静默交付）/ Skills / 上下文压缩 / 工具
结果摘要。这些能力并不全部作为 MCP 工具暴露。

---

## Workspace 与文件

每个聊天会话拥有独立 workspace：

```text
<data-root>/
├── workspaces/
│   └── <chat-or-scope>/
│       ├── upload/      # 准备发送给用户的文件
│       ├── download/    # Telegram 上传但尚未进入 workspace 的文件
│       ├── runtime/
│       └── ...
└── state/
```

路径边界由 `workspace_paths.py` 统一管理；workspace 根目录是 bash 的
固定起始目录，也是所有相对路径的唯一解析根。典型流程：

```text
Telegram 上传 → download/ → Agent/Bash 编辑 → upload/ → present_files → Telegram
```

Bash **禁止把 upload/download 作为工作目录执行命令**，避免待发送文件
或产物 staging 区被运行时文件污染。

---

## Bash 沙箱

Bash 不是普通的 `subprocess.run(...)`，而是一个持久的、受限制的 Bash
Session（`src/bash_session.py` + `src/sandbox.py`）。子进程启动前会：

1. 设置 `PR_SET_NO_NEW_PRIVS`；
2. 安装 Landlock，限制文件系统访问；
3. 设置 CPU / 文件大小 / 打开文件数等 `rlimit`；
4. 使用独立 process group，启动 fork bomb watchdog；
5. 不继承应用层 secret 环境变量。

Landlock 原则：`workspace → 可读写`；`/usr /bin ... → 只读 + 执行`；
其他应用私有目录 → 默认拒绝。Bash 可以使用镜像内的 Python/gcc/cmake
等工具，但不能读取 Bot Token、API Key、其他用户 workspace。

默认资源限制：

```bash
SANDBOX_MAX_PROCS=50
SANDBOX_MAX_CPU_SEC=300
SANDBOX_MAX_FILE_SIZE=104857600
SANDBOX_MAX_OPEN_FILES=256
SANDBOX_TIMEOUT_SEC=300
SANDBOX_OUTPUT_MAX_CHARS=80000
```

`SANDBOX_OUTPUT_MAX_CHARS` 超限时**保留开头与结尾、只省略中间**（编译
错误、traceback 几乎总在末尾），返回给模型的最终 token 预算由
`TOOL_RESPONSE_TOKEN_BUDGET`（默认 20000，bash 走头尾保留策略）兜底。

沙箱只限制文件系统，**不拦截出站网络**；`curl`/`wget`/`git`/`jq`/`zip`
已进入 Dockerfile，存量镜像会自动获得纯 Python 兜底 shim。项目刻意
不依赖 bubblewrap：目标环境可能禁止 unprivileged user namespace，而
Landlock 不需要特权容器即可工作。运行环境不支持 Landlock 时按
fail-closed 拒绝启动受保护 Bash 子进程（见 FAQ）。

---

## Skills 与外部 MCP

**Skills**：从 `.claude/skills` 等位置发现并加载（`src/skills.py`）。
Skill 包含 `SKILL.md` / `scripts/` / `assets/` / `references/`，runtime
负责扫描、解析 frontmatter、建立 catalog、按需读取与资产同步。Skill
不是普通 Python import，更接近 Agent 按需加载的"操作手册 + 工具资源"。

**外部 MCP**：使用 Streamable HTTP MCP Client 调用外部服务（当前仅保留
高德地图 `amap-maps`，见 `src/mcp_client.py`）。

---

## MCP Server

安装项目后启动：

```bash
export APITELEGRAMCHAT_MCP_SCOPE="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
export APITELEGRAMCHAT_DATA_DIR="/var/lib/apitelegramchat"
apitelegramchat-mcp
```

没有合法 `APITELEGRAMCHAT_MCP_SCOPE` 时 Server 拒绝启动——否则多个 MCP
session 会共享同一个 workspace/memory/todo/state，造成数据串扰。
scope 由受信任 host 生成，是 host/session 的隔离标识，不是模型上下文参数。

默认只开放读取型工具（搜索四件套 / 信息工具 / 地图 6 工具 /
`workspace.view`）；Bash、文件写入、生成、memory/todo mutation 等
mutation 能力必须显式 opt-in（`MUTATION_SPECS`）。不要因为 Runtime 有
某个工具，就默认认为它适合 MCP。

---

## 数据目录与持久化

生产环境建议：

```bash
export APITELEGRAMCHAT_DATA_DIR=/var/lib/apitelegramchat
```

目录权限按 `0700` 方向创建。不要把 `/app`、`/home/app` 直接作为
workspace，也不要把 Bot Token、API Keys、Docker secret、MCP token
放进 workspace。

R2 / S3 用于长期保存与公开资源 URL：

```bash
export R2_ENDPOINT="https://<account>.r2.cloudflarestorage.com"
export R2_ACCESS_KEY="..."
export R2_SECRET_KEY="..."
export R2_BUCKET_NAME="..."
export R2_PUBLIC_URL="https://cdn.example.com"
export R2_REGION="auto"
```

Bash sandbox 不直接拥有这些 credentials；预签名 URL 在过期前 5 分钟内
记忆化复用，保护 LLM 前缀缓存。

---

## 富消息与草稿滚动

Telegram 富消息不是简单的"不断 edit 一条消息"，而是一个完整的 Draft
Lifecycle（`ai/rich_message_builder.py` + `core/telegram_messaging.py`
的草稿状态机）。核心规则：

```text
接近容量 → 只标记 rollover_pending
        → 完成当前模型返回
        → 等待该返回中的全部并行工具完成
        → 关闭工具组并刷新最终状态
        → 永久化旧段 → 创建新 draft_id → 发送新草稿首帧
        → 再开始下一次模型请求
```

> 一个完整回合 = 一次 assistant 模型返回 + 该返回产生的全部并行工具
> 调用均进入终态。只有到这里，才允许 rollover。

过早滚动会把同批工具活动拆散到不同草稿；在任意 `flush()` 时机做异步
猜测则会引入旧 draft 迟到刷新、内容丢失、草稿位置跳动等回归。用户
消息打断进行中的回合时，已完成的 assistant/tool 消息会被补齐占位
tool_result 后沉淀进持久历史（打断保全，`turn_recovery.py`），
新回合从断点继续。

---

## 部署

### Docker

```bash
docker build -t apitelegramchat .
docker run --rm --env-file .env -p 5000:5000 apitelegramchat
```

镜像包含 Python 3 / Node.js 22 / gcc / cmake / LibreOffice / Pandoc /
ImageMagick / Tesseract / Poppler / qpdf 等 Skill 依赖，以非 root 用户
（UID/GID 2000）运行，默认 `PORT=5000`、
`APITELEGRAMCHAT_DATA_DIR=/tmp/apitelegramchat_data`。

### Render

仓库提供 `render.yaml` Blueprint：推送到 GitHub/GitLab → 在 Render 创建
Blueprint → 填写 Secret（`TELEGRAM_BOT_TOKEN`、`WEBHOOK_TOKEN`、各厂商
API Key、R2 密钥、`SERPER_API_KEY`、`GAODE_MCP_TOKEN` 等）→ 等待构建 →
检查 `/health` 与 Webhook。不要把 token 写进 `render.yaml` 明文。

---

## 测试

```bash
python -m pytest tests/ -v                 # 全部测试
python -m pytest tests/unit -v             # 仅单元测试
python -m pytest tests/integration -v      # 仅集成测试
python tests/test_whitelist_r2.py          # 白名单 R2 同步回归（可独立运行）
```

单元测试覆盖 Markdown→Rich HTML 转换、token 预算截断、上下文窗口核心
（轮块划分/淘汰规划/滚动摘要）、工具返回精简、搜索域名黑名单；集成
测试覆盖真实 Quart 应用的 `/health` 与 `/webhook` 鉴权路径
（`tests/conftest.py` 自动把 `src/` 加入 `sys.path`）。

---

## 项目结构

```text
.
├── src/
│   ├── app.py                        # Quart 装配层：路由 / 生命周期 / process_update 骨架
│   ├── app_state.py                  # update 队列与后台任务句柄
│   ├── app_turns.py                  # 回合任务管理 / 上下文守卫 / 6 类消息 handler / TIMER 唤醒
│   ├── app_media_groups.py           # 相册聚合（图片/视频/文档三族统一调度）
│   ├── app_commands.py               # 管理员命令 / 用户命令 / 按钮回调
│   ├── app_lists.py                  # role/model 列表 UI
│   ├── core/                         # 底层公共设施（自 utils.py 拆分）
│   │   ├── logging_setup.py          # 日志初始化 + 请求 ID 上下文
│   │   ├── http_session.py           # 全局 aiohttp 会话单例
│   │   ├── chat_guard.py             # chat 不可达熔断（403 类）
│   │   ├── text_utils.py             # escape_html / retry_async 等
│   │   ├── rich_media.py             # 富消息媒体兜底清理子系统
│   │   ├── balances.py               # 供应商余额查询
│   │   ├── telegram_messaging.py     # 消息删除 / 草稿状态机 / 富消息发送
│   │   └── message_extract.py        # 消息/贴纸文本提取 + Groq 转录
│   ├── search/                       # 工具能力包（自 search_engine.py 拆分）
│   │   ├── caches.py                 # web_search / fetch 双 TTL 缓存
│   │   ├── tool_schemas.py           # SEARCH_TOOLS 等工具 schema 数据底座
│   │   ├── serper.py                 # Serper 搜索客户端（4 mode）
│   │   ├── fetch_url.py              # 网页抓取 / SSRF / 重定向追踪
│   │   ├── quick_lookup.py           # wikipedia / weather / news / crypto / qr 等
│   │   ├── media_tools.py            # 图像/视频生成
│   │   ├── map_tools.py              # 高德地图 MCP 委托
│   │   └── text_editor.py            # 文本编辑器 + R2 持久化
│   ├── tool_dispatch.py              # 工具统一调度（dispatch_tool_call）
│   ├── tool_ui_render.py             # 工具结果卡片 UI 渲染工具箱
│   ├── tool_result_format.py         # format_tool_result（结果 → UI 分发）
│   ├── bash_session.py               # 持久 bash 沙箱会话
│   ├── file_delivery.py              # present_files 文件发送
│   ├── ai/
│   │   ├── agentic_loops.py          # Agent loop 入口（含各循环 re-export）
│   │   ├── bridge_common.py          # anthropic/gemini 桥接共享循环骨架
│   │   ├── anthropic_bridge.py       # Anthropic 原生桥接
│   │   ├── gemini_bridge.py          # Gemini 原生桥接
│   │   ├── tool_call_loop.py         # 工具调用编排
│   │   ├── rich_message_builder.py   # 富消息草稿构建器
│   │   ├── gemini_cache.py           # Gemini 显式缓存管理
│   │   ├── media_generation.py / attachment_content.py / tool_summary.py / ...
│   ├── utils.py                      # 兼容 facade（实现已拆至 core/）
│   ├── search_engine.py              # 兼容 facade（实现已拆至 search/）
│   ├── tool_executors.py             # 兼容 facade（实现已拆至调度/渲染/沙箱等）
│   ├── config.py / state.py / context_window.py / context_manager.py
│   ├── sandbox.py / workspace_paths.py / workspace_utils.py / s3_utils.py
│   ├── mcpserver/                    # stdio MCP Server（registry/context/server）
│   ├── mcp_client.py                 # 外部 Streamable HTTP MCP client
│   └── entrypoints/mcp_server.py     # console entrypoint
├── tests/                            # unit / integration / whitelist 回归
├── .claude/skills/                   # bundled Skills
├── Dockerfile / render.yaml / pyproject.toml / requirements.txt
└── README.md
```

> `utils.py`、`search_engine.py`、`tool_executors.py` 保留为薄 facade，
> 显式 re-export 全部既有符号：旧代码 `from search_engine import
> SEARCH_TOOLS` 等导入零改动；新代码请直接 import 对应子模块。

---

## 常见问题

### 1. Bot 能启动，但用户收到"未授权访问"

检查白名单文件（`APITELEGRAMCHAT_WHITELIST_FILE`，相对路径挂在
`APITELEGRAMCHAT_DATA_DIR` 下）。白名单同时支持 username 和 user ID。

### 2. MCP Server 启动失败，提示 scope

```bash
export APITELEGRAMCHAT_MCP_SCOPE="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
```

scope 是 host/session 的隔离标识，不要让 MCP Client 把它当模型上下文参数。

### 3. Bash 在本机无法运行

`uname -r` 确认内核 ≥ 5.13（Landlock）。不支持时项目拒绝启动受保护
Bash 子进程——这是有意的 fail-closed 行为，不是缺陷。

### 4. 搜索返回 502 / 超时 / 429

502/超时通常是 serper.dev 上游或网络暂时不可用，不要解释成 quota 用尽；
429 是限流/配额。检查 `SERPER_API_KEY`、`SERPER_API_TIMEOUT` 与
serper.dev 控制台（免费版 2,500 次/月）。

### 5. 网页根地址抓不到，但首页可以

深度链接失败时项目会自动回退站点根 URL 重试（`fetch_url_fallback.py`），
部分站点的反爬策略仍可能拦截；重试或换 UA 由上游决定。

### 6. R2 不工作

核对 `R2_ENDPOINT`（含 account id）、密钥对、bucket 名与
`R2_PUBLIC_URL`；R2 未配置时系统回退本地存储，已配置但对象不存在时
白名单会在启动时把本地数据播种上 R2。

### 7. 为什么某些 MCP 工具看不到？

MCP 默认只暴露 `READ_ONLY_SPECS`；mutation 工具需要显式 opt-in，且
scope 未设置时 Server 直接拒绝启动。

---

## 开发说明

### 修改模型

编辑 `src/config.py`，为模型声明完整能力位（provider / vision / audio /
video / supports_tools / native_image / native_document / native_video /
max_context / max_output_tokens）。不要只改显示名称而忽略实际能力，
否则多模态输入或 tool calling 会在运行时失败。

### 修改 Agent 工具

工具实现按域位于 `src/search/` 各子模块与 `src/tool_dispatch.py`，
schema 在 `src/search/tool_schemas.py`（`SEARCH_TOOLS` 是模型可见工具
面的唯一数据源）。工具的 UI 渲染在 `tool_ui_render.py` /
`tool_result_format.py`。若希望 MCP 暴露该能力，需同步评估
`src/mcpserver/registry.py` 的 `READ_ONLY_SPECS` / `MUTATION_SPECS` 归类。

### 修改底层发送/日志/HTTP

编辑 `src/core/` 对应子模块。`utils.py` 只是 facade——新增公共函数时
请加在 core 子模块里，并在 facade 中 re-export 保持旧导入路径可用。

### 修改 bridge 循环骨架

Anthropic / Gemini 两条原生循环的共享骨架（初始化、assistant 消息
组装、草稿流切换、超限总结、终局收束）集中在 `ai/bridge_common.py`，
厂商差异只保留在各自 bridge 的请求构造与流消费钩子里。修骨架 bug
改一处即两桥同时生效；新增厂商桥接时实现对应钩子即可复用整个骨架。

### 修改 workspace 安全边界

优先修改 `workspace_paths.py` / `workspace_utils.py` / `sandbox.py`，
不要在业务工具中自行拼接 workspace 路径——统一路径入口才能避免
`../` 穿越、symlink escape、chat 间串读与 staging 目录越界。

### 修改富消息

核心组件 `src/ai/rich_message_builder.py`。涉及草稿生命周期时保持
`capacity warning → turn boundary → rollover → handoff → next draft`
时序，不要在任意 `flush()` 中直接创建新 draft（会重新引入工具组被拆、
旧段未永久化、草稿位置跳动等回归）。

---

## 设计原则

1. **Fail closed**：安全边界失败时宁可拒绝操作，而不是放宽限制。
2. **Least privilege**：MCP 默认只读，高影响能力显式 opt-in。
3. **Explicit scope**：workspace、state、memory 等私有数据必须有明确命名空间。
4. **Tool correctness over tool count**：工具必须有明确 schema、明确边界、
   可诊断错误、不泄露内部路径/secret，并与 Agent context 生命周期一致。
5. **Turn boundary over arbitrary timing**：模型回合和工具批次是状态机
   边界；draft rollover 不根据某次 `flush()` 的时机做异步猜测。
6. **UI 与执行状态分离**：Telegram 富消息既是 UI 也是运行状态可视化，
   工具 `started/running/done/error` 与模型 `streaming/turn finished`
   必须保持一致的生命周期。

---

## License

见 [LICENSE](LICENSE)。

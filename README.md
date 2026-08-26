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
| `APITELEGRAMCHAT_DATA_DIR` | 可选 | 数据根目录 |
| `APITELEGRAMCHAT_WHITELIST_FILE` | 可选 | 白名单文件 |
| `LOG_LEVEL` | 可选 | 日志级别，默认 `INFO` |

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
web_search
fetch_url
wikipedia
exchange_rate
book_lookup
weather
news
crypto_price
ip_geo
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

### Workspace / 文件

```text
text_editor
bash
fetch_download
stage_upload
list_download
list_upload
present_files
```

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
- Ask User；
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
│       ├── files/
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

它们是 staging 区，而不是普通工作目录。

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
download/
    ↓
fetch_download
    ↓
workspace files/
    ↓
Agent/Bash 编辑
    ↓
stage_upload
    ↓
upload/
    ↓
present_files
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
```

可根据部署环境调整。

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

项目使用 Streamable HTTP MCP Client 调用外部服务。

目前主要用于：

### 搜索

Serper MCP：

```bash
export SERPER_MCP_URL="https://mcp.api-inference.modelscope.net/<deployment-id>/mcp"
export SERPER_MCP_TOKEN="..."
```

项目只允许访问受信任的 MCP 主机，并限制上游工具。

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

### MCP 错误诊断

搜索 MCP 会区分：

| 状态 | 含义 |
|---|---|
| 401 / 403 | token、URL 或授权配置错误 |
| 404 | endpoint 不存在/失效 |
| 429 / quota / rate limit | 上游限流或额度限制 |
| 502 / 503 / 504 | 上游临时不可用 |
| 其他 4xx | 参数或工具配置被拒绝 |

特别注意：

> `502/503/504` 不能证明额度用完。

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
search.ip_geo

geo.geocode
geo.route
geo.distance
geo.poi_keyword_search
geo.poi_nearby_search
geo.poi_details

workspace.list_download
workspace.list_upload
workspace.view
```

### Mutation 工具

以下能力默认关闭：

```text
memory.manage
todo.manage
workspace.edit
workspace.fetch_download
workspace.stage_upload
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
SERPER_MCP_TOKEN
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

---

## 项目结构

```text
.
├── src/
│   └── apitelegramchat/
│       ├── app.py                    # Quart Webhook / Telegram Runtime
│       ├── config.py                 # Provider / Model / runtime config
│       ├── state.py                  # 会话状态
│       ├── context_manager.py        # 上下文选择
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
│       ├── ask_user_tool.py          # 向用户请求确认/输入
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

### 4. 搜索返回 502

502 通常意味着上游 gateway/service 暂时不可用。

不要直接把它解释成 quota 用尽。

检查：

```text
SERPER_MCP_URL
SERPER_MCP_TOKEN
ModelScope MCP deployment 状态
```

---

### 5. 搜索返回 429

通常表示：

- rate limit；
- quota；
- throttling。

检查上游 MCP 部署的调用量和配额。

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

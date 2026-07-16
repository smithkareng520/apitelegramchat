# 🤖 Telegram AI Assistant

[![Python](https://img.shields.io/badge/Python-%3E=3.10-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-purple)](https://core.telegram.org/bots)

一个高性能的 **异步 Webhook 架构** Telegram AI 机器人，采用 Quart 框架构建。支持多款顶级 AI 模型灵活切换、全面的多模态输入处理（文本/图像/音频/文档）、云存储集成（Cloudflare R2）以及智能的 AI 驱动搜索能力。

---

## 🚀 核心功能

### 🧠 多模型支持
- **内置模型引擎**：Gemini、Grok、DeepSeek、OpenRouter（支持 GPT、GLM、Claude 等）
- **一键切换**：通过 `/model` 命令快速切换模型，对话历史自动保留
- **余额查询**：实时查看 DeepSeek 和 OpenRouter 的账户余额

### 🎯 全能多模态处理
- **文本对话**：自然流畅的 AI 交互
- **视觉识别**：支持批量图片分析（最高 10 张）、智能 OCR 识别
- **文档解析**：一站式处理 PDF、Word、Excel、PowerPoint、TXT 等主流格式
- **音频处理**：
  - 支持音频模型的直接处理（Gemini Audio）
  - 对于不支持音频的模型，通过 Groq API 智能转录为文本（上限 15MB / 30 分钟）
- **引用分析**：可直接分析引用消息中的媒体内容

### 🔍 AI 智能搜索
- **免费搜索回退**：优先使用 Google CSE，失败时自动切换到免费的 DuckDuckGo JSON API（无需 key，通过环境变量 `DDG_SEARCH_API_URL` 配置服务地址）
- **智能判断**：AI 自主决定何时联网获取实时信息，避免不必要的搜索调用
- **可溯源**：搜索结果附带源链接，支持信息验证

### 🎭 角色扮演
- **多种预设人格**：中国（外交官风格）、思考者（深度分析）、猫娘、魅魔、Isla 等
- **灵活切换**：无缝切换人格，影响对话风格和回复方式

### 🔐 权限管理
- **用户白名单**：支持按用户名或 ID 管理访问权限
- **管理员命令**：
  - `/adduser @username` 或 `/adduser 123456789` - 添加用户到白名单
  - `/deluser @username` 或 `/deluser 123456789` - 从白名单移除用户
  - `/listusers` - 查看当前白名单

### 💾 对话历史管理
- **自动优化**：历史记录上限为 **60 条消息** 或 **120 万字符**，超出自动修剪
- **自动清空**：通过 `/clear` 命令手动清除对话历史
- **上下文保留**：模型切换时自动保留对话上下文

### 📋 待办清单（Task / Todo 工具）
- **持久化存储**：每个 chat 一份清单，落在 `./workspace/{chat_id}/todos.json`，并随 Cloudflare R2 自动同步——会话结束后任务依旧保留。
- **8 种操作**：`add` / `list` / `done` / `undone` / `toggle` / `delete` / `clear` / `edit`，由 AI 自主决策调用。
- **富文本卡片**：列表以富 HTML 卡片渲染——优先级徽章（🔴 高 / 🟡 中 / 🟢 低）、已完成项自动加删除线、底部统计区显示「总数 · 已完成 · 待办」。
- **可点击 InlineKeyboard**：当条目不超过 12 项时，列表卡片附带可点击按钮：
  - `✓ #N` 一键标记完成 / 恢复
  - `✕ #N` 一键删除
  - `🧹 清空已完成` / `📋 全部` / `⏳ 未完成` / `✅ 已完成` 切换视图
- **灵活引用**：调用 done/delete/edit 时 `todo_id` 既支持 8 位短 id，也支持显示序号 `#3`，AI 与用户都能直接引用。
- **过滤与标签**：list 支持 `filter=all/pending/done`、按 `tag` 与 `priority` 过滤，便于聚焦查看。
- **AI 调用约定**：执行任何写操作后，AI 会自动再调用一次 `list`，让用户在结果卡片里立刻看到最新状态。

### 🧠 长期记忆（Memory 工具）
- **跨会话保留**：与对话历史（自动修剪 60 条）不同，memory 永久存储在 `./workspace/{chat_id}/memories.json`，并随 R2 同步——下次对话开始时仍可检索。
- **7 种操作**：`add` / `get` / `list` / `search` / `update` / `delete` / `clear`。
- **结构化字段**：每条记忆有 `category`（fact / preference / person / event / note / 自定义）、`tags`、`importance`（low/medium/high）。
- **零依赖检索**：`search` 用大小写不敏感的子串匹配扫 content + tags + category，无需外部向量数据库。
- **富文本卡片**：列表以重要性降序排列，每条带优先级徽章、分类标签、内容预览；详情页显示创建/更新时间与来源。
- **AI 调用约定**：用户主动表达"记住…"、提到长期偏好（口味/过敏）、介绍重要他人、提到截止日期时，AI 会写入 memory；回答涉及用户偏好的问题前，AI 会先 `search` 看相关记忆。

### 🎯 技能注册表（Skill 工具）
- **7 个内置技能**：`translator`（中英互译）、`summarizer`（长文摘要）、`coder`（工程化代码生成）、`reviewer`（代码评审）、`explainer`（概念解释）、`brainstormer`（头脑风暴）、`planner`（任务拆解）。
- **6 种操作**：`list` / `info` / `use` / `register` / `update` / `delete`。
- **激活语义**：调用 `use` 后，技能的 `system_prompt` 会指导 AI 接下来的回复风格与格式，直到用户切换或取消。
- **自定义技能**：用户可以描述"我希望你以后用 X 方式回复"，AI 帮他 `register` 一个 custom skill（name 用 snake_case，必填 description + system_prompt）。自定义技能存放在 `./workspace/{chat_id}/skills.json`，跨会话保留。
- **表格化列表**：所有可用技能以 `<table bordered striped>` 渲染，列 = 技能 / 类型（内置/自定义）/ 说明 / 可用工具。
- **保护机制**：内置技能不可删除/更新；自定义技能可自由 update / delete。

### 🤖 子 Agent（Subagent 工具）
- **干净上下文**：派生一个子 agent 处理独立子任务，子 agent 不继承主对话历史，只看到 `task` + 可选 `context`，避免主上下文污染。
- **最小化 agentic loop**：复用主 agent 的 `api_client` + `SUPPORTED_MODELS`，自带 8 轮上限循环，可并发执行工具调用。
- **工具白名单受控**：默认安全白名单（web_search / fetch_url / wikipedia / weather / bash / text_editor / todo 等）；调用方可进一步收窄；`[]` 表示禁止任何工具。
- **安全护栏**：子 agent 不能递归调用 `subagent` / `memory` / `skill`（防爆炸）；最大 8 轮 / 90 秒（可调到 300 秒）。
- **典型用法**：多步研究（"派子 agent 调研 X 的最新进展，返回要点"）、独立子问题（"把这段长文本翻译成英文"）、并行任务（一次回复多次调用 subagent）。
- **返回结构**：JSON 含 `answer`（子 agent 最终答复）、`rounds`、`tool_calls`、`elapsed`，AI 整合后写入最终回复；UI 渲染成「🤖 子 agent 已完成」卡片。

---

## 🛠 技术架构

### 核心框架
- **Web 框架**：Quart（基于 asyncio 的高性能异步框架）
- **架构模式**：Webhook 模式（相比轮询更高效）
- **并发处理**：`asyncio` + 任务管理器，支持并发处理多个用户请求

### 文件结构
```
apitelegramchat/
├── app.py                  # Webhook 服务器、消息路由与分发
├── ai_handlers.py          # AI 推理核心、模型调用、Agentic loop
├── search_engine.py        # 搜索工具集（Google CSE + 免费 DuckDuckGo JSON API 回退）
├── file_handlers.py        # 文件解析引擎（PDF、文档、图像处理）
├── s3_utils.py            # Cloudflare R2 云存储集成
├── todo_tool.py           # 📋 待办清单工具：存储 + 富文本渲染 + InlineKeyboard
├── memory_tool.py         # 🧠 长期记忆工具：跨会话事实/偏好/人物存储
├── skill_tool.py          # 🎯 技能注册表：内置 7 技能 + 自定义技能
├── subagent_tool.py       # 🤖 子 agent 工具：派生独立子任务的最小 agentic loop
├── config.py              # 环境配置、模型定义、API Key 管理
├── state.py               # 运行时状态管理、用户上下文、锁机制
├── utils.py               # 工具函数集（日志、消息发送、API 调用）
├── requirements.txt       # Python 依赖清单
└── Dockerfile            # 容器化部署配置
```

### 关键特性
- **Agentic 工作流**：通过工具调用实现 AI 自主决策和动作执行
- **异步媒体处理**：高效的批量图片和文档处理
- **并发任务管理**：防止用户重复请求，支持请求取消
- **云存储集成**：通过 Cloudflare R2 持久化存储用户媒体
- **分布式锁机制**：确保对话状态的线程安全

---

## ⚙️ 快速开始

### 前置要求
- Python 3.10+
- 从 [BotFather](https://t.me/BotFather) 获取 Telegram Bot Token
- 至少一个 AI 模型 API Key（Gemini / Grok / DeepSeek / OpenRouter 等）

### 1️⃣ 克隆与安装

```bash
git clone https://github.com/smithkareng520/apitelegramchat.git
cd apitelegramchat
pip install -r requirements.txt
```

### 2️⃣ 环境配置

创建 `.env` 文件，配置所需的 API Keys 和服务端点：

```env
# 必需：Telegram Bot
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
WEBHOOK_URL=https://your-domain.com/webhook
WEBHOOK_TOKEN=your_webhook_secret_token

# 必需：至少一个 AI 模型 API Key
GEMINI_API_KEY=
DEEPSEEK_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
XAI_API_KEY=
MODELSCOPE_API_KEY=

# 可选：图像和文件存储
R2_ENDPOINT=
R2_ACCESS_KEY=
R2_SECRET_KEY=
R2_BUCKET_NAME=
R2_REGION=auto
IMGBB_KEY=

# 可选：其他服务
GEOAPIFY_KEY=
TOMTOM_API_KEY=

# 可选：DuckDuckGo 免费搜索回退 API（不配置则使用默认值）
DDG_SEARCH_API_URL=https://my-search-api-08cb.onrender.com/duckduckgo/search

# 应用配置
LOG_LEVEL=INFO
LOG_TRUNCATE_LIMIT=500
SANDBOX_UNSHARE_NET=1    (启用断网)
```

### 3️⃣ 启动服务

```bash
# 本地开发运行
python app.py

# 或使用 Gunicorn（生产环境推荐）
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

### 4️⃣ 注册 Webhook

```bash
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=${WEBHOOK_URL}&secret_token=${WEBHOOK_TOKEN}&drop_pending_updates=true"
```

---

## 📖 使用指南

### 命令列表

| 命令 | 功能描述 |
|------|---------|
| `/start` | 显示欢迎信息和功能介绍 |
| `/model` | 🤖 切换 AI 模型（仅私聊） |
| `/role` | 🎭 选择角色扮演人设 |
| `/clear` | 🧹 清空对话历史 |
| `/balance` | 💰 查询 AI 服务账户余额 |
| `/adduser` | 🔐 添加用户到白名单（管理员） |
| `/deluser` | ✂️ 从白名单移除用户（管理员） |
| `/listusers` | 📋 查看当前白名单（管理员） |

### 交互示例

#### 文本对话
直接发送消息与 AI 交互：
```
你：请解释量子计算的基本原理
Bot：[提供详细的技术解释]
```

#### 图片分析
1. 发送图片（支持批量上传）
2. 可选：添加提示词（作为 caption）
3. Bot 自动分析并返回结果

#### 文档处理
1. 上传 PDF、Word 文档等
2. 可选：添加分析要求
3. Bot 自动解析并总结内容

#### 音频转录与分析
1. 发送音频/语音文件
2. Bot 自动转录或直接分析
3. 返回转录文本或分析结果

#### 引用分析
1. 回复一条包含媒体的消息
2. 可选：添加问题描述
3. Bot 分析引用内容并作答

#### 待办清单
直接用自然语言让 Bot 管理任务即可，无需记命令：
```
你：帮我记三件事——买菜、写周报、给妈妈打电话，周报优先级高
Bot：[调用 todo add 三次，再 list 一次，返回富文本卡片，附 InlineKeyboard 按钮]

你：把第 2 项标完成
Bot：[调用 todo done #2，再 list，返回更新后的卡片]

你：清掉已完成的
Bot：[调用 todo clear filter=done，再 list，返回更新后的卡片]
```
也可以直接点工具结果卡片上的 `✓` / `✕` 按钮一键操作，无需打字。

#### 长期记忆
让 Bot 记住跨会话的事实与偏好：
```
你：我对花生过敏，记住一下
Bot：[调用 memory add，category=fact, importance=high, tags=[健康,过敏]]

（几天后）
你：今天吃啥
Bot：[先 memory search "过敏"，看到花生过敏，避开花生相关菜品再回答]
```

#### 技能激活
让 Bot 进入某种专业模式：
```
你：帮我翻译这段话
Bot：[调用 skill use translator，按该技能的 system_prompt 调整风格后翻译]

你：以后我用「老板」开头时，你就用商务汇报的语气给我回
Bot：[调用 skill register name=boss_mode description=... system_prompt=...]

你：激活 boss_mode
Bot：[调用 skill use boss_mode]
```

#### 子 Agent
让 Bot 派一个独立子任务给子 agent：
```
你：帮我研究一下目前最热门的 3 个开源 LLM 项目，每个给出 stars / license / 一句话特色，最后给个推荐
Bot：[调用 subagent task="调研..." allowed_tools=["web_search","fetch_url"]
     子 agent 自己跑几轮 web_search + fetch_url，返回要点
     Bot 整合子 agent 的答复，给出最终推荐]
```

---

## 🌐 部署方案

### 云平台部署（推荐）

#### Render
1. Fork 本仓库到你的 GitHub
2. 在 [Render](https://render.com) 连接 GitHub 仓库
3. 配置环境变量，部署 Web Service
4. 自动 HTTPS 和持久化运行

#### Railway
1. 连接 GitHub 仓库
2. 自动检测 Python 项目
3. 配置 `.env` 环境变量
4. 一键部署

#### Heroku
```bash
heroku create your-app-name
git push heroku main
heroku config:set TELEGRAM_BOT_TOKEN=your_token
```

### VPS 部署

```bash
# 安装依赖
apt update && apt install -y python3.10 python3-pip

# 克隆并配置
git clone https://github.com/smithkareng520/apitelegramchat.git
cd apitelegramchat
pip install -r requirements.txt

# 配置 systemd 服务
sudo nano /etc/systemd/system/telegram-bot.service
```

### 监控与可用性

使用 UptimeRobot 监控 `/health` 端点（每 5 分钟检测一次）：
```
https://your-domain.com/health?token=${WEBHOOK_TOKEN}
```

---

## ⚙️ 配置详解

### 模型配置

每个模型支持以下属性：
- `name`：显示名称
- `vision`：是否支持图像分析
- `audio`：是否支持音频输入
- `document`：是否支持文档分析
- `api_key`：对应的 API Key 环境变量名

### 搜索引擎配置

- **Google CSE**（可选）：通过 `GOOGLE_CSE_KEY` / `GOOGLE_CSE_ID` 启用，作为主搜索路径
- **DuckDuckGo JSON API**（免费回退）：通过环境变量 `DDG_SEARCH_API_URL` 配置服务地址，默认指向 `https://my-search-api-08cb.onrender.com/duckduckgo/search`。调用方式：`<DDG_SEARCH_API_URL>?text=<URL-encoded query>`，返回 JSON 格式结果（含 `title` / `url` / `snippet`）。如自部署同类服务，将此变量改成自己的地址即可。

```env
# 可选：DuckDuckGo 免费搜索回退 API（不配置则使用默认值）
DDG_SEARCH_API_URL=https://my-search-api-08cb.onrender.com/duckduckgo/search
```

### 云存储配置（Cloudflare R2）

```env
R2_ENDPOINT=https://your-account-id.r2.cloudflarestorage.com
R2_ACCESS_KEY=your_access_key
R2_SECRET_KEY=your_secret_key
R2_BUCKET_NAME=your_bucket
R2_REGION=auto
```

---

## 📊 性能指标

- **并发处理**：支持 100+ 并发用户
- **响应延迟**：平均 < 5 秒（取决于模型和网络）
- **内存占用**：基础 ~100MB，峰值 ~500MB
- **存储占用**：对话历史 + 媒体缓存，可配置

---

## 🚨 常见问题

### Q1：音频转录显示 GROQ_API_KEY 未配置
**A**：如果你的模型不支持音频输入，需配置 GROQ_API_KEY 用于转录。如果支持，则不需要。

### Q2：图片无法加载
**A**：确保 R2 存储桶已正确配置且 `R2_PUBLIC_URL` 可访问。

### Q3：Webhook 连接失败
**A**：检查 `WEBHOOK_URL` 是否可从公网访问，HTTPS 证书是否有效，`WEBHOOK_TOKEN` 是否与设置一致。

### Q4：模型响应缓慢
**A**：可能原因：
- API 调用超时 → 增加超时时间
- 模型队列繁忙 → 尝试切换模型
- 网络延迟 → 检查网络连接

---

## 📝 许可证

本项目采用 [MIT License](LICENSE) 开源，欢迎贡献和使用。

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

*维护者：[smithkareng520](https://github.com/smithkareng520)*  
*最后更新：2026年6月*


---

## 🧩 MCP 支持

项目新增了标准的 MCP 暴露层，现有工具可以直接被 MCP 客户端发现和调用，而原有 Telegram 机器人功能保持不变。

### 启动方式

```bash
APP_MODE=mcp python mcp_server.py
```

默认使用 stdio 传输，适合 Claude Desktop、MCP Inspector 以及其他支持 MCP 的本地客户端。

### 工具分区

- `web_search`、`fetch_url`、`weather`、`news` 等工具是无状态的，可直接调用。
- `todo`、`memory`、`skill`、`subagent`、`bash`、`text_editor`、`present_files` 是有状态的，建议在参数里传入 `workspace_id`，这样可以隔离不同客户端或任务空间。
- 不传 `workspace_id` 时，服务器会自动使用默认工作区。

### 资源与提示词

- `project://tool-catalog`：返回完整工具目录的 JSON
- `project-brief`：返回一段适合放进系统提示词的项目说明


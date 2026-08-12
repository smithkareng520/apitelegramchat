# apitelegramchat

`apitelegramchat` 是一个 Telegram 助手，同时提供一个**本地 stdio MCP server**。MCP 层使用官方 Python SDK 的 legacy-compatible server，负责 JSON-RPC 生命周期、请求校验和 JSON Schema 校验；业务工具、资源访问和外部 MCP 调用均位于独立适配层。

## 本地运行

```bash
python -m venv .venv
.venv/bin/pip install -e .
export TELEGRAM_BOT_TOKEN=...
export WEBHOOK_TOKEN=...
export WEBHOOK_URL=https://example.com/webhook
export OPENROUTER_API_KEY=...
python -m quart --app apitelegramchat.app:app run --host 0.0.0.0 --port 5000
```

## MCP server

MCP server 只能由受信任的本地 host 作为子进程启动。每个 server 进程都**必须**设置一个由 host 生成的、不传入模型上下文的唯一 scope。scope 是私有状态和工作区的命名空间，不是客户端参数，也不会作为资源返回。

```bash
export APITELEGRAMCHAT_MCP_SCOPE='opaque-host-session-7bf52d8a5bda4f09'
export APITELEGRAMCHAT_DATA_DIR='/var/lib/apitelegramchat'
apitelegramchat-mcp
```

缺少或不符合格式的 `APITELEGRAMCHAT_MCP_SCOPE` 会使 server 拒绝启动，避免默认共享 workspace。运行时目录、workspace 和 state 目录均以 `0700` 创建。请将 `APITELEGRAMCHAT_DATA_DIR` 设为应用用户私有、持久化的目录。

### 默认工具面

默认只暴露读取型工具，例如 web、天气、汇率、地图查询、workspace 文件查看和私有暂存区列表。以下能力默认**不在 MCP 工具列表中**：

| 能力 | 默认策略 | 启用方式 |
| --- | --- | --- |
| 通用 shell | 永不由 MCP 暴露 | 无；Telegram runtime 的 shell 与 MCP server 分离。 |
| 文件写入、文件发送、memory/todo 变更 | 默认关闭 | 仅在受信任 host 已提供用户确认时设置 `APITELEGRAMCHAT_MCP_ENABLE_MUTATIONS=true`。 |
| 子代理、图片/视频生成等可产生连锁调用或费用的能力 | MCP 不暴露 | 通过 Telegram runtime 中的独立确认与配额策略使用。 |

即使开启 mutation 工具，host 仍应在任何删除、覆盖、上传或外部写操作前向用户展示参数并取得确认。MCP server 对所有工具声明显式 JSON Schema，拒绝未声明字段和不符合类型/范围的输入。

### 文件系统边界

workspace 编辑器只接收相对路径，并在解析符号链接后再次确认路径位于 workspace 内；目录枚举不会跟随子项符号链接。不要把 host 密钥、部署配置或应用源目录放进 workspace。项目还提供了测试覆盖该边界。

## 外部 MCP（地图与搜索）

地图和可选的 Bing 搜索通过 Streamable HTTP 调用外部 MCP server。外部 endpoint 必须使用 HTTPS、主机 allowlist 和固定的上游工具 allowlist；认证令牌不会发往未受信任的 URL。

```bash
# AMap / ModelScope
export GAODE_MCP_ENABLED=true
export GAODE_MCP_URL='https://mcp.api-inference.modelscope.net/<deployment-id>/mcp'
export GAODE_MCP_TOKEN='...'
# 仅在覆盖默认 ModelScope 主机时才需要指定。
export GAODE_MCP_ALLOWED_HOSTS='mcp.api-inference.modelscope.net'

# ModelScope Serper MCP：google_search / scrape 直接暴露给模型，由模型自行选择工具和参数。
# 常规部署只需提供 token；如需改用自定义 URL，才须同时覆盖 allowlist。
export SERPER_MCP_ENABLED=true
export SERPER_MCP_TOKEN='...'
# Serper google_search 还要求 gl（地区）和 hl（语言）。
# 默认：gl=cn, hl=zh；需要英文/美国结果时可改为 us/en。
export SERPER_MCP_REGION='cn'
export SERPER_MCP_LANGUAGE='zh'

# 可选：仅在使用自定义 endpoint 时设置。
# export SERPER_MCP_URL='https://mcp.example.com/mcp'
# export SERPER_MCP_ALLOWED_HOSTS='mcp.example.com'
```

地图坐标统一为 `longitude,latitude`，例如 `116.397128,39.916527`。

## 测试

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

测试覆盖：强制 scope、私有目录权限、默认最小权限工具表、编辑器符号链接拒绝、资源不泄露绝对 workspace 路径、外部 endpoint allowlist 以及 SDK 请求处理器注册。

## Docker

```bash
docker build -t apitelegramchat .
docker run --env-file .env -p 5000:5000 apitelegramchat
```

容器启动 MCP 子进程时仍须为每个受信任会话单独传入 `APITELEGRAMCHAT_MCP_SCOPE`。不要把 stdio MCP 直接暴露到网络；如需远程接入，应通过单独的认证网关、TLS、会话隔离、限流和审计层实现。

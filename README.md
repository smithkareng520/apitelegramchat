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

地图和可选的 Serper 搜索通过 Streamable HTTP 调用外部 MCP server。外部 endpoint 必须使用 HTTPS、主机 allowlist 和固定的上游工具 allowlist；认证令牌不会发往未受信任的 URL。`web_search` 会把 Serper 的 `organic` 结果转换回既有的成功数统计、标题、摘要和链接格式，因此 Telegram 侧的来源链接展示保持不变。

```bash
# AMap / ModelScope
export GAODE_MCP_ENABLED=true
export GAODE_MCP_URL='https://mcp.api-inference.modelscope.net/<deployment-id>/mcp'
export GAODE_MCP_TOKEN='...'
# 仅在覆盖默认 ModelScope 主机时才需要指定。
export GAODE_MCP_ALLOWED_HOSTS='mcp.api-inference.modelscope.net'

# Serper 搜索 MCP：URL 和令牌均由部署环境显式提供。
# 项目只允许调用 ModelScope MCP 域名下的 google_search 工具。
export SERPER_MCP_URL='https://mcp.api-inference.modelscope.net/<deployment-id>/mcp'
export SERPER_MCP_TOKEN='...'
```

### 网页搜索域名黑名单

`web_search` 会在展示 Serper 返回结果前，按照 `src/apitelegramchat/web_search_settings.py` 过滤不可抓取或不希望使用的网站。部署者只需编辑该文件中的 `BLACKLIST_DOMAINS`；**每一条规则都独立定义自己的匹配范围**，无需也不提供全局模式开关。

| 写入的规则 | 匹配范围 | 示例 |
|---|---|---|
| `example.com` | 仅精确主机名。 | 只过滤 `example.com`，不影响 `www.example.com`。 |
| `[*.]example.com` | 根域名及全部子域名。 | 过滤 `example.com`、`www.example.com`、`a.b.example.com`。 |
| `*.example.com` | 仅子域名，不含根域名。 | 过滤 `www.example.com`、`a.b.example.com`，不影响 `example.com`。 |

该文件还集中提供 `WEB_SEARCH_DOMAIN_FILTER_ENABLED`（启停本地最终过滤）、`WEB_SEARCH_UPSTREAM_DOMAIN_EXCLUDE_ENABLED`（启停上游预筛选）、`WEB_SEARCH_DEFAULT_RESULTS`、`WEB_SEARCH_MAX_RESULTS`、`WEB_SEARCH_CANDIDATE_MULTIPLIER`、`WEB_SEARCH_MAX_CANDIDATES`、`WEB_SEARCH_REGION` 与 `WEB_SEARCH_LANGUAGE`。当连接的是 [marcopesani/mcp-server-serper](https://github.com/marcopesani/mcp-server-serper) 时，只有 `[*.]example.com` 这类“根域名加全部子域名”规则会安全转换为 `exclude` 参数中的 `site:<域名>`，从而生成 Google 的 `-site:<域名>` 查询条件；精确规则和仅子域名规则不会发送可能扩大范围的 `-site:` 条件。本地 URL 主机名过滤始终在返回前执行，因此是最终保证。若改用不支持 `exclude` 参数的搜索 MCP，请将 `WEB_SEARCH_UPSTREAM_DOMAIN_EXCLUDE_ENABLED` 设为 `False`。修改后重启应用即可生效。不要填写协议、端口、路径、查询参数或其他通配符。

### MCP 搜索失败诊断

`web_search` 会保留外部 MCP 响应中的 HTTP 状态码，记录经过脱敏和长度限制的上游错误摘要，并将失败明确返回给调用方；它不再把服务故障误显示为“未找到结果”。

| 可见状态 | 含义 | 应采取的动作 |
|---|---|---|
| `HTTP 429` 或错误文本含 `quota`、`rate limit`、`throttled` | 上游限流或调用额度限制。此类请求不会在短时间内自动重试，避免额外消耗调用次数。 | 在 ModelScope MCP 部署的用量、调用日志或配额页面确认限制，稍后再试。 |
| `HTTP 502`、`503`、`504` | 上游网关或服务临时不可用，**不能据此确认额度已用完**；项目会保留短时自动重试。 | 稍后重试，并检查 MCP 部署状态和调用日志。 |
| `HTTP 401`、`403` | 访问令牌、部署地址或授权配置有误。 | 核对 `SERPER_MCP_URL`、`SERPER_MCP_TOKEN` 及部署授权。 |
| 其他 `4xx` | 请求参数或上游工具配置被拒绝。 | 查看日志中的 `status`、`category` 和脱敏 `detail` 字段。 |

### 根路径首页回退

部分网站的根路径（例如 `https://www.battleofballs.com/`）不能被静态抓取器可靠读取，但同一站点的 `https://www.battleofballs.com/index/` 可正常读取。为处理这一情况，`fetch_url` 在根路径的常规请求和正文提取均失败后，可按 `src/apitelegramchat/web_search_settings.py` 中的 `FETCH_URL_ROOT_FALLBACK_PATHS` 依次尝试同站点首页路径。默认启用的路径为 `/index/`。

回退仅对不带查询参数或片段的 HTTP(S) 根路径生效，并保持原 URL 的协议、主机和端口；深层路径、带参数链接、片段链接和跨站地址不会被改写。若不需要该行为，可将 `FETCH_URL_ROOT_FALLBACK_ENABLED` 设为 `False`。每个候选项必须是以 `/` 开头的纯站内路径，例如 `/index/` 或 `/home/`。

地图坐标统一为 `longitude,latitude`，例如 `116.397128,39.916527`。

## 测试

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

测试覆盖：强制 scope、私有目录权限、默认最小权限工具表、编辑器符号链接拒绝、资源不泄露绝对 workspace 路径、外部 endpoint allowlist、SDK 请求处理器注册，以及网页搜索的黑名单域名匹配与过滤。

## Docker

```bash
docker build -t apitelegramchat .
docker run --env-file .env -p 5000:5000 apitelegramchat
```

容器启动 MCP 子进程时仍须为每个受信任会话单独传入 `APITELEGRAMCHAT_MCP_SCOPE`。不要把 stdio MCP 直接暴露到网络；如需远程接入，应通过单独的认证网关、TLS、会话隔离、限流和审计层实现。

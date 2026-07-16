# apitelegramchat

一个 Telegram AI Assistant + MCP 项目。

## 安装

```bash
pip install .
```

## 运行 MCP

```bash
python -m apitelegramchat.entrypoints.mcp_server
```

Telegram Webhook 模式可直接使用 `apitelegramchat.app:app` 交给 Quart / Gunicorn。

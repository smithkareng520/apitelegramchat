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

## 运行 Webhook 服务

本项目的 Web 入口是 `app:app`，但项目根目录已经提供了兼容包装，
部署时也可以直接使用 `app:app` 或 `apitelegramchat.app:app`。

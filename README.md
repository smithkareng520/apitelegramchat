# apitelegramchat

Telegram AI assistant with web, workspace, memory, todo, map, and MCP tool support.

## Run locally

```bash
python -m venv .venv
.venv/bin/pip install -e .
export TELEGRAM_BOT_TOKEN=...
export WEBHOOK_TOKEN=...
export WEBHOOK_URL=https://example.com/webhook
export OPENROUTER_API_KEY=...
python -m quart --app apitelegramchat.app:app run --host 0.0.0.0 --port 5000
```

## Docker

```bash
docker build -t apitelegramchat .
docker run --env-file .env -p 5000:5000 apitelegramchat
```

## MCP server

```bash
apitelegramchat-mcp
```

## Optional map integration

Set `AMAP_KEY` to enable Gaode map services. Without it, the standard geographic providers remain available.

## Security verification

Run this in the deployed Linux container after enabling the shell tool:

```bash
python -m apitelegramchat.verify_security
```

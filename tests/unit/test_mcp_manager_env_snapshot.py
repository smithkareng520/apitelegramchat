"""Regression tests for MCP env resolution after config.py scrubs os.environ."""

import json
import asyncio


def test_mcp_json_env_resolution_uses_runtime_snapshot(tmp_path, monkeypatch):
    import mcp_manager

    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "gaode_mcp": {
                        "type": "streamable_http",
                        "url_env": "GAODE_MCP_URL",
                        "headers_env": {"Authorization": "GAODE_MCP_TOKEN"},
                    },
                    "internal_search": {
                        "type": "stdio",
                        "command": "python3",
                        "args": ["-m", "mcpserver.server", "--module", "search"],
                        "env": {"SERPER_API_KEY": "${SERPER_API_KEY}"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    # 模拟 config.py 已经 scrub os.environ，但启动快照仍保留 secret。
    monkeypatch.setattr(
        mcp_manager,
        "RUNTIME_ENV",
        {
            "GAODE_MCP_URL": "https://example.com/mcp",
            "GAODE_MCP_TOKEN": "gaode-secret",
            "SERPER_API_KEY": "serper-secret",
        },
    )
    monkeypatch.delenv("GAODE_MCP_URL", raising=False)
    monkeypatch.delenv("GAODE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    servers = mcp_manager.load_servers(str(config_path))

    assert servers["gaode_mcp"].url == "https://example.com/mcp"
    assert servers["gaode_mcp"].headers["Authorization"] == "Bearer gaode-secret"
    assert servers["internal_search"].env["SERPER_API_KEY"] == "serper-secret"


def test_stdio_params_start_from_runtime_snapshot(monkeypatch):
    import mcp_manager

    monkeypatch.setattr(
        mcp_manager,
        "RUNTIME_ENV",
        {"SAFE_RUNTIME_VALUE": "from-startup", "SERPER_API_KEY": "serper-secret"},
    )
    server = mcp_manager.MCPServerConfig(
        name="internal_search",
        type="stdio",
        command="python3",
        args=(),
        env={"SERPER_API_KEY": "serper-secret"},
    )

    class FakeStdioParams:
        def __init__(self, command, args, env):
            self.command = command
            self.args = args
            self.env = env

    monkeypatch.setattr(mcp_manager, "StdioServerParameters", FakeStdioParams)
    params = mcp_manager.MCPManager._stdio_params(
        mcp_manager.mcp_manager, server, "test-scope"
    )

    assert params.env["SERPER_API_KEY"] == "serper-secret"
    assert params.env["SAFE_RUNTIME_VALUE"] == "from-startup"
    assert params.env["APITELEGRAMCHAT_MCP_SCOPE"] == "test-scope"


def test_list_server_tools_deduplicates_concurrent_refresh(monkeypatch):
    import mcp_manager

    manager = mcp_manager.MCPManager.__new__(mcp_manager.MCPManager)
    manager.servers = {
        "remote": mcp_manager.MCPServerConfig(
            name="remote",
            type="streamable_http",
            url="https://example.com/mcp",
        )
    }
    manager._stdio = {}
    manager._inprocess_registry = {}
    manager._reaper_task = None
    manager._tools_cache = {}
    manager._tools_refresh_locks = {}
    manager._tools_cache_ttl = 300.0
    manager._closed = False
    monkeypatch.setattr(mcp_manager, "_MCP_SDK_AVAILABLE", True)

    class Tool:
        name = "ping"
        description = "ping"
        inputSchema = {"type": "object", "properties": {}}
        meta = None

    calls = {"count": 0}

    async def fake_list_raw_tools(_server):
        calls["count"] += 1
        await asyncio.sleep(0)
        return [Tool()]

    monkeypatch.setattr(manager, "_list_raw_tools", fake_list_raw_tools)

    async def run():
        return await asyncio.gather(
            manager.list_server_tools("remote"),
            manager.list_server_tools("remote"),
        )

    results = asyncio.run(run())

    assert calls["count"] == 1
    assert results[0] == results[1]
    assert results[0][0]["function"]["name"] == "mcp__remote__ping"

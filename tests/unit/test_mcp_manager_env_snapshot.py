"""Regression tests for MCP env resolution after config.py scrubs os.environ."""

import json


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

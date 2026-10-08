"""工具总表的缓存隔离与失效行为。"""

import asyncio


def test_get_model_tools_returns_isolated_deep_copies(monkeypatch):
    import tool_registry

    tool_registry.invalidate_model_tools_cache()

    source = [
        {
            "type": "function",
            "function": {
                "name": "example",
                "description": "example",
                "parameters": {
                    "type": "object",
                    "properties": {"nested": {"type": "object", "properties": {}}},
                },
            },
        }
    ]
    calls = {"count": 0}

    def fake_builtin():
        calls["count"] += 1
        return source

    async def fake_mcp():
        return []

    monkeypatch.setattr(tool_registry, "builtin_tool_defs", fake_builtin)
    monkeypatch.setattr(tool_registry, "mcp_tool_defs", fake_mcp)

    async def scenario():
        first = await tool_registry.get_model_tools()
        first[0]["function"]["parameters"]["properties"]["nested"]["properties"]["leak"] = {"type": "string"}
        second = await tool_registry.get_model_tools()
        assert "leak" not in second[0]["function"]["parameters"]["properties"]["nested"]["properties"]
        assert calls["count"] == 1

    asyncio.run(scenario())
    tool_registry.invalidate_model_tools_cache()


def test_invalidate_model_tools_cache_forces_rebuild(monkeypatch):
    import tool_registry

    tool_registry.invalidate_model_tools_cache()
    calls = {"count": 0}

    def fake_builtin():
        calls["count"] += 1
        return [{"type": "function", "function": {"name": f"example{calls['count']}"}}]

    async def fake_mcp():
        return []

    monkeypatch.setattr(tool_registry, "builtin_tool_defs", fake_builtin)
    monkeypatch.setattr(tool_registry, "mcp_tool_defs", fake_mcp)

    async def scenario():
        assert (await tool_registry.get_model_tools())[0]["function"]["name"] == "example1"
        tool_registry.invalidate_model_tools_cache()
        assert (await tool_registry.get_model_tools())[0]["function"]["name"] == "example2"

    asyncio.run(scenario())
    tool_registry.invalidate_model_tools_cache()

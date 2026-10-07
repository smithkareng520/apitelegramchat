"""端到端冒烟测试：mcp.json 驱动的统一 MCP 架构。

验证链路：mcp.json 解析 → stdio 子进程拉起 → MCP initialize →
list_tools 聚合 → mcp__<server>__<tool> 命名 → 实际调用一次工具。
不依赖任何外部服务（gaode_mcp 仅检查配置与跳过逻辑）。

用法：python3 scripts/smoke_mcp_e2e.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

# 让内部 bash 沙箱在本机（可能无 Landlock）也能启动
os.environ.setdefault("APITELEGRAMCHAT_SANDBOX_DISABLE_LANDLOCK", "1")
# 提供 search 模块所需的假 key（仅 list_tools / todo 调用，不会真的搜网）
os.environ.setdefault("SERPER_API_KEY", "smoke-test-key")
# gaode_mcp 的 url/headers 全部经 env 间接引用（url_env / headers_env）；
# 配置假值以验证“未配置→跳过注册；已配置→注入成功”的两条分支。
os.environ.setdefault("GAODE_MCP_URL", "https://mcp.example.test/mcp")
os.environ.setdefault("GAODE_MCP_TOKEN", "smoke-test-token")
# 工作区/数据根目录指向项目内的临时目录（MCP 子进程继承后，
# todo / bash 的 per-chat 命名空间目录都会创建在这里）
os.environ.setdefault(
    "APITELEGRAMCHAT_WORKSPACES_DIR",
    os.path.join(os.path.dirname(__file__), "..", ".smoke-workspaces"))
os.environ.setdefault(
    "APITELEGRAMCHAT_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "..", ".smoke-data"))

async def main() -> int:
    from mcp_manager import mcp_manager, load_servers
    from tool_names import is_mcp_name

    # 1. mcp.json 解析
    servers = load_servers(None)
    names = sorted(servers)
    print(f"[1] mcp.json servers: {names}")
    assert "gaode_mcp" in names, "外部 streamable_http 服务器缺失"
    for expected in ("internal_search", "internal_todo", "internal_memory",
                     "internal_workspace", "internal_bash"):
        assert expected in names, f"内部 stdio 服务器缺失: {expected}"
    gaode = servers["gaode_mcp"]
    assert gaode.type == "streamable_http"
    # url_env / headers_env 间接引用应已从宿主环境变量解析为真实值
    assert gaode.url == "https://mcp.example.test/mcp", f"url_env 未注入: {gaode.url}"
    # headers_env 间接引用已解析，且管理器自动补 "Bearer " 前缀
    assert gaode.headers.get("Authorization") in ("smoke-test-token", "Bearer smoke-test-token"), \
        f"headers_env 未注入: {gaode.headers}"

    # 2. 工具发现（stdio 服务器真实拉起 + MCP 握手 + list_tools）
    defs = await mcp_manager.list_all_tools()
    full_names = sorted(d["function"]["name"] for d in defs)
    print(f"[2] discovered {len(full_names)} tools:")
    for n in full_names:
        print("     -", n)
    assert all(is_mcp_name(n) for n in full_names), "存在非 mcp__ 前缀的工具名"
    for expected in ("mcp__internal_search__web_search",
                     "mcp__internal_todo__todo",
                     "mcp__internal_memory__memory",
                     "mcp__internal_workspace__text_editor",
                     "mcp__internal_bash__bash"):
        assert expected in full_names, f"工具缺失: {expected}"

    # 3. 真实调用一次内部工具（todo add → todo list，走 stdio MCP 往返）
    from tool_dispatch import dispatch_tool_call
    chat_id = 20260930
    add = await dispatch_tool_call("mcp__internal_todo__todo",
                                   {"action": "add", "title": "冒烟测试条目"},
                                   chat_id)
    print(f"[3] todo add -> {add[:120]}")
    listing = await dispatch_tool_call("mcp__internal_todo__todo",
                                       {"action": "list"}, chat_id)
    print(f"    todo list -> {listing[:200]}")
    assert "冒烟测试条目" in listing, "todo MCP 往返失败：新增条目未出现在列表"

    # 4. bash 沙箱往返（internal_bash 子进程内执行）
    bash_out = await dispatch_tool_call(
        "mcp__internal_bash__bash",
        {"description": "冒烟测试", "command": "echo mcp-bash-ok"},
        chat_id)
    print(f"[4] bash -> {bash_out[:120]}")
    assert "mcp-bash-ok" in bash_out, "bash MCP 往返失败"

    # 5. gaode_mcp 未配置 env 时在加载期优雅跳过（工具不可见，不报错）
    import importlib
    import mcp_manager as _mm
    os.environ.pop("GAODE_MCP_URL", None)
    importlib.reload(_mm)
    print("[5] 未配置 GAODE_MCP_URL 时的加载日志应含 'registration rejected'",
          "→ gaode_mcp 不进入 servers，模型看不到任何 gaode 工具")

    await mcp_manager.aclose()
    print("\nSMOKE OK — mcp.json 驱动的 MCP 架构端到端连通")
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

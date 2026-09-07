"""针对 4 项 UI / Schema 修复的回归测试。

覆盖：
1. web_search / message_user 及其他直接铺开返回的工具，结果统一用
   ``<pre><code>`` 等宽代码面板展示（与 bash / text_editor 同规范）；
2. ``_description`` 不再被 normalize_tool_schema 强制注入 required，
   且只有 bash 声明该字段；web_search 不带 _description 可通过校验；
3. memory / todo 工具组完成态摘要显示 Memory / Todo（含复数与名词
   大写豁免），不再退化为默认的 "Ran an action"；
4. 工具组完成态摘要统计失败条目，末尾追加 ``(failed n)``；全部失败
   时只显示 ``(failed n)``，不再是笼统的 "Tools failed"。
"""
import asyncio

import pytest

from ai.rich_message_builder import RichMessageBuilder
from ai.schema_validation import normalize_and_validate
from ai.web_search_render import format_web_search_result
from tool_assembly import normalize_tool_schema, valid_tool_defs
from tool_result_format import format_tool_result


# =========================================================================
# 问题 1：工具返回统一 <pre><code> 面板
# =========================================================================

def test_web_search_result_wrapped_in_code_panels():
    envelope = (
        "🔍 [成功: Serper / Google] 搜索「np」的结果（1/1）：\n"
        "1. 标题：某标题\n"
        "   摘要：某摘要\n"
        "   链接：https://example.com/a\n"
    )
    summary, details = format_web_search_result({"query": "np"}, envelope)
    assert summary == "np 1 result"
    assert "<pre><code>" in details and "</code></pre>" in details
    assert ">Input</b>" in details and "query: np" in details
    assert ">Output</b>" in details
    assert "标题：某标题" in details


def test_web_search_error_wrapped_in_code_panel():
    summary, details = format_web_search_result(
        {"query": "球球大作战"}, "❌ 搜索失败：配额不足\n第二行"
    )
    assert summary == "Search failed"
    assert "<pre><code>" in details
    assert "搜索失败" in details


def test_message_user_result_uses_code_panels():
    result = '{"type":"custom","value":"我在"}'
    summary, details = asyncio.run(
        format_tool_result("message_user", {"question": "在吗？"}, result)
    )
    assert "<pre><code>" in details and "</code></pre>" in details
    assert "在吗？" in details          # Input 面板展示发出的提问
    assert '"type":"custom"' in details  # Output 面板展示原始返回


@pytest.mark.parametrize("fn_name,args", [
    ("exchange_rate", {"base": "USD"}),
    ("book_lookup", {"query": "三体"}),
    ("news", {"source": "bbc"}),
    ("crypto_price", {"coin": "btc"}),
])
def test_info_tools_output_wrapped_in_code_panel(fn_name, args):
    summary, details = asyncio.run(
        format_tool_result(fn_name, args, "正文 <含> 标签 & 实体")
    )
    assert "<pre><code>" in details
    # 严格转义：原始尖括号不得原样出现
    assert "正文 &lt;含&gt; 标签 &amp; 实体" in details


def test_unknown_tool_output_wrapped_in_code_panel():
    summary, details = asyncio.run(format_tool_result("whatever", {}, "raw <text>"))
    assert "<pre><code>" in details
    assert "raw &lt;text&gt;" in details


# =========================================================================
# 问题 2：_description 仅 bash 声明且必填
# =========================================================================

def test_normalize_tool_schema_does_not_inject_required_description():
    tool = {
        "type": "function",
        "function": {
            "name": "web_search",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": [],
            },
        },
    }
    norm = normalize_tool_schema(tool)
    assert norm["function"]["parameters"]["required"] == []
    # 字段仍排在 properties 首位（展示顺序规范化保留）
    assert list(norm["function"]["parameters"]["properties"])[0] == "_description"
    # 深拷贝：原对象不被修改
    assert tool["function"]["parameters"]["required"] == []


def test_search_tool_schemas_description_only_on_bash():
    tools = valid_tool_defs(
        [t for t in __import__("search.tool_schemas", fromlist=["SEARCH_TOOLS"]).SEARCH_TOOLS
         if isinstance(t, dict)]
    )
    by_name = {t["function"]["name"]: t for t in tools}
    assert "web_search" in by_name
    ws_props = by_name["web_search"]["function"]["parameters"]["properties"]
    assert "_description" not in ws_props
    assert "_description" not in (by_name["web_search"]["function"]["parameters"].get("required") or [])

    bash_params = by_name["bash"]["function"]["parameters"]
    assert "_description" in bash_params["properties"]
    assert bash_params["required"] == ["_description", "command"]

    for name in ("weather", "exchange_rate", "book_lookup", "crypto_price",
                 "geocode", "route", "distance", "poi_keyword_search",
                 "poi_nearby_search", "poi_details"):
        assert "_description" not in by_name[name]["function"]["parameters"]["properties"], name


def test_memory_todo_schemas_have_no_description():
    from memory_tool import MEMORY_TOOL
    from todo_tool import TODO_TOOL
    assert "_description" not in MEMORY_TOOL["function"]["parameters"]["properties"]
    assert "_description" not in TODO_TOOL["function"]["parameters"]["properties"]


def test_web_search_without_description_passes_validation():
    tools = valid_tool_defs(
        [t for t in __import__("search.tool_schemas", fromlist=["SEARCH_TOOLS"]).SEARCH_TOOLS
         if isinstance(t, dict)]
    )
    args, err = normalize_and_validate("web_search", {"query": "球球大作战 最新活动"}, tools)
    assert err is None
    assert args["query"] == "球球大作战 最新活动"


# =========================================================================
# 问题 3：memory / todo 组摘要显示 Memory / Todo
# =========================================================================

def _builder():
    return RichMessageBuilder(chat_id=1)


def test_group_summary_memory_single():
    b = _builder()
    group = {"items": [{"id": "1", "type": "memory", "status": "done", "fn_args": {}}]}
    assert b._generate_group_summary(group) == "Memory"


def test_group_summary_todo_plural():
    b = _builder()
    group = {"items": [
        {"id": "1", "type": "todo", "status": "done", "fn_args": {}},
        {"id": "2", "type": "todo", "status": "done", "fn_args": {}},
    ]}
    assert b._generate_group_summary(group) == "Todo ×2"


def test_group_summary_noun_style_not_lowercased():
    b = _builder()
    group = {"items": [
        {"id": "1", "type": "bash", "status": "done", "fn_args": {}},
        {"id": "2", "type": "memory", "status": "done", "fn_args": {}},
        {"id": "3", "type": "todo", "status": "done", "fn_args": {}},
    ]}
    assert b._generate_group_summary(group) == "Ran a command, Memory, Todo"


def test_action_description_for_memory_todo():
    from ai.tool_summary import _generate_action_description
    assert _generate_action_description("memory", {}) == "updating memory"
    assert _generate_action_description("todo", {}) == "updating todos"


# =========================================================================
# 问题 4：组摘要追加 (failed n)
# =========================================================================

def test_group_summary_mixed_failures():
    b = _builder()
    group = {"items": [
        {"id": "1", "type": "bash", "status": "done", "fn_args": {}},
        {"id": "2", "type": "fetch_url", "status": "done", "fn_args": {}},
        {"id": "3", "type": "fetch_url", "status": "done", "fn_args": {}},
        {"id": "4", "type": "web_search", "status": "error", "fn_args": {}},
    ]}
    assert b._generate_group_summary(group) == "Ran a command, fetched 2 pages, (failed 1)"


def test_group_summary_all_failed():
    b = _builder()
    group = {"items": [
        {"id": "1", "type": "bash", "status": "error", "fn_args": {}},
        {"id": "2", "type": "web_search", "status": "error", "fn_args": {}},
    ]}
    assert b._generate_group_summary(group) == "(failed 2)"


def test_finish_group_all_failed_shows_failed_count():
    b = _builder()
    idx = b.start_new_tool_group()
    b.add_tool_item("t1", "bash", "cmd")
    b.update_tool_item("t1", "❌ Bash 执行失败", "<p>x</p>", status="error")
    b.finish_group(idx)
    assert b._tool_groups[idx]["outer_summary"] == "(failed 1)"


def test_finish_group_success_unchanged():
    b = _builder()
    idx = b.start_new_tool_group()
    b.add_tool_item("t1", "bash", "cmd")
    b.update_tool_item("t1", "ok", "<p>x</p>", status="done")
    b.finish_group(idx)
    assert b._tool_groups[idx]["outer_summary"] == "Ran a command"


def test_finish_group_five_tools_two_failed():
    b = _builder()
    idx = b.start_new_tool_group()
    for i in range(1, 6):
        b.add_tool_item(f"t{i}", "bash" if i <= 3 else "fetch_url", "s")
    b.update_tool_item("t1", "ok", "<p>x</p>", status="done")
    b.update_tool_item("t2", "ok", "<p>x</p>", status="done")
    b.update_tool_item("t3", "bad", "<p>x</p>", status="error")
    b.update_tool_item("t4", "ok", "<p>x</p>", status="done")
    b.update_tool_item("t5", "bad", "<p>x</p>", status="error")
    b.finish_group(idx)
    assert b._tool_groups[idx]["outer_summary"] == "Ran 2 commands, fetched a page, (failed 2)"

"""针对 4 项 UI / Schema 修复的回归测试。

覆盖：
1. web_search 与信息类工具（exchange_rate）结果用富文本卡片展示
   （web_search 为紧凑列表：标题链接 + 来源徽标，不含 section 头与
   斜体摘要）；message_user 及其他
   纯文本返回的工具，统一用 ``<pre><code>`` 等宽代码面板展示（与 bash /
   text_editor 同规范）；
2. ``description`` 不再被 normalize_tool_schema 强制注入 required，
   且只有 bash 声明该字段；web_search 不带 description 可通过校验；
3. memory / todo / subagent / deliver_reply 完成态摘要按「动作 + 对象」
   生成（与 text_editor 同规范），不再退化为默认的 "Ran an action"；
   组摘要同步按动作细分并改为动词短语，不再豁免首字母小写规范；
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
# 问题 1：message_user 等纯文本返回统一 <pre><code> 面板；
#         web_search / 信息类工具保留富文本卡片展示（从旧版恢复）
# =========================================================================

def test_web_search_result_rich_cards():
    envelope = (
        "🔍 [成功: Serper / Google] 搜索「np」的结果（1/1）：\n"
        "1. 标题：某标题\n"
        "   摘要：某摘要\n"
        "   链接：https://example.com/a\n"
    )
    summary, details = format_web_search_result({"query": "np"}, envelope)
    assert summary == "np 1 result"
    # 紧凑卡片：<ol> 列表 + 标题链接 + 来源徽标；
    # 前端不再显示 section 头与摘要（多结果累计太长），模型上下文仍保留完整摘要
    assert "<ol>" in details and "</ol>" in details
    assert '<b><a href="https://example.com/a">某标题</a></b>' in details
    assert "<code>example.com</code>" in details
    # section 头（🔍 「query」 引擎 · N/M 条）不再渲染
    assert "<b>🔍 「np」</b>" not in details
    assert "Serper / Google" not in details
    assert "1/1 条" not in details
    # 摘要 snippet 不再渲染
    assert "某摘要" not in details
    assert "<i>" not in details
    # 不再使用等宽代码面板
    assert "<pre><code>" not in details


def test_web_search_error_rich_fallback():
    summary, details = format_web_search_result(
        {"query": "球球大作战"}, "❌ 搜索失败：配额不足\n第二行"
    )
    assert summary == "Search failed"
    assert "<b>❌ 搜索失败</b>" in details
    assert "配额不足" in details
    assert "<pre><code>" not in details


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
])
def test_info_tools_result_rich_passthrough(fn_name, args):
    """信息类工具成功结果按富 HTML 原样透传进卡片（自旧版恢复）。"""
    rich = '<b>USD</b> 汇率 <code>7.12</code> CNY <a href="https://x.com/a">来源</a>'
    summary, details = asyncio.run(format_tool_result(fn_name, args, rich))
    assert details == rich                      # 成功结果原样透传，保留富文本排版
    assert "<pre><code>" not in details

    # 失败文本（"失败："前缀）：转义后展示，防止上游错误消息打坏 Rich Message
    summary, details = asyncio.run(
        format_tool_result(fn_name, args, "失败：上游 <api> 超时 & 重试失败")
    )
    assert "<pre><code>" not in details
    assert "失败：" in details
    assert "&lt;api&gt;" in details and "&amp;" in details


def test_unknown_tool_output_wrapped_in_code_panel():
    summary, details = asyncio.run(format_tool_result("whatever", {}, "raw <text>"))
    assert "<pre><code>" in details
    assert "raw &lt;text&gt;" in details


# =========================================================================
# 问题 2：description 仅 bash 声明且必填
# =========================================================================

def test_normalize_tool_schema_does_not_inject_required_description():
    tool = {
        "type": "function",
        "function": {
            "name": "web_search",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": [],
            },
        },
    }
    norm = normalize_tool_schema(tool)
    assert norm["function"]["parameters"]["required"] == []
    # 字段仍排在 properties 首位（展示顺序规范化保留）
    assert list(norm["function"]["parameters"]["properties"])[0] == "description"
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
    assert "description" not in ws_props
    assert "description" not in (by_name["web_search"]["function"]["parameters"].get("required") or [])

    bash_params = by_name["bash"]["function"]["parameters"]
    assert "description" in bash_params["properties"]
    assert bash_params["required"] == ["description", "command"]

    for name in ("weather", "exchange_rate",
                 "geocode", "route", "distance", "poi_keyword_search",
                 "poi_nearby_search", "poi_details"):
        assert "description" not in by_name[name]["function"]["parameters"]["properties"], name


def test_memory_todo_schemas_have_no_description():
    from memory_tool import MEMORY_TOOL
    from todo_tool import TODO_TOOL
    assert "description" not in MEMORY_TOOL["function"]["parameters"]["properties"]
    assert "description" not in TODO_TOOL["function"]["parameters"]["properties"]


def test_web_search_without_description_passes_validation():
    tools = valid_tool_defs(
        [t for t in __import__("search.tool_schemas", fromlist=["SEARCH_TOOLS"]).SEARCH_TOOLS
         if isinstance(t, dict)]
    )
    args, err = normalize_and_validate("web_search", {"query": "球球大作战 最新活动"}, tools)
    assert err is None
    assert args["query"] == "球球大作战 最新活动"


# =========================================================================
# 问题 3：todo / memory / subagent / deliver_reply 按动作拆分摘要
# =========================================================================

def _builder():
    return RichMessageBuilder(chat_id=1)


def test_group_summary_memory_single():
    b = _builder()
    group = {"items": [{"id": "1", "type": "memory", "status": "done", "fn_args": {}}]}
    assert b._generate_group_summary(group) == "Listed memories"


def test_group_summary_todo_plural():
    b = _builder()
    group = {"items": [
        {"id": "1", "type": "todo", "status": "done", "fn_args": {"action": "add"}},
        {"id": "2", "type": "todo", "status": "done", "fn_args": {"action": "add"}},
    ]}
    assert b._generate_group_summary(group) == "Added 2 todos"


def test_group_summary_verb_phrases_lowercased():
    """todo / memory 改为动词短语后不再豁免首字母小写规范。"""
    b = _builder()
    group = {"items": [
        {"id": "1", "type": "bash", "status": "done", "fn_args": {}},
        {"id": "2", "type": "memory", "status": "done", "fn_args": {"action": "add"}},
        {"id": "3", "type": "todo", "status": "done", "fn_args": {"action": "add"}},
    ]}
    assert b._generate_group_summary(group) == "Ran a command, saved a memory, added a todo"


def test_group_summary_first_desc_capitalized():
    b = _builder()
    group = {"items": [
        {"id": "1", "type": "memory", "status": "done", "fn_args": {"action": "add"}},
        {"id": "2", "type": "bash", "status": "done", "fn_args": {}},
    ]}
    assert b._generate_group_summary(group) == "Saved a memory, ran a command"


def test_action_description_for_memory_todo():
    from ai.tool_summary import _generate_action_description
    assert _generate_action_description("memory", {}) == "listing memories"
    assert _generate_action_description("todo", {}) == "listing todos"
    assert _generate_action_description("todo", {"action": "add"}) == "adding a todo"
    # 惯性携带的 description 不被采用（位于 custom_desc 检查之前）
    assert _generate_action_description("todo", {"action": "add", "description": "写待办"}) == "adding a todo"


def test_single_block_done_summaries_todo():
    from ai.tool_summary import _generate_tool_summary_done as done
    add = '{"ok":true,"action":"add","todo":{"title":"买牛奶","done":false}}'
    assert done("todo", {"action": "add"}, add) == "Added todo 买牛奶"
    assert done("todo", {"action": "list"}, '{"ok":true,"action":"list","total":3}') == "Listed todos"
    toggle_done = '{"ok":true,"action":"toggle","todo":{"title":"买牛奶","done":true}}'
    toggle_undone = '{"ok":true,"action":"toggle","todo":{"title":"买牛奶","done":false}}'
    assert done("todo", {"action": "done", "todo_id": "ab"}, toggle_done) == "Completed todo 买牛奶"
    assert done("todo", {"action": "undone", "todo_id": "ab"}, toggle_undone) == "Reopened todo 买牛奶"
    assert done("todo", {"action": "toggle", "todo_id": "ab"}, toggle_done) == "Completed todo 买牛奶"
    assert done("todo", {"action": "toggle", "todo_id": "ab"}, toggle_undone) == "Reopened todo 买牛奶"
    assert done("todo", {"action": "edit"}, '{"ok":true,"action":"edit","todo":{"title":"新标题"}}') == "Updated todo 新标题"
    assert done("todo", {"action": "delete"}, '{"ok":true,"action":"delete","todo":{"title":"旧任务"}}') == "Deleted todo 旧任务"
    assert done("todo", {"action": "clear"}, '{"ok":true,"action":"clear","removed":4}') == "Cleared 4 todos"
    assert done("todo", {"action": "clear"}, '{"ok":true,"action":"clear","removed":0}') == "Cleared the todo list"
    # 长标题截短
    long = '{"ok":true,"action":"add","todo":{"title":"' + "很长的标题" * 10 + '"}}'
    assert done("todo", {"action": "add"}, long).endswith("…")
    # 非法 JSON：按请求动作兜底
    assert done("todo", {"action": "add"}, "not-json") == "Added a todo"


def test_single_block_done_summaries_memory():
    from ai.tool_summary import _generate_tool_summary_done as done
    add = '{"ok":true,"action":"add","memory":{"content":"用户对花生过敏"}}'
    assert done("memory", {"action": "add"}, add) == "Saved memory: 用户对花生过敏"
    assert done("memory", {"action": "get"}, add) == "Retrieved memory: 用户对花生过敏"
    assert done("memory", {"action": "update"}, add) == "Updated memory: 用户对花生过敏"
    assert done("memory", {"action": "delete"}, add) == "Deleted memory: 用户对花生过敏"
    assert done("memory", {"action": "search"}, '{"ok":true,"action":"search","matches":2}') == "Searched memories"
    assert done("memory", {"action": "clear"}, '{"ok":true,"action":"clear","removed":3}') == "Cleared 3 memories"
    assert done("memory", {}, '{"ok":true,"action":"list","total":0}') == "Listed memories"
    assert done("memory", {"action": "add"}, "not-json") == "Saved a memory"


def test_single_block_done_summaries_subagent_deliver_reply():
    from ai.tool_summary import _generate_tool_summary_done as done
    assert done("subagent", {}, '{"ok":true,"rounds":3}') == "Ran a subagent"
    delivered = "已发送：本轮最后一条消息正文已永久发送给用户，交付完成。"
    suppressed = "未发送：send=false，本轮保持静默，用户不会收到任何内容。"
    assert done("deliver_reply", {"send": True}, delivered) == "Delivered the final reply"
    assert done("deliver_reply", {"send": False}, suppressed) == "Skipped the final reply"
    # send 未填（TIMER 回合缺省 false）：按实际结果区分
    assert done("deliver_reply", {}, suppressed) == "Skipped the final reply"
    assert done("deliver_reply", {}, delivered) == "Delivered the final reply"


def test_deliver_reply_failure_uses_dedicated_title():
    """交付失败（"失败："前缀）走 error 路径，显示专用失败标题而非 🔧 兜底。"""
    summary, details = asyncio.run(
        format_tool_result("deliver_reply", {"send": True}, "失败：消息发送异常，可稍后重试。")
    )
    assert summary == "❌ 最终回复未交付"
    assert "<pre><code>" in details and "消息发送异常" in details
    # 静默是正常终态：formatted_summary 备用值不覆盖完成态摘要
    summary, _ = asyncio.run(
        format_tool_result("deliver_reply", {"send": False}, "未发送：send=false，本轮保持静默。")
    )
    assert summary == "💬 已跳过交付"


def test_single_block_running_summaries():
    from ai.tool_summary import _generate_initial_tool_summary as running
    assert running("todo", {"action": "add"}) == "Adding a todo"
    assert running("todo", {"action": "done"}) == "Completing a todo"
    assert running("todo", {"action": "undone"}) == "Reopening a todo"
    assert running("todo", {}) == "Listing todos"
    assert running("memory", {"action": "add"}) == "Saving a memory"
    assert running("memory", {"action": "search"}) == "Searching memories"
    assert running("memory", {}) == "Listing memories"
    assert running("subagent", {}) == "Running a subagent"
    assert running("deliver_reply", {}) == "Delivering the final reply"


def test_group_type_derivation_for_action_tools():
    b = _builder()
    assert b._get_group_type_for_item({"type": "todo", "fn_args": {"action": "add"}}) == "todo_add"
    assert b._get_group_type_for_item({"type": "todo", "fn_args": {}}) == "todo_list"
    assert b._get_group_type_for_item({"type": "todo", "fn_args": {"action": "done"}}) == "todo_done"
    # toggle 方向从条目最终摘要回推
    assert b._get_group_type_for_item(
        {"type": "todo", "fn_args": {"action": "toggle"}, "summary": "Completed todo 买牛奶"}) == "todo_done"
    assert b._get_group_type_for_item(
        {"type": "todo", "fn_args": {"action": "toggle"}, "summary": "Reopened todo 买牛奶"}) == "todo_undone"
    assert b._get_group_type_for_item({"type": "memory", "fn_args": {"action": "search"}}) == "memory_search"
    assert b._get_group_type_for_item({"type": "memory", "fn_args": {"action": "bad"}}) == "memory_list"
    assert b._get_group_type_for_item(
        {"type": "deliver_reply", "fn_args": {"send": False}}) == "deliver_reply_silent"
    assert b._get_group_type_for_item(
        {"type": "deliver_reply", "fn_args": {"send": True}}) == "deliver_reply"
    # send 未填：从条目最终摘要回推
    assert b._get_group_type_for_item(
        {"type": "deliver_reply", "fn_args": {}, "summary": "Skipped the final reply"}) == "deliver_reply_silent"
    assert b._get_group_type_for_item(
        {"type": "deliver_reply", "fn_args": {}, "summary": "Delivered the final reply"}) == "deliver_reply"


def test_group_summary_deliver_reply_and_clear():
    b = _builder()
    group = {"items": [
        {"id": "1", "type": "memory", "status": "done", "fn_args": {"action": "clear"},
         "summary": "Cleared 3 memories"},
        {"id": "2", "type": "deliver_reply", "status": "done", "fn_args": {"send": False},
         "summary": "Skipped the final reply"},
    ]}
    assert b._generate_group_summary(group) == "Cleared memories, skipped the final reply"


def test_group_summary_failure_suffix_unchanged():
    b = _builder()
    group = {"items": [
        {"id": "1", "type": "memory", "status": "done", "fn_args": {"action": "add"}},
        {"id": "2", "type": "web_search", "status": "error", "fn_args": {}},
    ]}
    assert b._generate_group_summary(group) == "Saved a memory, (failed 1)"


def test_update_tool_args_refreshes_action_description():
    """流式参数增量期间，进行态摘要与动作描述应随 action 一并刷新。"""
    b = _builder()
    idx = b.start_new_tool_group()
    b.add_tool_item("t1", "todo", "Listing todos", action_description="listing todos", fn_args={})
    b.update_tool_args("t1", {"action": "add", "title": "买牛奶"})
    item = b._tool_groups[idx]["items"][0]
    assert item["summary"] == "Adding a todo"
    assert item["action_description"] == "adding a todo"
    # 工具组进行态标题同步刷新
    b._refresh_outer_summary(b._tool_groups[idx])
    assert b._tool_groups[idx]["outer_summary"] == "Adding a todo..."


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

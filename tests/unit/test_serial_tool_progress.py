"""同批串行工具的进度展示 + 图片文案回归。

期望的前端效果：模型一次发 3 个 generate_image，执行是串行的——
第一个工具「进行中 → 完成」，然后第二个才出现「进行中 → 完成」，再是第三个；
没轮到执行的工具不显示（不引入任何新状态，只是不渲染）。
工具组结束后不统计数量，像 "Searched the web" 一样固定写 "Generated image(s)"。
单个工具完成标题仍按结果里实际返回的图片张数。
"""
import pytest

import token_budget
from ai.rich_message_builder import RichMessageBuilder
from ai.tool_summary import _generate_tool_summary_done, image_result_count


class _FakeEncoding:
    def encode(self, text: str, disallowed_special=()) -> list:
        return list(text.encode("utf-8"))

    def decode(self, tokens: list) -> str:
        return bytes(tokens).decode("utf-8", errors="ignore")


@pytest.fixture(autouse=True)
def _stub_tiktoken_encoding(monkeypatch):
    token_budget._get_encoding.cache_clear()
    monkeypatch.setattr(token_budget, "_get_encoding", lambda name: _FakeEncoding())
    yield


def _image_call(call_id: str, num_images: int = 2) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "generate_image",
            "arguments": f'{{"prompt": "p-{call_id}", "model": "m", "num_images": {num_images}}}',
        },
    }


def _ok(n: int, tag: str) -> str:
    links = "\n".join(f"https://r2.example/{tag}-{i}.png?X-Amz-Signature=s&x=1" for i in range(n))
    return f"✅ 已生成 {n} 张图片。\n图片链接：\n{links}"


def _visible(builder: RichMessageBuilder) -> list[tuple[str, str]]:
    """前端当前能看到的条目：[(id, status)]，隐藏的不算。"""
    return [(it["id"], it["status"])
            for g in builder._tool_groups for it in g["items"] if not it.get("hidden")]


def test_image_result_count_counts_real_links():
    assert image_result_count("generate_image", _ok(2, "a")) == 2
    assert image_result_count("generate_image", _ok(1, "a")) == 1
    assert image_result_count("generate_image", "❌ 图片生成失败") is None
    assert image_result_count("web_search", _ok(2, "a")) is None


def test_item_done_summary_uses_actual_count_not_requested():
    # 请求 3 张，ModelScope 并发子任务只成功 1 张
    assert _generate_tool_summary_done(
        "generate_image", {"num_images": 3}, _ok(1, "a")) == "Generated an image"
    assert _generate_tool_summary_done(
        "generate_image", {"num_images": 2}, _ok(2, "a")) == "Generated 2 images"
    assert _generate_tool_summary_done(
        "generate_image", {"num_images": 3}, "r") == "Generated 3 images"


def test_group_summary_is_fixed_text_not_counted():
    b = RichMessageBuilder(chat_id=1)

    def group(*args_list):
        return {"items": [
            {"id": str(i), "type": "generate_image", "status": "done", "fn_args": a}
            for i, a in enumerate(args_list)
        ]}

    # 无论 1 次还是多次调用、请求几张，组标题都不带数量
    assert b._generate_group_summary(group({})) == "Generated image(s)"
    assert b._generate_group_summary(group({"num_images": 2})) == "Generated image(s)"
    assert b._generate_group_summary(group({}, {}, {"num_images": 4})) == "Generated image(s)"
    # 编辑同理；混合时按规范首字母大写 + 后续小写
    edit = {"image_url": "https://x/y.png"}
    assert b._generate_group_summary(group(edit)) == "Edited image(s)"
    assert b._generate_group_summary(group({}, edit)) == "Generated image(s), edited image(s)"
    # 旧名别名同样聚合
    legacy = {"items": [
        {"id": "1", "type": "generate_image_from_text", "status": "done", "fn_args": {}},
        {"id": "2", "type": "edit_image_with_reference", "status": "done", "fn_args": edit},
    ]}
    assert b._generate_group_summary(legacy) == "Generated image(s), edited image(s)"


def test_hidden_items_are_not_rendered_until_revealed():
    b = RichMessageBuilder(chat_id=1)
    b.request_flush = lambda force=False: None
    b.start_new_tool_group()
    group = b._tool_groups[-1]
    for tid in ("t1", "t2", "t3"):
        b.add_tool_item(tid, "generate_image", "Generating 2 images", fn_args={"num_images": 2})

    b.hide_tools_until_started(["t2", "t3"])
    html = b._build_tool_group_html(group)
    assert html.count("<details><summary>Generating 2 images") == 2  # 外层组 + 仅 t1
    assert [i for i, _ in _visible(b)] == ["t1"]
    assert group["outer_summary"] == "Generating 2 images"

    b.update_tool_item("t1", "Generated 2 images", "<p>x</p>", status="done")
    b.reveal_tool("t2")
    assert [i for i, _ in _visible(b)] == ["t1", "t2"]
    assert [s for _, s in _visible(b)] == ["done", "running"]
    # status 里从头到尾没有 queued 这种东西
    assert all(it["status"] in ("running", "done") for it in group["items"])

    # 终态一定可见：即使从未执行过（如预算耗尽被跳过）
    b.update_tool_item("t3", "Not executed (budget)", "<p>x</p>", status="error")
    assert [i for i, _ in _visible(b)] == ["t1", "t2", "t3"]


def test_group_with_only_hidden_items_renders_nothing():
    b = RichMessageBuilder(chat_id=1)
    b.request_flush = lambda force=False: None
    b.start_new_tool_group()
    b.add_tool_item("t1", "generate_image", "Generating an image", fn_args={})
    b.hide_tools_until_started(["t1"])
    assert b._build_tool_group_html(b._tool_groups[-1]) == ""


@pytest.mark.asyncio
async def test_serial_batch_shows_one_tool_at_a_time(monkeypatch):
    from ai import tool_call_loop

    builder = RichMessageBuilder(chat_id=42)
    builder.request_flush = lambda force=False: None
    snapshots: list[list[tuple[str, str]]] = []
    n_by_call = {"c1": 2, "c2": 1, "c3": 2}

    async def _fake_dispatch(name, arguments, chat_id=None, progress_callback=None):
        snapshots.append(_visible(builder))
        return _ok(n_by_call[arguments["prompt"][2:]], arguments["prompt"])

    monkeypatch.setattr(tool_call_loop, "dispatch_tool_call", _fake_dispatch)

    loop_messages: list = []
    status = await tool_call_loop._run_tool_calls_and_append(
        [_image_call("c1"), _image_call("c2"), _image_call("c3")],
        loop_messages, [], [0], "test", builder, chat_id=42, tools=None,
    )
    assert status == "continue"

    # 执行每个工具那一刻前端所见：只有已完成的 + 当前这个，后面的不出现
    assert snapshots == [
        [("c1", "running")],
        [("c1", "done"), ("c2", "running")],
        [("c1", "done"), ("c2", "done"), ("c3", "running")],
    ]
    items = builder._tool_groups[-1]["items"]
    assert [it["summary"] for it in items] == [
        "Generated 2 images", "Generated an image", "Generated 2 images"]
    assert builder._tool_groups[-1]["outer_summary"] == "Generated image(s)"
    assert [m.tool_result_block().tool_call_id for m in loop_messages if m.role == "tool"] == ["c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_single_tool_batch_has_nothing_hidden(monkeypatch):
    from ai import tool_call_loop

    builder = RichMessageBuilder(chat_id=42)
    builder.request_flush = lambda force=False: None
    seen: list = []

    async def _fake_dispatch(name, arguments, chat_id=None, progress_callback=None):
        seen.append(_visible(builder))
        return _ok(2, "solo")

    monkeypatch.setattr(tool_call_loop, "dispatch_tool_call", _fake_dispatch)
    await tool_call_loop._run_tool_calls_and_append(
        [_image_call("only")], [], [], [0], "test", builder, chat_id=42, tools=None)
    assert seen == [[("only", "running")]]
    assert builder._tool_groups[-1]["outer_summary"] == "Generated image(s)"


# ---------------------------------------------------------------------
# 生产路径：tool_call_loop 拿到的是 DraftManager（事件流 + 滚动缓冲），不是裸 builder
# ---------------------------------------------------------------------
def _draft_manager():
    from ai.draft_manager import DraftManager

    builder = RichMessageBuilder(chat_id=42)
    builder.request_flush = lambda force=False: None
    return builder, DraftManager(builder)


@pytest.mark.asyncio
async def test_serial_batch_through_draft_manager(monkeypatch):
    from ai import tool_call_loop

    builder, manager = _draft_manager()
    snapshots: list = []

    async def _fake_dispatch(name, arguments, chat_id=None, progress_callback=None):
        snapshots.append(_visible(builder))
        return _ok(2, arguments["prompt"])

    monkeypatch.setattr(tool_call_loop, "dispatch_tool_call", _fake_dispatch)
    await tool_call_loop._run_tool_calls_and_append(
        [_image_call("c1"), _image_call("c2"), _image_call("c3")],
        [], [], [0], "test", manager, chat_id=42, tools=None,
    )
    assert snapshots == [
        [("c1", "running")],
        [("c1", "done"), ("c2", "running")],
        [("c1", "done"), ("c2", "done"), ("c3", "running")],
    ]
    assert builder._tool_groups[-1]["outer_summary"] == "Generated image(s)"


@pytest.mark.asyncio
async def test_concurrent_safe_first_group_not_hidden(monkeypatch):
    """开头连续的并发安全工具同时开始，不应被隐藏；其后的串行工具才隐藏到轮到。"""
    from ai import tool_call_loop

    builder = RichMessageBuilder(chat_id=42)
    builder.request_flush = lambda force=False: None
    seen: dict[str, list] = {}

    async def _fake_dispatch(name, arguments, chat_id=None, progress_callback=None):
        key = name if name != "web_search" else f"web_search:{arguments['query']}"
        seen[key] = [i for i, _ in _visible(builder)]
        return _ok(1, "x") if name == "generate_image" else "ok"

    monkeypatch.setattr(tool_call_loop, "dispatch_tool_call", _fake_dispatch)
    calls = [
        {"id": "s1", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "a"}'}},
        {"id": "s2", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "b"}'}},
        _image_call("g1", 1),
    ]
    await tool_call_loop._run_tool_calls_and_append(
        calls, [], [], [0], "test", builder, chat_id=42, tools=None)
    # 两个搜索启动时都可见，图片尚未出现；图片启动时已含前两个
    assert "g1" not in seen["web_search:a"] and "g1" not in seen["web_search:b"]
    assert {"s1", "s2"} <= set(seen["web_search:a"]) | set(seen["web_search:b"])
    assert seen["generate_image"] == ["s1", "s2", "g1"]


def test_hide_and_reveal_replay_in_order_after_rollover():
    """滚动换血期间事件进缓冲，回放后效果与不滚动时一致。"""
    builder, manager = _draft_manager()
    manager._swap_scheduled = True  # 模拟后台滚动进行中：事件只入缓冲

    for tid in ("t1", "t2"):
        manager.add_tool_item(tid, "generate_image", "Generating 2 images", fn_args={"num_images": 2})
    manager.hide_tools_until_started(["t2"])
    manager.update_tool_item("t1", "Generated 2 images", "<p>x</p>", status="done")
    manager.reveal_tool("t2")
    assert builder._tool_groups == []  # 缓冲期间旧草稿不被直写

    manager._swap_scheduled = False
    manager._replay_buffer()

    assert _visible(builder) == [("t1", "done"), ("t2", "running")]

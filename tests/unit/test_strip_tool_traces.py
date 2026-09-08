# -*- coding: utf-8 -*-
"""strip_tool_traces（能力维度全量清除）单元测试。

覆盖场景（对应 tool_visibility.strip_tool_traces 的行为契约）：
  - assistant 剔除全部 ToolCallBlock，文本保留；
  - role=tool 消息整条移除（含未配对的孤儿结果）；
  - 只有工具调用没有正文的 assistant 空壳整条丢弃；
  - 未被打断的"无结果 tool_call"同样清除（drop 不需要配对回溯）；
  - user / system 消息原样引用（零拷贝）；
  - 持久历史对象绝不被原地修改（只改出站副本）；
  - 旧 dict 形状兼容（assistant dict 剔除 tool_calls 键、tool dict 移除、
    空壳 dict 丢弃、非 assistant/tool dict 直通）；
  - 无工具痕迹时零开销直通（返回原列表对象）；
  - 确定性：同一输入两次调用结果一致；
  - wire 级验证：清理后的 Message 渲染出的 OpenAI JSON 不含 tool_calls
    键、不含 role=tool 消息（三条协议出站的共同上游）。
"""
import json

from core.messages import (
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    render_openai_messages,
)
from tool_visibility import strip_tool_traces


def _assistant_with_calls(text, calls):
    """构造带工具调用的 assistant 消息（calls: [(id, name, args_dict)]）。"""
    msg = Message.assistant_text(text)
    for cid, name, args in calls:
        msg.blocks.append(ToolCallBlock(id=cid, name=name, arguments=args))
    return msg


def _tool_result(call_id, name, content):
    return Message.tool_result(call_id, name, content)


# ---------------------------------------------------------------------------
# Message 路径
# ---------------------------------------------------------------------------
def test_assistant_text_kept_tool_calls_removed():
    history = [
        Message.system("sys"),
        Message.user_text("帮我搜下天气"),
        _assistant_with_calls("我来查一下", [("c1", "web_search", {"q": "天气"})]),
        _tool_result("c1", "web_search", "晴 25 度"),
        _assistant_with_calls("今天晴，25 度。", []),
    ]
    out = strip_tool_traces(history)

    assert [m.role for m in out] == ["system", "user", "assistant", "assistant"]
    # 文本保留，工具调用块被剔除
    assert out[2].text() == "我来查一下"
    assert out[2].tool_calls() == []
    assert out[3].text() == "今天晴，25 度。"


def test_tool_messages_removed_including_orphans():
    """role=tool 消息全部移除——包括没有配对 tool_call 的孤儿结果。"""
    history = [
        _assistant_with_calls("查一下", [("c1", "t1", {})]),  # 带正文，剔除后保留
        _tool_result("c1", "t1", "r1"),
        _tool_result("c_orphan", "t2", "无主结果"),
        Message.user_text("下一问"),
    ]
    out = strip_tool_traces(history)
    assert all(m.role != "tool" for m in out)
    assert [m.role for m in out] == ["assistant", "user"]
    assert out[0].text() == "查一下"


def test_call_only_assistant_shell_dropped():
    """只有工具调用、没有正文的 assistant 剔除后成空壳 → 整条丢弃。"""
    history = [
        _assistant_with_calls(None, [("c1", "t1", {})]),
        _tool_result("c1", "t1", "r1"),
        Message.user_text("继续"),
    ]
    out = strip_tool_traces(history)
    assert [m.role for m in out] == ["user"]


def test_unanswered_tool_call_also_stripped():
    """被打断回合留下的无结果 tool_call：drop 模式不需要配对回溯，直接清除。"""
    history = [
        _assistant_with_calls("查一下", [("c1", "t1", {})]),  # 无配对 tool 消息
        Message.user_text("换个话题"),
    ]
    out = strip_tool_traces(history)
    assert [m.role for m in out] == ["assistant", "user"]
    assert out[0].text() == "查一下"
    assert out[0].tool_calls() == []


def test_upstream_objects_untouched():
    """纯函数契约：持久历史对象绝不被原地修改，切回支持工具的模型自动恢复。"""
    call_block = ToolCallBlock(id="c1", name="t1", arguments={"a": 1})
    assistant = Message.assistant_text("说明文字")
    assistant.blocks.append(call_block)
    history = [assistant, _tool_result("c1", "t1", "r1")]

    out = strip_tool_traces(history)

    # 出站副本已清理
    assert out[0].tool_calls() == []
    # 持久历史原对象完好
    assert assistant.tool_calls() == [call_block]
    assert assistant.blocks[1] is call_block
    assert history[1].role == "tool"
    # 未改写的消息保持原对象引用（user 消息零拷贝直通）
    user = Message.user_text("hi")
    out2 = strip_tool_traces([user, _tool_result("cX", "t", "r")])
    assert out2[0] is user


def test_parallel_calls_and_results_all_gone():
    """一轮多个并行调用 + 多条结果：全部清除。"""
    history = [
        _assistant_with_calls("并行查", [
            ("c1", "t1", {"q": 1}),
            ("c2", "t1", {"q": 2}),
            ("c3", "t2", {"q": 3}),
        ]),
        _tool_result("c1", "t1", "r1"),
        _tool_result("c2", "t1", "r2"),
        _tool_result("c3", "t2", "r3"),
        Message.user_text("谢谢"),
    ]
    out = strip_tool_traces(history)
    assert [m.role for m in out] == ["assistant", "user"]
    assert out[0].text() == "并行查"
    assert out[0].tool_calls() == []


# ---------------------------------------------------------------------------
# 旧 dict 形状（双形状过渡期兼容）
# ---------------------------------------------------------------------------
def test_legacy_dict_shapes():
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": "有正文的调用",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "t1", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "t1", "content": "r1"},
        {
            "role": "assistant",
            "content": None,  # 只有调用没有正文
            "tool_calls": [{"id": "c2", "type": "function",
                            "function": {"name": "t2", "arguments": "{}"}}],
        },
        {"role": "user", "content": "下一问"},
    ]
    out = strip_tool_traces(history)

    assert [m.get("role") for m in out] == ["system", "user", "assistant", "user"]
    assert "tool_calls" not in out[2]
    assert out[2]["content"] == "有正文的调用"
    # 入参 dict 不被原地修改
    assert history[2]["tool_calls"][0]["id"] == "c1"
    assert history[4]["tool_calls"][0]["id"] == "c2"


# ---------------------------------------------------------------------------
# 直通与确定性
# ---------------------------------------------------------------------------
def test_no_traces_passthrough_returns_same_list():
    """无工具痕迹：零开销直通，返回原列表对象。"""
    history = [Message.system("s"), Message.user_text("u"),
               Message.assistant_text("a")]
    assert strip_tool_traces(history) is history
    assert strip_tool_traces([]) == []


def test_deterministic_output():
    """同一输入两次调用结果一致（前缀缓存友好的确定性保证）。"""
    history = [
        Message.user_text("q"),
        _assistant_with_calls("调", [("c1", "t1", {"a": 1})]),
        _tool_result("c1", "t1", "r"),
    ]
    out1 = strip_tool_traces(history)
    out2 = strip_tool_traces(history)
    assert len(out1) == len(out2)
    for a, b in zip(out1, out2):
        assert a.to_openai_dict() == b.to_openai_dict()


# ---------------------------------------------------------------------------
# wire 级验证（三条协议出站的共同上游：OpenAI 渲染出口）
# ---------------------------------------------------------------------------
def test_rendered_openai_wire_has_no_tool_traces():
    history = [
        Message.system("sys"),
        Message.user_text("帮我搜下天气"),
        _assistant_with_calls("我来查一下", [("c1", "web_search", {"q": "天气"})]),
        _tool_result("c1", "web_search", "晴 25 度"),
        _assistant_with_calls("今天晴，25 度。", []),
        Message.user_text("画张图"),
    ]
    wire = render_openai_messages(strip_tool_traces(history))

    assert all("tool_calls" not in m for m in wire)
    assert all(m["role"] != "tool" for m in wire)
    # 正文与轮次结构保持完整
    assert [m["role"] for m in wire] == [
        "system", "user", "assistant", "assistant", "user",
    ]
    assert wire[2]["content"] == "我来查一下"
    assert wire[3]["content"] == "今天晴，25 度。"


def test_call_only_shell_not_rendered_as_invalid_assistant():
    """空壳丢弃后不会渲染出 content=None 且无 tool_calls 的非法 assistant。"""
    history = [
        _assistant_with_calls(None, [("c1", "t1", {})]),
        _tool_result("c1", "t1", "r"),
        Message.user_text("下一问"),
    ]
    wire = render_openai_messages(strip_tool_traces(history))
    for m in wire:
        if m["role"] == "assistant":
            assert m.get("content") or m.get("tool_calls"), \
                "非法 assistant 消息：content 为空且无 tool_calls"


if __name__ == "__main__":
    raise SystemExit("请用 pytest 运行：pytest tests/unit/test_strip_tool_traces.py")

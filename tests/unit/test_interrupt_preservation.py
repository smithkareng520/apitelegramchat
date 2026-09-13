# tests/unit/test_interrupt_preservation.py
"""打断信息丢失修复的回归测试（对应问题排查文档的改动指南）+
五阶段打断规范测试（2026-09 二期重构）。

问题回顾（修复前）：
  ``content_acc`` / ``reasoning_acc`` 是流式循环的局部变量，只有循环正常
  跑完才经 append_assistant_message 写入 journal；``except CancelledError:
  raise`` 在写入之前——打断一旦发生，已产出文本从未落地。草稿层（用户
  可见）保全了"数到 123"，历史层（模型记忆）完全为空。

五阶段打断规范（本次重构，数据保留基准"文本看前端、数据看后台"）：

  阶段1  思考推演中打断 → 残缺思考全量丢弃（不进历史，防污染隐空间）
  阶段2  文本输出中打断 → journal 文本按前端渲染确认游标物理截断
         （后端 500 字 / 前端只渲染 100 字 → 历史只留 100 字）
  阶段3  工具参数 JSON 未收完打断 → 半截 tool_use 整体剥离（无悬空）
  阶段4a 只读工具执行中打断 → 取消拒断底层请求 + aborted 占位回执
  阶段4b 写操作工具执行中打断 → shield 脱离后台死等终态，回写历史
  阶段5  工具结果刚返回打断 → 调用声明与真实数据全量保留

覆盖（问题文档"验证方式建议"五条 + 五阶段场景 + 机制组件单元）：

  场景1  纯文本长输出中途打断 → 历史保全已产出文本（模型能看到说到哪）
  场景2  工具参数 JSON 还没收完就打断 → journal 无悬空半截 tool_use
  场景3  工具已完整发起、等待执行结果时打断 → assistant(tool_call) +
         占位 tool_result 配对齐全（aborted 状态语义）
  场景4  多轮工具调用，末轮进行到一半被打断 → 前几轮完整保留
  场景5  打断后立刻发新消息 → 历史含进度、新消息正常追加（衔接可续）

组件单元（改动点 1/3/4/5 的机制面 + 五阶段专用）：
  - LiveAssistantSlot：sync 增量同步 / finalize 双列表同一对象 /
         LIVE_STREAM_FLAG 直播标记生命周期（裁剪判据）
  - _normalize_journal：空占位与"只思考"占位剔除（Codex #22602 防御）
  - trim_interrupted_stream：阶段1/2 字段级裁剪（游标截断 / 思考丢弃 /
         定稿消息不误伤 / synthetic 前文排除）
  - MediaProgressSlot：媒体生成占位 complete / drop / 幂等 / synthetic 标记
  - 草稿层↔历史层反向校验：失同步 WARNING / 一致 INFO（改动点3）
  - persist_user_message_entry 合并分支防御性 ERROR（改动点5）
  - attach_render_cursor：游标引用绑定 / 零拷贝读取
  - writeback_detached_tool_result：脱离工具终态原地替换历史占位
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Optional

import pytest

import turn_recovery
from turn_recovery import (
    INTERRUPTED_TOOL_PLACEHOLDER,
    DETACHED_TOOL_PLACEHOLDER,
    LIVE_STREAM_FLAG,
    SYNTHETIC_ASSISTANT_FLAG,
    _normalize_journal,
    attach_render_cursor,
    finalize_interrupted_turn,
    persist_user_message_entry,
    register_inflight_turn,
    trim_interrupted_stream,
    writeback_detached_tool_result,
)
from state import get_or_init_context
from core.messages import Message, TextBlock, render_openai_messages

from ai.agentic_loops import _agentic_loop_openai_compat
from ai.bridge_common import LiveAssistantSlot, MediaProgressSlot
from ai.rich_message_builder import _log_draft_journal_consistency

# config.py 默认注册的聊天模型（无环境变量依赖），provider=agnes。
TEST_MODEL = "agnes-3.0-flash"

# 每个用例独立 chat_id（与会话级全局 user_contexts / _inflight 隔离）。
_CHAT_SEQ = 92_000_000


def _next_chat_id() -> int:
    global _CHAT_SEQ
    _CHAT_SEQ += 1
    return _CHAT_SEQ


# ===========================================================================
# 假件：OpenAI 兼容流式客户端（SSE chunk 形状与 openai SDK 对齐）
# ===========================================================================
class FakeStream:
    """按序吐 chunk 的假流；chunks 耗尽后按 hang 标志挂起或正常终止。"""

    def __init__(self, chunks: list, hang: bool = False):
        self._chunks = list(chunks)
        self._i = 0
        self._hang = hang
        # 全部 chunk 已消费、流"仍在接收中"（挂起）时置位——测试据此
        # 得知取消将落在流式过程中而非收尾阶段。
        self.hanging = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i < len(self._chunks):
            chunk = self._chunks[self._i]
            self._i += 1
            return chunk
        if self._hang:
            self.hanging.set()
            await asyncio.sleep(3600)  # 模拟模型仍在输出，永远不到终止事件
        raise StopAsyncIteration


class FakeCompletions:
    def __init__(self, stream_factory):
        self._factory = stream_factory
        self.calls = 0

    async def create(self, **params):
        self.calls += 1
        return self._factory(self.calls - 1)


class FakeClient:
    """client.chat.completions.create(**params) 的最小假件。"""

    def __init__(self, stream_factory):
        self.chat = SimpleNamespace(completions=FakeCompletions(stream_factory))


def _text_chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            finish_reason=None,
            delta=SimpleNamespace(content=text, tool_calls=None),
        )],
    )


def _reasoning_chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            finish_reason=None,
            delta=SimpleNamespace(content="", reasoning=text, tool_calls=None),
        )],
    )


def _tool_call_chunk(idx: int, tc_id: str, name: str, args: str) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            finish_reason=None,
            delta=SimpleNamespace(content="", tool_calls=[SimpleNamespace(
                index=idx, id=tc_id,
                function=SimpleNamespace(name=name, arguments=args),
            )]),
        )],
    )


def _finish_chunk(reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            finish_reason=reason,
            delta=SimpleNamespace(content="", tool_calls=None),
        )],
    )


# ===========================================================================
# 假件：builder（duck-typing DraftManager；全部方法离线、无网络）
# ===========================================================================
class FakeBuilder:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.draft_id = "test-draft"
        self.draft_message_id = 0
        self.blocks: list = []
        self.block_types: list = []
        self._tool_groups: list = []
        # 渲染确认游标（阶段2裁剪基准）：模拟"用户已看到的字符数"，
        # 测试里手工推进到任意值。
        self._render_cursor_box: list = [0]

    @property
    def render_cursor_box(self) -> list:
        return self._render_cursor_box

    # switch_stream 状态机触及的同步方法
    def end_stream(self):  # noqa: D401
        pass

    def begin_stream_reasoning(self):
        pass

    def begin_stream_text(self):
        pass

    def on_stream_block_closed(self, ended):
        pass

    def append_stream_delta(self, text):
        pass

    def append_to_current_tool_group_text(self, text):
        pass

    def request_flush(self, force: bool = False):
        pass

    def finalize_reasoning_block(self):
        pass

    def on_round_boundary(self):
        pass

    def finish_group(self, idx):
        pass

    def add_tool_item(self, *args, **kwargs):
        pass

    def attach_stream_tool_identity(self, *args, **kwargs):
        pass

    def update_tool_args(self, *args, **kwargs):
        pass

    def replace_trailing_text(self, raw, new):
        return True

    def add_text(self, text):
        pass

    def on_tool_batch_end(self):
        pass

    def begin_tool_batch(self):
        return -1

    def finish_tool_batch(self, idx):
        pass

    def update_tool_item(self, *args, **kwargs):
        pass

    def update_tool_preview(self, *args, **kwargs):
        pass

    async def finalize_turn(self):
        return True


async def _noop_async(*args, **kwargs):
    return None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """隔离网络副作用 + 用例后清理全局注册表。"""
    monkeypatch.setattr(
        "ai.agentic_loops.start_chat_action", _noop_async, raising=True)
    monkeypatch.setattr(
        "ai.agentic_loops.stop_chat_action", _noop_async, raising=True)
    used: list[int] = []

    def _register(chat_id: int) -> int:
        used.append(chat_id)
        return chat_id

    yield _register
    for chat_id in used:
        turn_recovery._inflight.pop(chat_id, None)
        from state import user_contexts
        user_contexts.pop(chat_id, None)


# ===========================================================================
# 组件单元：LiveAssistantSlot / _normalize_journal / MediaProgressSlot
# ===========================================================================
def test_live_assistant_slot_sync_and_finalize():
    """sync 随增量原地同步；finalize 升级占位并双列表共享同一对象。"""
    journal: list = []
    loop_messages: list = []
    slot = LiveAssistantSlot(journal)
    assert len(journal) == 1
    assert journal[0].role == "assistant" and journal[0].text() == ""

    slot.sync("数到 1", "")
    slot.sync("数到 12", "先想")
    # journal 持有的是同一 Message 引用：原地更新即时可见。
    assert journal[0].text() == "数到 12"
    assert journal[0].reasoning() == "先想"

    msg = slot.finalize(loop_messages, "数到 123", [], "先想")
    assert journal[0] is msg and loop_messages[0] is msg
    assert len(journal) == 1, "finalize 是升级占位而非新增消息"
    assert msg.text() == "数到 123"
    wire = msg.to_openai_dict()
    assert wire["content"] == "数到 123" and "tool_calls" not in wire


def test_live_assistant_slot_finalize_with_tool_calls():
    """finalize 携带 tool_calls 时升级出结构化 ToolCallBlock。"""
    journal: list = []
    slot = LiveAssistantSlot(journal)
    slot.sync("先查天气", "")
    msg = slot.finalize(
        [], "先查天气",
        [{"id": "call_1", "type": "function",
          "function": {"name": "web_search", "arguments": '{"query": "北京天气"}'}}],
        "",
    )
    calls = msg.tool_calls()
    assert len(calls) == 1
    assert calls[0].id == "call_1" and calls[0].name == "web_search"
    assert calls[0].arguments == {"query": "北京天气"}


def test_normalize_journal_drops_empty_assistant_placeholder():
    """模型一字未吐即被打断：空占位不得进历史（Codex #22602 防御）。"""
    journal: list = []
    LiveAssistantSlot(journal)  # 空 slot（从未 sync 到内容）
    normalized = _normalize_journal(journal)
    assert len(normalized) == 0


def test_normalize_journal_drops_reasoning_only_placeholder():
    """只吐了思考、没吐正文的半成品同样剔除（content=null 形状防御）。"""
    journal: list = []
    slot = LiveAssistantSlot(journal)
    slot.sync("", "思考到一半被打断")
    normalized = _normalize_journal(journal)
    assert len(normalized) == 0


def test_normalize_journal_keeps_text_and_pairs_unpaired_tool_calls():
    """有文本保留；未配对 tool_call 由占位 tool_result 补齐（既有机制）。"""
    journal: list = []
    slot = LiveAssistantSlot(journal)
    slot.finalize(
        [], "我先查一下天气。",
        [{"id": "call_9", "type": "function",
          "function": {"name": "web_search", "arguments": '{"query": "x"}'}}],
        "",
    )
    normalized = _normalize_journal(journal)
    assert len(normalized) == 2
    assert normalized[0].text() == "我先查一下天气。"
    tr = normalized[1].tool_result_block()
    assert tr is not None
    assert tr.tool_call_id == "call_9"
    assert tr.content == INTERRUPTED_TOOL_PLACEHOLDER


def test_media_progress_slot_lifecycle():
    """媒体占位：成功定稿 / 失败移除 / drop 幂等 / journal=None 容忍。"""
    journal: list = []
    slot = MediaProgressSlot(journal, "[图片生成中] 指令: 画一只猫")
    assert journal and journal[0].text() == "[图片生成中] 指令: 画一只猫"

    entries = slot.complete("[图片已生成] 指令: 画一只猫 | 1024x1024")
    assert entries == [journal[0]]
    assert journal[0].text() == "[图片已生成] 指令: 画一只猫 | 1024x1024"

    slot.drop()
    assert len(journal) == 0
    slot.drop()  # 幂等

    slot_none = MediaProgressSlot(None, "[视频生成中]")
    assert slot_none.complete("[视频已生成]")[0].text() == "[视频已生成]"


# ===========================================================================
# 组件单元：草稿层↔历史层反向校验（改动点3）与合并防御（改动点5）
# ===========================================================================
def test_draft_journal_consistency_warning_and_ok(caplog):
    """草稿有内容而 journal 无 assistant 进度 → WARNING；一致 → INFO。"""
    with caplog.at_level("WARNING", logger="ai.rich_message_builder"):
        _log_draft_journal_consistency(1, "d1", "数到 123" * 20, [])
        assert "失同步" in caplog.text
    caplog.clear()
    with caplog.at_level("INFO", logger="ai.rich_message_builder"):
        _log_draft_journal_consistency(1, "d1", "数到 123" * 20,
                                       [Message.assistant_text("数到 123")])
        assert "两层一致" in caplog.text


def test_draft_journal_consistency_ignores_tool_only_journal(caplog):
    """journal 有 assistant 工具调用（无正文）不算失同步（工具卡片场景）。"""
    with caplog.at_level("WARNING", logger="ai.rich_message_builder"):
        journal = [Message.assistant_with_tool_calls(
            "", [{"id": "c1", "type": "function",
                  "function": {"name": "web_search", "arguments": "{}"}}], "")]
        _log_draft_journal_consistency(1, "d1", "工具卡片摘要文本" * 20, journal)
        assert "失同步" not in caplog.text


@pytest.mark.asyncio
async def test_persist_user_message_entry_flags_residual_inflight(caplog):
    """改动点5：合并分支触发时注册表仍有带进度旧登记 → ERROR 落日志。"""
    chat_id = _next_chat_id()
    history = get_or_init_context(chat_id).setdefault("conversation_history", [])
    history.append(Message.user_text("第一条"))

    # 人为破坏前提：塞入一条带非空 journal 的旧登记（模拟保全时序 bug）。
    turn_recovery._inflight[chat_id] = [turn_recovery._InFlightEntry(
        chat_id=chat_id, journal=[Message.assistant_text("旧轮次进度")],
        task=None, event_source="USER")]

    with caplog.at_level("ERROR", logger="turn_recovery"):
        await persist_user_message_entry(
            chat_id, {"role": "user", "content": "第二条"})
        assert "合并前提" in caplog.text

    # 前提正常时（无残留登记）不误报。
    caplog.clear()
    turn_recovery._inflight.pop(chat_id, None)
    history.append(Message.assistant_text("已有输出"))
    with caplog.at_level("ERROR", logger="turn_recovery"):
        await persist_user_message_entry(
            chat_id, {"role": "user", "content": "第三条"})
        assert "合并前提" not in caplog.text


# ===========================================================================
# 集成回归：真实取消 openai_compat 流式循环（问题文档验证场景 1-5）
# ===========================================================================
async def _run_turn_and_interrupt(
    chat_id: int,
    journal: list,
    client: FakeClient,
    builder: FakeBuilder,
    hang_event: Optional[asyncio.Event],
    rendered_chars: Optional[int] = None,
):
    """把轮次跑成 task，在指定挂起点取消，随后走打断保全。

    ``rendered_chars``：模拟打断时刻用户已看到的流式文本字符数（渲染
    确认游标）。None = 不 attach 游标（等价静默回合/理想零滞后信道，
    打断保全不裁剪文本——既有五场景测试的语义）；int 值 = 按真实链路
    attach，trim 会把直播文本物理截断到该边界。
    """

    async def turn():
        await register_inflight_turn(chat_id, journal, event_source="USER")
        if rendered_chars is not None:
            attach_render_cursor(chat_id, journal, builder.render_cursor_box)
        return await _agentic_loop_openai_compat(
            client, TEST_MODEL, [Message.user_text("请从 1 数到 100")],
            "test", builder, tools=[], journal=journal,
        )

    task = asyncio.create_task(turn())
    if hang_event is not None:
        await hang_event.wait()
    else:
        await asyncio.sleep(0)  # 至少让 task 启动一拍
    if rendered_chars is not None:
        builder._render_cursor_box[0] = rendered_chars
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return await finalize_interrupted_turn(chat_id, reason="user-interrupt")


@pytest.mark.asyncio
async def test_interrupt_mid_text_stream_preserves_progress(_isolate):
    """场景1：纯文本流式中途打断 → 已产出文本保全进历史。

    修复前：journal 空 → 保全 0 条，历史只有 user 消息，模型下一轮
    完全不知道自己说过什么（"草稿显示数到 123，模型说还没开始数"）。
    """
    chat_id = _isolate(_next_chat_id())
    history = get_or_init_context(chat_id).setdefault("conversation_history", [])
    await persist_user_message_entry(chat_id, {"role": "user", "content": "数到100"})
    assert len(history) == 1 and history[-1].role == "user"

    stream = FakeStream(
        [_text_chunk("1, "), _text_chunk("2, "), _text_chunk("3, ")],
        hang=True,
    )
    client = FakeClient(lambda i: stream)
    journal: list = []

    salvaged = await _run_turn_and_interrupt(
        chat_id, journal, client, FakeBuilder(chat_id), stream.hanging)

    assert salvaged > 0, "打断保全必须沉淀出消息（修复前为 0）"
    assert len(history) == 2
    preserved = history[-1]
    assert preserved.role == "assistant"
    assert preserved.text() == "1, 2, 3, "
    # 出站形状合法：文本存在，content 为字符串（非 None）。
    wire = preserved.to_openai_dict()
    assert wire["content"] == "1, 2, 3, " and "tool_calls" not in wire


@pytest.mark.asyncio
async def test_interrupt_with_reasoning_stream_drops_reasoning(_isolate):
    """阶段1：思考流 + 文本流交错时打断 → 残缺思考全量丢弃、文本保全。

    规范：未完成的逻辑推导前提已失效，残缺思考会污染模型下一轮的隐
    空间——ReasoningBlock 不进历史；文本按无游标基准处理（本测试未
    推进游标，等价于静默语义）→ 后端全量保全。
    """
    chat_id = _isolate(_next_chat_id())
    get_or_init_context(chat_id).setdefault("conversation_history", [])

    stream = FakeStream(
        [_reasoning_chunk("用户要数数，"),
         _text_chunk("好的，我开始数："),
         _reasoning_chunk("继续。"),
         _text_chunk("1, 2, ")],
        hang=True,
    )
    client = FakeClient(lambda i: stream)
    journal: list = []

    await _run_turn_and_interrupt(
        chat_id, journal, client, FakeBuilder(chat_id), stream.hanging)

    history = get_or_init_context(chat_id)["conversation_history"]
    preserved = history[-1]
    assert preserved.role == "assistant"
    assert preserved.text() == "好的，我开始数：1, 2, "
    # 阶段1：残缺思考全量丢弃——不进历史、不进出站请求体。
    assert preserved.reasoning() == ""
    wire = preserved.to_openai_dict()
    assert "reasoning_content" not in wire


@pytest.mark.asyncio
async def test_interrupt_mid_tool_args_no_dangling_tool_use(_isolate):
    """场景2：工具参数 JSON 还没收完就打断 → 无悬空半截 tool_use。

    官方原则："Tool use ... cannot be partially recovered"——半截参数
    整体丢弃，只保留已同步文本。
    """
    chat_id = _isolate(_next_chat_id())
    get_or_init_context(chat_id).setdefault("conversation_history", [])

    stream = FakeStream(
        [_text_chunk("我先查一下天气。"),
         _tool_call_chunk(0, "call_w1", "web_search", '{"que'),   # 参数截断
         _tool_call_chunk(0, "", "", 'ry": "北'),               # 后续增量（仍不完整）
         ],
        hang=True,
    )
    client = FakeClient(lambda i: stream)
    journal: list = []

    await _run_turn_and_interrupt(
        chat_id, journal, client, FakeBuilder(chat_id), stream.hanging)

    history = get_or_init_context(chat_id)["conversation_history"]
    preserved = history[-1]
    assert preserved.role == "assistant"
    assert preserved.text() == "我先查一下天气。"
    # 半截 tool_call 整体不落历史：无 tool_use，也无需配对占位。
    assert preserved.tool_calls() == []
    assert len(history) == 1


@pytest.mark.asyncio
async def test_interrupt_during_tool_execution_pairs_placeholder(
        _isolate, monkeypatch):
    """场景3：工具已完整发起、等待执行结果时打断 → 配对占位齐全。"""
    chat_id = _isolate(_next_chat_id())
    get_or_init_context(chat_id).setdefault("conversation_history", [])

    stream = FakeStream(
        [_text_chunk("我来查天气。"),
         _tool_call_chunk(0, "call_w2", "web_search",
                          '{"query": "北京天气"}'),
         _finish_chunk("tool_calls"),
         ],
        hang=False,
    )
    client = FakeClient(lambda i: stream)
    journal: list = []

    entered = asyncio.Event()

    async def fake_run_tool_batch(builder, tool_calls_list, loop_messages,
                                  new_history_entries, tool_call_count_ref,
                                  api_label, tools, error_streak=None):
        # 模拟工具正在执行（assistant 消息应已进 journal）。
        assert any(m.role == "assistant" for m in new_history_entries), (
            "执行工具前 assistant(tool_calls) 必须已在 journal（改动点1）")
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("ai.agentic_loops.run_tool_batch", fake_run_tool_batch)

    await _run_turn_and_interrupt(
        chat_id, journal, client, FakeBuilder(chat_id), entered)

    history = get_or_init_context(chat_id)["conversation_history"]
    assert len(history) == 2
    assistant_msg, tool_msg = history
    assert assistant_msg.role == "assistant"
    assert assistant_msg.text() == "我来查天气。"
    calls = assistant_msg.tool_calls()
    assert len(calls) == 1 and calls[0].id == "call_w2"
    assert calls[0].arguments == {"query": "北京天气"}
    tr = tool_msg.tool_result_block()
    assert tool_msg.role == "tool" and tr is not None
    assert tr.tool_call_id == "call_w2"
    # 阶段4a：只读工具的占位回执带 aborted（已中止）状态语义——
    # 调用声明与回执绝对成对闭合，模型不会误读为"执行成功"。
    assert tr.content == INTERRUPTED_TOOL_PLACEHOLDER
    assert "aborted" in tr.content and "中止" in tr.content


@pytest.mark.asyncio
async def test_interrupt_third_round_keeps_first_two(_isolate, monkeypatch):
    """场景4：三轮工具调用，末轮文本中途打断 → 前两轮完整保留。"""
    chat_id = _isolate(_next_chat_id())
    get_or_init_context(chat_id).setdefault("conversation_history", [])

    stream3 = FakeStream(
        [_text_chunk("最后总结：前两步是 1、2，")], hang=True)
    streams = [
        FakeStream([
            _text_chunk("先搜索第一步。"),
            _tool_call_chunk(0, "call_r1", "web_search", '{"query": "a"}'),
            _finish_chunk("tool_calls"),
        ]),
        FakeStream([
            _text_chunk("再搜索第二步。"),
            _tool_call_chunk(0, "call_r2", "web_search", '{"query": "b"}'),
            _finish_chunk("tool_calls"),
        ]),
        stream3,
    ]
    client = FakeClient(lambda i: streams[i])
    journal: list = []

    async def fake_run_tool_batch(builder, tool_calls_list, loop_messages,
                                  new_history_entries, tool_call_count_ref,
                                  api_label, tools, error_streak=None):
        for tc in tool_calls_list:
            result = Message.tool_result(
                tc["id"], tc["function"]["name"],
                f"result-of-{tc['function']['name']}")
            loop_messages.append(result)
            new_history_entries.append(result)
        return "continue"

    monkeypatch.setattr("ai.agentic_loops.run_tool_batch", fake_run_tool_batch)

    await _run_turn_and_interrupt(
        chat_id, journal, client, FakeBuilder(chat_id), stream3.hanging)

    history = get_or_init_context(chat_id)["conversation_history"]
    # r1 assistant + r1 tool_result + r2 assistant + r2 tool_result + r3 文本
    assert len(history) == 5
    assert history[0].text() == "先搜索第一步。"
    assert history[1].tool_result_block().tool_call_id == "call_r1"
    assert history[2].text() == "再搜索第二步。"
    assert history[3].tool_result_block().tool_call_id == "call_r2"
    assert history[4].text() == "最后总结：前两步是 1、2，"
    # 前两轮的配对完整（无需补占位）。
    paired = {m.tool_result_block().tool_call_id for m in history
              if m.role == "tool"}
    assert paired == {"call_r1", "call_r2"}


@pytest.mark.asyncio
async def test_new_message_after_interrupt_continues_from_progress(_isolate):
    """场景5：打断后立刻发新消息 → 进度在历史中、新消息正常追加衔接。"""
    chat_id = _isolate(_next_chat_id())
    history = get_or_init_context(chat_id).setdefault("conversation_history", [])
    await persist_user_message_entry(chat_id, {"role": "user", "content": "数到100"})

    stream = FakeStream(
        [_text_chunk("1, "), _text_chunk("2, "), _text_chunk("3, ")],
        hang=True,
    )
    client = FakeClient(lambda i: stream)
    journal: list = []
    await _run_turn_and_interrupt(
        chat_id, journal, client, FakeBuilder(chat_id), stream.hanging)

    # 打断后新消息：历史末尾已是 assistant → 追加（不再与 user 合并）。
    env = {"role": "user", "content": "继续，从 4 开始"}
    await persist_user_message_entry(chat_id, env)
    assert env[turn_recovery.EARLY_PERSIST_MODE] == "appended"
    assert len(history) == 3
    assert [m.role for m in history] == ["user", "assistant", "user"]

    # 下一轮请求（渲染出站）必然包含被打断轮次的进度文本。
    wire = render_openai_messages(history)
    assert wire[1]["role"] == "assistant"
    assert wire[1]["content"] == "1, 2, 3, "
    assert wire[2]["content"] == "继续，从 4 开始"


@pytest.mark.asyncio
async def test_normal_completion_single_assistant_message(_isolate):
    """正常路径回归：流跑完 → journal 恰一条完整 assistant（无重复占位）。"""
    chat_id = _isolate(_next_chat_id())
    get_or_init_context(chat_id).setdefault("conversation_history", [])

    stream = FakeStream(
        [_text_chunk("1, 2, 3, 4, 5, 6, 7, 8, 9, 10."),
         _finish_chunk("stop")],
        hang=False,
    )
    client = FakeClient(lambda i: stream)
    journal: list = []
    builder = FakeBuilder(chat_id)

    async def turn():
        return await _agentic_loop_openai_compat(
            client, TEST_MODEL, [Message.user_text("数到10")],
            "test", builder, tools=[], journal=journal,
        )

    final_content, usage, new_entries = await asyncio.create_task(turn())
    assert final_content == "1, 2, 3, 4, 5, 6, 7, 8, 9, 10."
    assert len(new_entries) == 1, "占位升级而非新增：恰一条 assistant"
    assert new_entries[0] is journal[0]
    assert new_entries[0].text() == final_content


# ===========================================================================
# 五阶段打断规范（2026-09 二期）：阶段1/2/4 专用测试
# ===========================================================================
def test_live_slot_flag_lifecycle():
    """LIVE_STREAM_FLAG：流式期间携带（裁剪判据），finalize 后摘除。"""
    journal: list = []
    slot = LiveAssistantSlot(journal)
    assert journal[0].meta.get(LIVE_STREAM_FLAG) is True

    slot.sync("正文", "思考")
    assert journal[0].meta.get(LIVE_STREAM_FLAG) is True  # 直播中仍携带

    slot.finalize([], "正文", [], "思考")
    # 定稿：标记摘除——打断裁剪从此不触碰本条（思考/文本已完整）。
    assert journal[0].meta.get(LIVE_STREAM_FLAG) is None
    assert journal[0].reasoning() == "思考"


def test_media_progress_slot_synthetic_flag():
    """媒体进度占位带 SYNTHETIC 标记：不参与裁剪的前文合计。"""
    journal: list = []
    slot = MediaProgressSlot(journal, "[图片生成中] 指令: 画一只猫")
    assert journal[0].meta.get(SYNTHETIC_ASSISTANT_FLAG) is True
    assert journal[0].text() == "[图片生成中] 指令: 画一只猫"


def test_trim_truncates_to_render_cursor():
    """阶段2核心：直播文本按前端渲染游标物理截断。

    后端已生成 500 字、用户只渲染了 100 字 → 历史只留 100 字
    （"用户没看到的等于模型没说过"）。残缺思考同时丢弃（阶段1）。
    """
    journal: list = []
    # 前一轮已定稿的 assistant（50 字流式文本，已计入游标）。
    prev = Message.assistant_text("前一轮的完整回答，占五十字符。" * 2)
    journal.append(prev)
    # 当前直播占位：500 字文本 + 残缺思考。
    slot = LiveAssistantSlot(journal)
    slot.sync("数" * 500, "我打算先数到 100，然后……")

    rendered = len("前一轮的完整回答，占五十字符。" * 2) + 100
    assert trim_interrupted_stream(journal, rendered) is True

    msg = journal[-1]
    assert msg.text() == "数" * 100
    assert msg.reasoning() == ""
    assert msg.meta.get(LIVE_STREAM_FLAG) is None  # 裁剪后标记摘除
    # 前一轮定稿消息不受影响。
    assert journal[0].text() == prev.text()


def test_trim_no_cursor_keeps_text_drops_reasoning():
    """无渲染基准（静默回合）：文本后端全量保全，思考仍丢弃。"""
    journal: list = []
    slot = LiveAssistantSlot(journal)
    slot.sync("数" * 500, "半截思考")

    assert trim_interrupted_stream(journal, None) is True
    assert journal[-1].text() == "数" * 500
    assert journal[-1].reasoning() == ""


def test_trim_finalized_message_untouched():
    """末尾是已定稿消息（阶段4/5 打断点）：不剥思考、不裁文本。"""
    journal: list = []
    slot = LiveAssistantSlot(journal)
    # 流正常结束 → 定稿（含完整思考 + tool_calls）。
    slot.finalize([], "先查天气。",
                  [{"id": "c1", "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"}}],
                  "完整的思考过程")

    assert trim_interrupted_stream(journal, 3) is False  # 未触到直播占位
    assert journal[0].text() == "先查天气。"
    assert journal[0].reasoning() == "完整的思考过程"
    assert len(journal[0].tool_calls()) == 1


def test_trim_synthetic_prior_excluded():
    """前文合计排除 synthetic 占位：媒体进度文本不挤压截断预算。"""
    journal: list = []
    synthetic = Message(
        role="assistant", blocks=[TextBlock("[图片生成中] 指令: 画一只猫")],
        meta={SYNTHETIC_ASSISTANT_FLAG: True},
    )
    journal.append(synthetic)
    slot = LiveAssistantSlot(journal)
    slot.sync("这是图片完成后的总结文字。" * 10, "")

    # 用户看到了 synthetic 占位（20 字）+ 流式文本 100 字。
    rendered = 100  # 游标只计流式文本（synthetic 从未进入游标计数）
    trim_interrupted_stream(journal, rendered)
    # 预算 = 渲染游标 100 - 前文合计（synthetic 排除 → 0）= 100 字。
    assert len(journal[-1].text()) == 100


def test_trim_over_trimmed_message_filtered_by_normalize():
    """裁剪到空（游标落后于前文）→ 空消息被 _normalize_journal 剔除。"""
    journal: list = []
    prev = Message.assistant_text("前一轮回答" * 10)
    journal.append(prev)
    slot = LiveAssistantSlot(journal)
    slot.sync("正文" * 50, "")

    # 渲染游标只有 10（用户只看到前一轮的一部分）→ 当前轮预算为负。
    trim_interrupted_stream(journal, 10)
    assert journal[-1].text() == ""
    normalized = _normalize_journal(journal)
    assert len(normalized) == 1  # 空占位被剔除（Codex #22602 防御兜底）
    assert normalized[0] is prev


def test_interrupted_and_detached_placeholders_semantics():
    """阶段4 两种占位回执的语义区分：只读=aborted；写操作=脱离待回填。"""
    assert "aborted" in INTERRUPTED_TOOL_PLACEHOLDER
    assert "中止" in INTERRUPTED_TOOL_PLACEHOLDER
    assert "后台" in DETACHED_TOOL_PLACEHOLDER
    assert "回填" in DETACHED_TOOL_PLACEHOLDER
    assert INTERRUPTED_TOOL_PLACEHOLDER != DETACHED_TOOL_PLACEHOLDER


@pytest.mark.asyncio
async def test_attach_render_cursor_binding(_isolate):
    """attach_render_cursor：按 journal 身份绑定；保全时读取最新值。"""
    chat_id = _isolate(_next_chat_id())
    journal: list = []
    await register_inflight_turn(chat_id, journal, event_source="USER")
    box = [0]
    attach_render_cursor(chat_id, journal, box)

    entry = turn_recovery._inflight[chat_id][0]
    assert entry.render_cursor_ref is box
    # box 原地推进（builder 每帧送达后更新）→ 注册表零拷贝读到最新值。
    box[0] = 321
    assert turn_recovery._read_render_cursor(entry.render_cursor_ref) == 321
    # 静默回合：None = 无渲染基准。
    attach_render_cursor(chat_id, journal, None)
    assert turn_recovery._inflight[chat_id][0].render_cursor_ref is None


@pytest.mark.asyncio
async def test_interrupt_mid_text_trims_to_rendered_cursor(_isolate):
    """阶段2集成：后端生成全量、前端只渲染部分 → 历史按游标截断。

    模拟：后端已流式 7 字符（"1, 2, 3, "），草稿最后一帧只送达了
    3 字符（"1, "）——打断后历史只留 "1, "，模型下一轮不会"记得"
    用户从未看到的 "2, 3, "。
    """
    chat_id = _isolate(_next_chat_id())
    get_or_init_context(chat_id).setdefault("conversation_history", [])

    stream = FakeStream(
        [_text_chunk("1, "), _text_chunk("2, "), _text_chunk("3, ")],
        hang=True,
    )
    client = FakeClient(lambda i: stream)
    journal: list = []
    builder = FakeBuilder(chat_id)

    async def turn():
        await register_inflight_turn(chat_id, journal, event_source="USER")
        attach_render_cursor(chat_id, journal, builder.render_cursor_box)
        return await _agentic_loop_openai_compat(
            client, TEST_MODEL, [Message.user_text("数到100")],
            "test", builder, tools=[], journal=journal,
        )

    task = asyncio.create_task(turn())
    await stream.hanging.wait()
    builder._render_cursor_box[0] = 3  # 用户只看到了 "1, "（3 字符）
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finalize_interrupted_turn(chat_id, reason="user-interrupt")

    history = get_or_init_context(chat_id)["conversation_history"]
    preserved = history[-1]
    assert preserved.role == "assistant"
    assert preserved.text() == "1, "  # 物理截断到渲染游标


@pytest.mark.asyncio
async def test_writeback_detached_tool_result_replaces_placeholder(_isolate):
    """阶段4b：脱离工具终态到达 → 历史占位原地替换为真实终态。"""
    chat_id = _isolate(_next_chat_id())
    history = get_or_init_context(chat_id).setdefault("conversation_history", [])
    # 历史已有：assistant(声明) + tool(DETACHED 占位)——打断保全后的形态。
    history.append(Message.assistant_with_tool_calls(
        "正在执行转账。",
        [{"id": "call_w", "type": "function",
          "function": {"name": "bash", "arguments": '{"command": "transfer"}'}}],
        "",
    ))
    history.append(Message.tool_result("call_w", "bash", DETACHED_TOOL_PLACEHOLDER))

    replaced = await writeback_detached_tool_result(
        chat_id, "call_w", "bash", "转账成功：100.00 元已到账，流水号 TX-42")
    assert replaced is True
    tr = history[-1].tool_result_block()
    assert tr.content == "转账成功：100.00 元已到账，流水号 TX-42"
    # 配对结构不变：消息条数与 tool_call_id 不变。
    assert len(history) == 2 and tr.tool_call_id == "call_w"


@pytest.mark.asyncio
async def test_writeback_detached_tool_result_waits_for_history(_isolate, monkeypatch):
    """阶段4b：终态先于历史写入到达 → 轮询等待占位出现后替换。"""
    chat_id = _isolate(_next_chat_id())
    history = get_or_init_context(chat_id).setdefault("conversation_history", [])
    monkeypatch.setattr(turn_recovery, "_DETACHED_WRITEBACK_POLL", 0.05)

    writeback_task = asyncio.create_task(writeback_detached_tool_result(
        chat_id, "call_x", "text_editor", "文件已写入：report.md（3.2 KB）"))
    await asyncio.sleep(0.12)  # 轮询等待中……
    # 此刻占位才被写入历史（打断方保全完成）。
    history.append(Message.tool_result("call_x", "text_editor", DETACHED_TOOL_PLACEHOLDER))
    assert await asyncio.wait_for(writeback_task, timeout=2) is True
    assert history[-1].tool_result_block().content == "文件已写入：report.md（3.2 KB）"


@pytest.mark.asyncio
async def test_interrupt_write_tool_detached_and_final_state_writeback(
        _isolate, monkeypatch):
    """阶段4b 端到端：写操作执行中打断 → 脱离后台死等 → 终态回写历史。

    链路：run_one 对 bash（写操作）走 shield——外层取消只取消"等待"，
    执行脱离主进程继续；取消路径回填 DETACHED 占位（非 aborted 占位）；
    后台任务拿到真实终态后原地替换历史中的占位。
    """
    chat_id = _isolate(_next_chat_id())
    get_or_init_context(chat_id).setdefault("conversation_history", [])
    monkeypatch.setattr(turn_recovery, "_DETACHED_WRITEBACK_POLL", 0.05)

    entered = asyncio.Event()
    inner_completed = asyncio.Event()

    async def fake_write_dispatch(fn_name, fn_args, chat_id=None, progress_callback=None):
        # 模拟写库/转账：被打断后仍需要 0.15s 才拿到确切终态。
        entered.set()
        await asyncio.sleep(0.15)
        inner_completed.set()
        return "写库成功：已写入 42 行，事务提交完成"

    monkeypatch.setattr("ai.tool_call_loop.dispatch_tool_call", fake_write_dispatch)

    journal: list = []
    loop_messages: list = []
    # assistant(tool_calls) 已 finalize（阶段4 的前置：流已完整、声明已落 journal）。
    journal.append(Message.assistant_with_tool_calls(
        "我来写入数据。",
        [{"id": "call_wb", "type": "function",
          "function": {"name": "bash", "arguments": '{"command": "db write"}'}}],
        "",
    ))
    await register_inflight_turn(chat_id, journal, event_source="USER")

    from ai.tool_call_loop import _run_tool_calls_and_append
    batch_task = asyncio.create_task(_run_tool_calls_and_append(
        [{"id": "call_wb", "type": "function",
          "function": {"name": "bash", "arguments": '{"command": "db write"}'}}],
        loop_messages, journal, [0], "test", FakeBuilder(chat_id),
        chat_id=chat_id, tools=[],
    ))
    await asyncio.wait_for(entered.wait(), timeout=5)
    batch_task.cancel()  # 用户打断（工具执行中）
    with pytest.raises(asyncio.CancelledError):
        await batch_task

    # 取消路径回填：写操作用 DETACHED 占位（不是 aborted 占位）。
    tool_msgs = [m for m in journal if m.role == "tool"]
    assert len(tool_msgs) == 1
    tr = tool_msgs[0].tool_result_block()
    assert tr.tool_call_id == "call_wb"
    assert tr.content == DETACHED_TOOL_PLACEHOLDER

    # 打断保全 → 历史含 assistant(声明) + tool(DETACHED 占位)。
    await finalize_interrupted_turn(chat_id, reason="user-interrupt")
    history = get_or_init_context(chat_id)["conversation_history"]
    assert len(history) == 2

    # 后台死等拿到确切终态 → 回写历史（原地替换占位）。shield 缺失时
    # dispatch 会随取消直接死亡（事件永不触发）→ wait_for 快速失败，
    # 不挂死 CI。
    await asyncio.wait_for(inner_completed.wait(), timeout=5)
    await asyncio.sleep(0.12)  # 轮询命中
    final_tr = history[-1].tool_result_block()
    assert final_tr.tool_call_id == "call_wb"
    assert final_tr.content == "写库成功：已写入 42 行，事务提交完成"
    # 出站请求体：配对完整、终态真实（数据看后台）。
    wire = render_openai_messages(history)
    assert wire[1]["role"] == "tool"
    assert wire[1]["content"] == "写库成功：已写入 42 行，事务提交完成"


@pytest.mark.asyncio
async def test_interrupt_read_tool_stays_aborted_no_writeback(_isolate, monkeypatch):
    """阶段4a 对照：只读工具执行中打断 → aborted 占位，无后台回写。"""
    chat_id = _isolate(_next_chat_id())
    get_or_init_context(chat_id).setdefault("conversation_history", [])

    entered = asyncio.Event()

    async def fake_read_dispatch(fn_name, fn_args, chat_id=None, progress_callback=None):
        entered.set()
        await asyncio.sleep(3600)  # 挂起的网络请求（打断即被掐断）

    monkeypatch.setattr("ai.tool_call_loop.dispatch_tool_call", fake_read_dispatch)

    journal: list = []
    loop_messages: list = []
    journal.append(Message.assistant_with_tool_calls(
        "我来搜索。",
        [{"id": "call_ro", "type": "function",
          "function": {"name": "web_search", "arguments": '{"query": "x"}'}}],
        "",
    ))
    await register_inflight_turn(chat_id, journal, event_source="USER")

    from ai.tool_call_loop import _run_tool_calls_and_append
    batch_task = asyncio.create_task(_run_tool_calls_and_append(
        [{"id": "call_ro", "type": "function",
          "function": {"name": "web_search", "arguments": '{"query": "x"}'}}],
        loop_messages, journal, [0], "test", FakeBuilder(chat_id),
        chat_id=chat_id, tools=[],
    ))
    await asyncio.wait_for(entered.wait(), timeout=5)
    batch_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await batch_task

    tool_msgs = [m for m in journal if m.role == "tool"]
    assert tool_msgs[0].tool_result_block().content == INTERRUPTED_TOOL_PLACEHOLDER
    # 只读工具无脱离任务注册（沉没成本只有流量，掐断即结束）。
    from ai.tool_call_loop import _DETACHED_TASKS
    assert not _DETACHED_TASKS


# ===========================================================================
# v3 修订（2026-09-13 实测反馈）：以"实际发送到草稿"为截断基准
#
# 实测 bug：打断信号发出后，草稿有时还会再刷新一段文本才停。根因：
#   1. finalize_interrupted_draft 固化时发送的是全量后端缓冲——未送达
#      草稿的积压文本被一次性"倒"给用户（固化消息 > 用户已见）；
#   2. 打断信号传播到 stop_flush_loop 之间存在事件循环窗口，刷新 tick
#      仍可能推出积压帧。
# 修复语义：
#   - 固化消息 == 冻结草稿最后一帧 == 历史记录（三层同一条送达边界）；
#   - 打断入口先行冻结草稿推送（freeze_draft_streaming）；
#   - 游标入账完整（工具组旁白同样计入可见文本）。
# ===========================================================================
from ai.rich_message_builder import (  # noqa: E402
    RichMessageBuilder,
    _FROZEN_DRAFTS,
    _rich_visible_text,
    freeze_draft_streaming,
)
from ai.draft_manager import DraftManager  # noqa: E402


def test_tool_group_narration_counts_into_visible_total():
    """游标入账完整性：工具组旁白（折叠块内可见文字）计入累计可见文本。

    不计数会挤压后续直播占位的截断预算（历史层 budget = 游标 - 前文
    合计，而前文 assistant 文本含旁白）→ 把用户已看到的流式文本多裁掉。
    """
    b = RichMessageBuilder(1)
    assert b._visible_text_chars_total == 0
    b.start_new_tool_group()
    b.append_to_current_tool_group_text("先搜索再总结")
    assert b._visible_text_chars_total == len("先搜索再总结")
    b.append_to_current_tool_group_text("。")
    assert b._visible_text_chars_total == len("先搜索再总结。")


def test_truncate_backlog_text_tail():
    """裁剪主路径：文本积压从尾部精确裁到送达游标。"""
    b = RichMessageBuilder(1)
    b.begin_stream_text()
    b.append_stream_delta("A" * 100)
    b.end_stream()
    b._advance_render_cursor(60)  # 用户只看到前 60 字符（最后一帧送达边界）
    trimmed = b._truncate_unrendered_backlog()
    assert trimmed == 40
    assert b.blocks == ["A" * 60]
    assert b._visible_text_chars_total == 60 == b._render_confirmed_chars


def test_truncate_backlog_spans_tool_group_narration():
    """裁剪跨层：积压横跨"末段文本 + 工具组旁白尾部"时按显示顺序回裁。"""
    b = RichMessageBuilder(1)
    b.begin_stream_text()
    b.append_stream_delta("正文一")
    b.end_stream()
    b.start_new_tool_group()
    b.append_to_current_tool_group_text("工具旁白")
    b.finish_group(0)
    b.begin_stream_text()
    b.append_stream_delta("正文二")
    b.end_stream()
    # total = 3 + 4 + 3 = 10；送达游标 6（正文一 + 旁白前 3 字）
    b._advance_render_cursor(6)
    trimmed = b._truncate_unrendered_backlog()
    assert trimmed == 4  # 尾部：正文二(3) + 旁白尾(1)
    assert b.blocks[0] == "正文一"
    assert b._tool_groups[0]["text_content"] == "工具旁"
    text_all = "".join(
        blk for t, blk in zip(b.block_types, b.blocks) if t == "text"
    )
    assert text_all == "正文一"
    assert b._visible_text_chars_total == 6


def test_truncate_backlog_inflight_stream_buffer():
    """裁剪覆盖在途流式缓冲（打断落在流式中、缓冲未提交时）。"""
    b = RichMessageBuilder(1)
    b.begin_stream_text()
    b.append_stream_delta("已送达前段")
    b._advance_render_cursor(len("已送达前段"))
    b.append_stream_delta("积压尾巴")
    trimmed = b._truncate_unrendered_backlog()
    assert trimmed == len("积压尾巴")
    assert b._stream_buffer == "已送达前段"


def test_truncate_backlog_silent_builder_noop():
    """静默回合无渲染基准（box=None）→ 不裁剪，保全后端全量。"""
    b = RichMessageBuilder(1)
    b.begin_stream_text()
    b.append_stream_delta("静默回合不应被裁")
    b.end_stream()
    b._render_cursor_box = None
    assert b._truncate_unrendered_backlog() == 0
    assert b.blocks == ["静默回合不应被裁"]


def test_truncate_backlog_over_trim_guard(caplog):
    """过度裁剪防御：积压大于可裁文本（口径漂移）→ 整体不裁 + WARNING。"""
    b = RichMessageBuilder(1)
    b.begin_stream_text()
    b.append_stream_delta("A" * 30)
    b.end_stream()
    b._advance_render_cursor(10)
    b._visible_text_chars_total = 100  # 模拟口径漂移（积压 90 > 可裁 30）
    with caplog.at_level("WARNING", logger="ai.rich_message_builder"):
        trimmed = b._truncate_unrendered_backlog()
    assert trimmed == 0
    assert b.blocks == ["A" * 30]  # 绝不裁掉已送达内容
    assert "整体不裁" in caplog.text


def test_truncate_backlog_no_backlog_noop():
    """无积压（全部送达）→ 裁剪为 no-op（正常结束语义不受影响）。"""
    b = RichMessageBuilder(1)
    b.begin_stream_text()
    b.append_stream_delta("全部送达")
    b.end_stream()
    b._advance_render_cursor(len("全部送达"))
    assert b._truncate_unrendered_backlog() == 0
    assert b.blocks == ["全部送达"]


@pytest.mark.asyncio
async def test_freeze_draft_streaming_gates_flush_and_clears_on_stop(monkeypatch):
    """冻结门：打断入口冻结后 flush 不再推送任何新帧；stop 时清理登记。"""
    b = RichMessageBuilder(1)
    sent: list[str] = []

    async def fake_draft_send(chat_id, draft_id, html, force=False):
        sent.append(html)
        return 0

    monkeypatch.setattr(
        "ai.rich_message_builder.send_rich_message_draft", fake_draft_send)

    b.begin_stream_text()
    b.append_stream_delta("hello")

    freeze_draft_streaming(b.draft_id)
    assert b.draft_id in _FROZEN_DRAFTS
    await b.flush(force=True)
    assert sent == []  # 冻结门：不推送任何新帧

    await b.stop_flush_loop()  # 推流生命周期终点 → 清理冻结登记
    assert b.draft_id not in _FROZEN_DRAFTS
    await b.flush(force=True)
    assert sent and "hello" in sent[0]  # 解冻后恢复推送


@pytest.mark.asyncio
async def test_stop_flush_drains_inflight_delivery_before_interrupt_finalize(monkeypatch):
    """已发往 Telegram、但尚未返回的帧不能因打断被漏记到渲染游标。

    这是线上“草稿已见 400 字、固定消息少几十字”的回归：取消发生在
    HTTP 请求飞行中时，stop_flush_loop 必须排空该请求，而非取消它。
    """
    b = RichMessageBuilder(1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_draft_send(chat_id, draft_id, html, force=False):
        started.set()
        await release.wait()
        return 123

    monkeypatch.setattr(
        "ai.rich_message_builder.send_rich_message_draft", delayed_draft_send)

    b.begin_stream_text()
    b.append_stream_delta("Telegram 已接收的尾段")
    b.end_stream()
    inflight = asyncio.create_task(b.flush(force=True))
    await started.wait()

    freeze_draft_streaming(b.draft_id)
    stopping = asyncio.create_task(b.stop_flush_loop())
    await asyncio.sleep(0)
    assert not inflight.done()  # 修复前：stop_flush_loop 会取消这条请求。

    release.set()
    await stopping
    await inflight
    assert b._render_confirmed_chars == len("Telegram 已接收的尾段")

    # 固化不再把客户端已经看到的尾段裁掉。
    assert b._truncate_unrendered_backlog() == 0
    assert b.blocks == ["Telegram 已接收的尾段"]


@pytest.mark.asyncio
async def test_freeze_draft_streaming_ignores_none():
    """freeze(None)（无活跃草稿）为安全 no-op。"""
    freeze_draft_streaming(None)
    assert all(v is not None for v in _FROZEN_DRAFTS)


@pytest.mark.asyncio
async def test_finalize_interrupted_draft_clamps_to_delivered_cursor(monkeypatch):
    """打断固化 = 用户实际所见：固化消息不含未送达草稿的积压文本。"""
    b = RichMessageBuilder(1)
    sent: list[str] = []

    async def fake_send(chat_id, html, reassert_draft=True):
        sent.append(html)
        return True

    async def fake_dead(draft_id):
        return False

    async def fake_delete(chat_id, msg_id):
        return True

    monkeypatch.setattr("ai.rich_message_builder.send_rich_html_message", fake_send)
    monkeypatch.setattr("ai.rich_message_builder.is_draft_dead", fake_dead)
    monkeypatch.setattr("ai.rich_message_builder.delete_message_fast", fake_delete)

    b.begin_stream_text()
    b.append_stream_delta("用户看到的部分" + "用户没看到的积压")
    b.end_stream()
    b._advance_render_cursor(len("用户看到的部分"))

    ok = await b.finalize_interrupted_draft(journal=None)
    assert ok
    assert len(sent) == 1
    visible = _rich_visible_text(sent[0])
    assert "用户看到的部分" in visible
    assert "积压" not in visible  # 修复前：全量缓冲被"倒"给用户


@pytest.mark.asyncio
async def test_interrupt_three_layers_align_on_delivered_cursor(
        _isolate, monkeypatch):
    """三层同基准集成：固化消息 == 冻结草稿边界 == 历史记录（同一游标）。

    真实 DraftManager + RichMessageBuilder 走 openai_compat 流式循环，
    打断于流式中途（用户只见 "1, "）：固化消息与历史记录都必须是
    "1, "，且打断后不再有任何草稿帧推送（冻结门）。
    """
    chat_id = _isolate(_next_chat_id())
    history = get_or_init_context(chat_id).setdefault("conversation_history", [])
    await persist_user_message_entry(chat_id, {"role": "user", "content": "数到100"})

    frames: list[str] = []

    async def fake_draft_send(chat_id_, draft_id_, html, force=False):
        raise RuntimeError("offline: draft channel disabled")  # 不推进游标

    async def fake_send(chat_id_, html, reassert_draft=True):
        frames.append(html)
        return True

    async def fake_dead(draft_id_):
        return False

    async def fake_delete(chat_id_, msg_id_):
        return True

    monkeypatch.setattr(
        "ai.rich_message_builder.send_rich_message_draft", fake_draft_send)
    monkeypatch.setattr("ai.rich_message_builder.send_rich_html_message", fake_send)
    monkeypatch.setattr("ai.rich_message_builder.is_draft_dead", fake_dead)
    monkeypatch.setattr("ai.rich_message_builder.delete_message_fast", fake_delete)

    builder = DraftManager(RichMessageBuilder(chat_id))
    stream = FakeStream(
        [_text_chunk("1, "), _text_chunk("2, "), _text_chunk("3, ")],
        hang=True,
    )
    client = FakeClient(lambda i: stream)
    journal: list = []

    async def turn():
        await register_inflight_turn(chat_id, journal, event_source="USER")
        attach_render_cursor(chat_id, journal, builder.render_cursor_box)
        return await _agentic_loop_openai_compat(
            client, TEST_MODEL, [Message.user_text("请从 1 数到 100")],
            "test", builder, tools=[], journal=journal,
        )

    task = asyncio.create_task(turn())
    await stream.hanging.wait()
    # 打断时刻：用户只看到 "1, "（3 字符）——最后一帧成功送达的边界。
    builder._advance_render_cursor(3)
    # 打断入口先行冻结（真实顺序：冻结 → 取消 → 收尾固化 → 历史保全）。
    freeze_draft_streaming(builder.draft_id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await builder.stop_flush_loop()

    # 可见层：固化消息 = 用户实际所见，绝不含积压（"2, 3" 不得出现）。
    ok = await builder.finalize_interrupted_draft(journal=journal)
    assert ok
    assert len(frames) == 1
    assert "2, 3" not in frames[0]
    visible = _rich_visible_text(frames[0]).strip()

    # 历史层：同一游标裁剪（trim 预算 = 游标 3 - 前文 0）。
    await finalize_interrupted_turn(chat_id, reason="user-interrupt")
    preserved = history[-1]
    assert preserved.role == "assistant"

    # 三层同基准：固化消息 == 历史记录 == "1, "。
    assert visible == "1,"
    assert preserved.text() == "1, "
    assert visible.rstrip(", ") == preserved.text().rstrip(", ")

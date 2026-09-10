# =====================================================================
# tests/unit/test_draft_manager.py — Agent Stream 与 Draft 显示层解耦验收
# =====================================================================
# 对应改造需求文档的三个验收场景 + UI Event Buffer + 双状态机：
#   Test 1：超长 reasoning → Draft1 完整思考，Draft2 正文（场景一）
#   Test 2：超长 content  → Draft1 完整 markdown，Draft2 剩余内容（场景二）
#   Test 3：tool call + result + 下一轮回答 → tool result 立即进入上下文、
#           Agent 不等待 UI；UI 侧工具组完整落在同一草稿（场景三）
#   另覆盖：滚动期事件缓冲与按序回放（§9）、终局收束、容量未满不滚动、
#           Agent/Draft 状态机独立演化（§10）。
# =====================================================================
import asyncio
from typing import List

import pytest

from ai import rich_message_builder as rmb
from ai.draft_manager import (
    AgentEvent,
    DraftEventBuffer,
    DraftManager,
    DraftPhase,
    AgentPhase,
    EventTypes,
)

CHAT_ID = 424242


# ---------------------------------------------------------------------
# 测试基础设施：拦截全部 Telegram 网络出口
# ---------------------------------------------------------------------
class _Sends:
    """记录草稿帧 / 永久消息 / 清理动作的假传输层。"""

    def __init__(self) -> None:
        self.drafts: List[tuple] = []
        self.permanent: List[str] = []
        self.dead: List[int] = []
        self.deleted: List[int] = []

    @property
    def permanent_count(self) -> int:
        return len(self.permanent)


@pytest.fixture()
def env(monkeypatch):
    """构建 RichMessageBuilder + DraftManager，并把所有网络出口换成记录器。"""
    sends = _Sends()

    async def fake_send_draft(chat_id, draft_id, html, force=False):
        sends.drafts.append((draft_id, html))
        return 90000 + len(sends.drafts)

    async def fake_send_permanent(chat_id, html, reassert_draft=True, **kwargs):
        sends.permanent.append(html)
        return 80000 + len(sends.permanent)

    async def fake_mark_dead(draft_id):
        sends.dead.append(draft_id)
        return True

    async def fake_delete_fast(chat_id, message_id):
        sends.deleted.append(message_id)
        return True

    async def fake_set_active_draft(chat_id, draft_id, message_id=0):
        return True

    monkeypatch.setattr(rmb, "send_rich_message_draft", fake_send_draft)
    monkeypatch.setattr(rmb, "send_rich_html_message", fake_send_permanent)
    monkeypatch.setattr(rmb, "mark_draft_dead", fake_mark_dead)
    monkeypatch.setattr(rmb, "delete_message_fast", fake_delete_fast)
    import state
    monkeypatch.setattr(state, "set_active_draft", fake_set_active_draft)

    builder = rmb.RichMessageBuilder(CHAT_ID)
    manager = DraftManager(builder)
    return {"builder": builder, "manager": manager, "sends": sends}


def _chunks(text: str, size: int) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


async def _await_swap(manager: DraftManager) -> None:
    """等待在途后台滚动任务结束（含缓冲回放），并让分离清理任务跑几拍。"""
    task = manager._rollover_task
    if task is not None and not task.done():
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    for _ in range(10):
        await asyncio.sleep(0)


async def _flush_like_stream_loop(manager: DraftManager) -> None:
    """模拟生产刷新时序：后台 _stream_flush_loop 每帧先提交流式缓冲再 flush。

    经 DraftManager.flush 调用（带 Draft 状态机镜像同步），与生产中
    事件消费入口一致。
    """
    manager._commit_stream_buffer()
    await manager.flush()


# ---------------------------------------------------------------------
# DraftEventBuffer（§9）单元语义
# ---------------------------------------------------------------------
def test_event_buffer_push_flush_order():
    buf = DraftEventBuffer()
    assert len(buf) == 0
    buf.push(AgentEvent(type=EventTypes.CONTENT_DELTA, data="a", seq=1))
    buf.push(AgentEvent(type=EventTypes.CONTENT_DELTA, data="b", seq=2))
    assert len(buf) == 2
    events = buf.flush()
    assert [e.data for e in events] == ["a", "b"]   # 按入队顺序
    assert len(buf) == 0
    assert buf.flush() == []                        # flush 后清零


# ---------------------------------------------------------------------
# Test 1（场景一）：超长 reasoning → Draft1 完整思考 / Draft2 正文
# ---------------------------------------------------------------------
def test_1_long_reasoning_rolls_complete_at_reasoning_end(env):
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        old_draft_id = builder.draft_id
        tail_marker = "REASONING_TAIL_MARKER_结束"

        # 超长 reasoning：增量经事件流进入草稿
        manager.emit(EventTypes.REASONING_START)
        assert manager.agent_phase == AgentPhase.THINKING
        reason_text = ("分析任务执行状态并思考实现路径，验证每一步约束是否成立。" * 300)
        for chunk in _chunks(reason_text, 180):
            manager.emit(EventTypes.REASONING_DELTA, chunk)
        manager.emit(EventTypes.REASONING_DELTA, tail_marker)

        # 真实容量检测（flush → _arm_rollover_if_needed）：只置 pending，不切换（§6）
        await _flush_like_stream_loop(manager)
        assert builder._rollover_pending is True
        assert manager.draft_phase == DraftPhase.ROLLOVER_PENDING
        assert sends.permanent_count == 0  # 容量预警绝不立即切换

        # reasoning.end 安全点 → 非阻塞调度后台滚动（Agent 不等待，§8）
        manager.emit(EventTypes.REASONING_END)
        assert manager._swap_scheduled is True
        assert sends.permanent_count == 0  # 事件提交即返回，网络发送在后台

        await _await_swap(manager)

        # Draft1：完整 reasoning 永久化（思考块绝不被截断，§7 场景一）
        assert sends.permanent_count == 1
        permanent_html = sends.permanent[0]
        assert tail_marker in permanent_html
        assert "分析任务执行状态" in permanent_html
        # 滚动守卫：不可拆散的结构未闭合时不滚动——此处思考块已闭合
        assert old_draft_id in sends.dead

        # Draft2 已建立：后续正文进入新草稿
        assert builder.draft_id != old_draft_id
        assert manager.draft_phase == DraftPhase.ACTIVE
        manager.emit(EventTypes.CONTENT_START)
        manager.emit(EventTypes.CONTENT_DELTA, "<p>最终答案正文</p>")
        assert manager.agent_phase == AgentPhase.CONTENT
        manager.emit(EventTypes.CONTENT_END)

        draft_html = builder._build_html_no_thinking()
        assert "最终答案正文" in draft_html
        assert "最终答案正文" not in permanent_html  # 正文没有混进 Draft1
        assert manager.pending_ui_events == 0

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# Test 2（场景二）：超长 content → Draft1 完整 markdown / Draft2 剩余内容
# ---------------------------------------------------------------------
def test_2_long_content_rolls_at_complete_markdown_block(env):
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        # 多个完整 <p> 块，总量显著超过滚动预算
        paragraphs = [
            f"<p>段落{idx:03d}：" + "内容文本持续输出用于撑大草稿体积。" * 12 + "</p>"
            for idx in range(80)
        ]
        manager.emit(EventTypes.CONTENT_START)
        for p in paragraphs:
            manager.emit(EventTypes.CONTENT_DELTA, p)
        await _flush_like_stream_loop(manager)
        assert builder._rollover_pending is True

        manager.emit(EventTypes.CONTENT_END)  # content.end 安全点 → 后台滚动
        assert manager._swap_scheduled is True
        await _await_swap(manager)

        assert sends.permanent_count == 1
        permanent_html = sends.permanent[0]
        # Draft1：完整 markdown——切割点必须落在完整外层块边界（§7 场景二）
        assert permanent_html.rstrip().endswith("</p>")
        assert permanent_html.count("<p>") == permanent_html.count("</p>")
        assert "<p>段落000" in permanent_html
        first_missing = next(
            (i for i in range(80) if f"段落{i:03d}" not in permanent_html), None)
        assert first_missing is not None, "滚动必须发生"
        # Draft2：剩余内容完整衔接，不丢段、不重段
        remainder_html = builder._build_html_no_thinking()
        assert f"段落{first_missing:03d}" in remainder_html
        for i in range(first_missing):
            assert f"段落{i:03d}" not in remainder_html
        # 滚动完成后相位：ACTIVE；若回放进新草稿的剩余内容自身又超过
        # 交互阈值，则合法进入 ROLLOVER_PENDING（下一安全点会再滚）。
        assert manager.draft_phase in (DraftPhase.ACTIVE, DraftPhase.ROLLOVER_PENDING)

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# Test 3（场景三）：tool call + result + 下一轮回答
#   Agent 侧：tool result 立即进入上下文、下一轮立即开始（不等待 UI）
#   UI 侧：tool call + tool result 完整落在同一草稿
# ---------------------------------------------------------------------
def test_tool_batch_end_consumes_newly_armed_rollover_without_waiting_for_next_round(env):
    """回归：tool.result 后才越过预警阈值时，tool.end 就是最近安全点。

    旧实现的问题是 _rollover_pending 只在异步 flush() 中 arm；TOOL_END 先
    检查 pending，往往读到 False，随后下一轮 reasoning 的 flush 才把 pending
    置上，导致必须等 reasoning.end 才 rollover。现在 safe boundary 会同步补做
    一次容量扫描，因此完整工具批次收束后即可调度滚动。
    """
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        old_draft_id = builder.draft_id

        manager.add_tool_item(
            "call_regression", "web_search", "Searching the web",
            search_query="large result", fn_args={"query": "large result"},
        )
        token = manager.begin_tool_batch()

        # 最后的工具结果本身把当前草稿推过交互预警阈值；不显式调用 flush，
        # 模拟生产中的“异步 flush 尚未来得及 arm”窗口。
        huge_result = "<p>" + ("工具返回内容。" * 1200) + "</p>"
        manager.update_tool_item(
            "call_regression", "Search complete", huge_result, status="done")
        assert builder._rollover_pending is False

        manager.finish_tool_batch(token)

        # tool.end 本身就是完整工具批次的最近退出点；这里必须已经调度，
        # 而不是等下一轮 reasoning.end。
        assert manager._swap_scheduled is True
        assert builder._rollover_pending is True

        await _await_swap(manager)

        assert sends.permanent_count == 1
        assert old_draft_id in sends.dead
        assert builder.draft_id != old_draft_id
        permanent_html = sends.permanent[0]
        assert "Search complete" in permanent_html
        assert "工具返回内容" in permanent_html

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# Test 3（场景三）：tool call + result + 下一轮回答
# ---------------------------------------------------------------------
def test_3_tool_result_and_next_round_answer_same_draft(env):
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        old_draft_id = builder.draft_id

        # ---- Round 1：说明文 + 流式工具卡片 ----
        manager.emit(EventTypes.CONTENT_START)
        manager.emit(EventTypes.CONTENT_DELTA, "<p>我来查一下天气。</p>")
        manager.add_tool_item(
            "call_1", "web_search", "Searching the web",
            search_query="weather", fn_args={"query": "weather"},
        )
        assert manager.agent_phase == AgentPhase.TOOL_RUNNING
        await _flush_like_stream_loop(manager)
        builder._rollover_pending = True  # 聚焦工具组守卫语义（容量已满）

        # 流结束后检查点：存在未收束工具组 → 推迟到 tool.end（WAIT_SAFE_POINT）
        manager.on_round_boundary()
        assert manager.draft_phase == DraftPhase.WAIT_SAFE_POINT
        assert manager._swap_scheduled is False
        assert sends.permanent_count == 0

        # ---- 工具执行完成：result + tool.end ----
        manager.update_tool_item(
            "call_1", "Searched the web", "<p>25°C, sunny</p>", status="done")
        manager.finish_tool_batch(manager.begin_tool_batch())  # finish_group

        # ★ Agent 不等待：tool.end 事件提交即返回，滚动已调度但尚未上网络
        assert manager._swap_scheduled is True
        assert sends.permanent_count == 0
        manager.on_tool_batch_end()  # 幂等：批次结束检查点不重复调度

        # ---- 下一轮 LLM 立即开始（滚动期间事件进入缓冲，§9）----
        manager.emit(EventTypes.CONTENT_START)
        manager.emit(EventTypes.CONTENT_DELTA, "<p>今天 25 度，晴。</p>")
        await _await_swap(manager)

        # UI 断言：Draft1 = tool call + tool result 完整同草稿（§7 场景三）
        assert sends.permanent_count == 1
        permanent_html = sends.permanent[0]
        assert "我来查一下天气" in permanent_html
        assert "Searched the web" in permanent_html
        assert "25°C, sunny" in permanent_html      # tool result 与 call 同草稿
        assert "今天 25 度" not in permanent_html     # 下一轮回答没有混进 Draft1

        # UI 断言：Draft2 = 下一轮回答（新 draft_id 已建立）
        assert builder.draft_id != old_draft_id
        draft_html = builder._build_html_no_thinking()
        assert "今天 25 度，晴。" in draft_html
        assert manager.pending_ui_events == 0
        assert manager.draft_phase == DraftPhase.ACTIVE
        assert manager.agent_phase == AgentPhase.CONTENT

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# §9：滚动换血期间事件缓冲 + 按序回放
# ---------------------------------------------------------------------
def test_buffer_replay_order_during_slow_swap(env, monkeypatch):
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        gate = asyncio.Event()

        async def slow_permanent(chat_id, html, reassert_draft=True, **kwargs):
            await gate.wait()  # 模拟永久化网络往返挂起
            sends.permanent.append(html)
            return 80001

        monkeypatch.setattr(rmb, "send_rich_html_message", slow_permanent)
        manager.emit(EventTypes.REASONING_START)
        manager.emit(EventTypes.REASONING_DELTA, "<p>思考中</p>")
        # 预警在思考增量之后置位：聚焦缓冲语义（若在增量前置位，预警后的
        # 首个思考增量会触发"思考折叠提前收束"路径，见下方专门用例）。
        builder._rollover_pending = True
        manager.emit(EventTypes.REASONING_END)   # 调度后台滚动，卡在 gate
        assert manager._swap_scheduled is True
        for _ in range(5):
            await asyncio.sleep(0)
        assert sends.permanent_count == 0        # 确认滚动挂起中

        # 滚动期间到达的下一轮事件：全部入缓冲（§9）
        manager.emit(EventTypes.CONTENT_START)
        manager.emit(EventTypes.CONTENT_DELTA, "<p>缓冲的增量一</p>")
        manager.emit(EventTypes.CONTENT_DELTA, "<p>缓冲的增量二</p>")
        manager.emit(EventTypes.CONTENT_END)
        assert manager.pending_ui_events == 4    # start + 2 delta + end
        assert "缓冲的增量一" not in builder._build_html_no_thinking()

        gate.set()
        await _await_swap(manager)

        # 回放：按原序写入新草稿，缓冲清零
        assert manager.pending_ui_events == 0
        draft_html = builder._build_html_no_thinking()
        assert "缓冲的增量一" in draft_html
        assert "缓冲的增量二" in draft_html
        assert draft_html.index("缓冲的增量一") < draft_html.index("缓冲的增量二")
        assert manager.draft_phase == DraftPhase.ACTIVE

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# 终局（turn.end）：同步收束旧段、不创建新草稿
# ---------------------------------------------------------------------
def test_finalize_turn_permanentizes_without_new_draft(env):
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        old_draft_id = builder.draft_id
        builder._rollover_pending = True
        manager.emit(EventTypes.CONTENT_START)
        manager.emit(EventTypes.CONTENT_DELTA, "<p>终局回复正文</p>")
        # 终局轮不再发中途边界事件：直接走 finalize_turn（对应生产中
        # will_request_again=False 的终局分支——只永久化旧段、不创建新草稿）
        result = await manager.finalize_turn()
        assert result is True
        assert sends.permanent_count == 1
        assert "终局回复正文" in sends.permanent[0]
        assert builder.draft_id == old_draft_id  # 终局不创建新草稿
        assert builder._rollover_count == 1
        assert manager.draft_phase == DraftPhase.CLOSED
        assert manager.agent_phase == AgentPhase.DONE
        assert manager.pending_ui_events == 0

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# 容量未满：安全点零开销，绝不滚动
# ---------------------------------------------------------------------
def test_no_capacity_no_swap(env):
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        manager.emit(EventTypes.REASONING_START)
        manager.emit(EventTypes.REASONING_DELTA, "<p>短思考</p>")
        manager.emit(EventTypes.REASONING_END)   # 安全点：未达容量 → 无动作
        manager.emit(EventTypes.CONTENT_START)
        manager.emit(EventTypes.CONTENT_DELTA, "<p>短回答</p>")
        manager.on_round_boundary()
        manager.on_tool_batch_end()
        for _ in range(10):
            await asyncio.sleep(0)
        assert manager._swap_scheduled is False
        assert manager._rollover_task is None
        assert sends.permanent_count == 0
        assert builder._rollover_pending is False
        assert manager.draft_phase == DraftPhase.ACTIVE
        assert "短回答" in builder._build_html_no_thinking()

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# §10：Agent / Draft 两条状态机独立演化
# ---------------------------------------------------------------------
def test_state_machines_evolve_independently(env):
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        # Agent 相位：THINKING → CONTENT → TOOL_RUNNING → CONTINUE_GENERATION
        manager.emit(EventTypes.REASONING_START)
        assert manager.agent_phase == AgentPhase.THINKING
        assert manager.draft_phase == DraftPhase.ACTIVE
        manager.emit(EventTypes.CONTENT_START)
        assert manager.agent_phase == AgentPhase.CONTENT
        manager.add_tool_item("call_x", "bash", "Running...", fn_args={})
        assert manager.agent_phase == AgentPhase.TOOL_RUNNING
        # Draft 相位独立：容量预警只改变 Draft 侧，不影响 Agent 相位
        builder._rollover_pending = True
        manager.emit(EventTypes.TOOL_RESULT, {
            "tool_id": "call_x", "summary": "Done",
            "details_html": "<p>ok</p>", "status": "done",
        })
        assert manager.agent_phase == AgentPhase.TOOL_RUNNING
        assert manager.draft_phase == DraftPhase.ROLLOVER_PENDING  # 镜像已同步，尚未到安全点
        manager.finish_group(None)  # tool.end → Agent 进入 CONTINUE_GENERATION
        assert manager.agent_phase == AgentPhase.CONTINUE_GENERATION
        assert manager.draft_phase in (DraftPhase.WAIT_SAFE_POINT, DraftPhase.ROLLOVER_PENDING)
        await _await_swap(manager)
        # turn.end：Agent → DONE，与 Draft 收束各自独立完成
        await manager.finalize_turn()
        assert manager.agent_phase == AgentPhase.DONE
        assert manager.draft_phase == DraftPhase.CLOSED
        assert sends.permanent_count >= 1

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# §7 场景三守卫：工具组未收束时，其他安全点绝不拆散工具组
# ---------------------------------------------------------------------
def test_pending_tool_group_defers_rollover(env):
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        builder._rollover_pending = True
        manager.add_tool_item("call_1", "bash", "Running...", fn_args={})
        # 工具组未收束：reasoning/content 安全点都必须推迟
        manager.on_round_boundary()
        assert manager._swap_scheduled is False
        assert manager.draft_phase == DraftPhase.WAIT_SAFE_POINT
        assert sends.permanent_count == 0
        # tool.end 到达：安全点满足 → 后台滚动
        manager.finish_group(None)
        assert manager._swap_scheduled is True
        await _await_swap(manager)
        assert sends.permanent_count == 1
        # 工具卡片（call）完整留在永久化草稿中，绝不被拆散
        assert "Running..." in sends.permanent[0] or "bash" in sends.permanent[0]

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# 草稿预警后的超长思考：提前收束思考折叠 → 滚动 → 新草稿折叠块续写
# ---------------------------------------------------------------------
def test_long_reasoning_splits_fold_at_capacity_warning(env):
    """预警后思考流仍在输出：无需等 reasoning.end，首个增量即提前收束。

    原行为下思考必须整段输出完毕才在 reasoning.end 安全点滚动；预警后
    思考继续写入同一折叠块，草稿一路膨胀。现行为在预警后的首个思考增量
    处提前收束折叠块并调度滚动，续写增量落入新草稿的新折叠块。
    """
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        old_draft_id = builder.draft_id
        head_marker = "预警前思考段_头部"
        trigger_marker = "触发提前收束的思考增量"
        continuation_marker = "预警后思考续写段_尾部"

        manager.emit(EventTypes.REASONING_START)
        manager.emit(EventTypes.REASONING_DELTA, head_marker)
        # 直接置位预警（真实容量 arm 路径已由 Test 1 覆盖）：聚焦收束行为。
        builder._rollover_pending = True

        # 预警后的首个思考增量：触发提前收束 + 调度后台滚动（无需等显式
        # reasoning.end；Agent 不等待，网络发送在后台）。该增量本身仍写入
        # 旧折叠块（先应用增量、再收束定桥）。
        manager.emit(EventTypes.REASONING_DELTA, trigger_marker)
        assert manager._swap_scheduled is True
        assert manager._reasoning_split_pending is True
        assert sends.permanent_count == 0
        # Agent 侧思考流未受影响：流类别保持 reasoning、相位保持 THINKING
        assert manager._open_stream_kind == "reasoning"
        assert manager.agent_phase == AgentPhase.THINKING
        assert manager.draft_phase == DraftPhase.WAIT_SAFE_POINT

        # 滚动已调度：后续思考增量进入缓冲（§9），滚动完成后回放进新草稿。
        manager.emit(EventTypes.REASONING_DELTA, continuation_marker)

        await _await_swap(manager)

        # 旧草稿：思考折叠块以完整结构定格并永久化（含触发增量，
        # 绝不包含滚动之后才到达的续写内容）。
        assert sends.permanent_count == 1
        permanent_html = sends.permanent[0]
        assert head_marker in permanent_html
        assert trigger_marker in permanent_html
        assert permanent_html.rstrip().endswith("</details>")
        assert continuation_marker not in permanent_html
        assert old_draft_id in sends.dead

        # 新草稿：续写增量落入新的思考折叠块（滚动后重开的 reasoning 块；
        # 增量先入流式缓冲，提交后按折叠块渲染——生产中刷新循环每帧提交）。
        assert builder.draft_id != old_draft_id
        assert manager.draft_phase == DraftPhase.ACTIVE
        assert builder.block_types[-1] == "reasoning"

        # 思考流真实结束：语义不变——提交续写折叠块；新草稿容量未满，
        # 不再触发第二次滚动。
        manager.emit(EventTypes.REASONING_DELTA, continuation_marker)
        manager.emit(EventTypes.REASONING_END)
        for _ in range(10):
            await asyncio.sleep(0)
        assert manager._swap_scheduled is False
        assert sends.permanent_count == 1
        draft_html = builder._build_html_no_thinking()
        assert "<details>" in draft_html and "</details>" in draft_html
        assert continuation_marker in draft_html
        assert manager.pending_ui_events == 0

    asyncio.run(scenario())


def test_reasoning_fold_split_defers_to_pending_tool_group(env):
    """预警 + 思考流 + 未收束工具组：不提前收束，等 tool.end 安全点。"""
    async def scenario():
        builder, manager, sends = env["builder"], env["manager"], env["sends"]
        builder._rollover_pending = True
        manager.add_tool_item("call_1", "bash", "Running...", fn_args={})
        manager.emit(EventTypes.REASONING_START)
        manager.emit(EventTypes.REASONING_DELTA, "工具组未收束时的思考增量")
        for _ in range(10):
            await asyncio.sleep(0)
        # 工具组未收束：不提前收束、不调度滚动
        assert manager._swap_scheduled is False
        assert manager._reasoning_split_pending is False
        assert sends.permanent_count == 0
        # 思考未被拆成两个折叠块，增量仍写入原折叠块（提交流式缓冲后断言）
        manager._commit_stream_buffer()
        reasoning_blocks = [
            b for b, t in zip(builder.blocks, builder.block_types)
            if t == "reasoning"
        ]
        assert len(reasoning_blocks) == 1
        assert "工具组未收束时的思考增量" in reasoning_blocks[0]
        # tool.end 后预警仍在：安全点照常调度滚动（原有行为不变）
        manager.finish_group(None)
        assert manager._swap_scheduled is True
        await _await_swap(manager)
        assert sends.permanent_count == 1

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# 兼容性：DraftManager 透传未拦截属性（duck typing 冒充 builder）
# ---------------------------------------------------------------------
def test_manager_proxies_builder_attributes(env):
    async def scenario():
        builder, manager = env["builder"], env["manager"]
        assert manager.chat_id == builder.chat_id
        assert manager.draft_id == builder.draft_id
        assert manager._tool_groups is builder._tool_groups
        assert manager.blocks is builder.blocks
        manager.add_initial_thinking("Thinking...")
        assert manager.set_thinking_status("加载上下文...") is True
        assert manager.blocks[0].startswith("<tg-thinking>")
        assert "加载上下文" in manager.blocks[0]

    asyncio.run(scenario())


from ai.draft_manager import AgentEvent as _AgentEvent  # noqa: F401,E402 - 兼容旧导入路径

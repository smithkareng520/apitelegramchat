# -*- coding: utf-8 -*-
"""ai bridge 公共基座：anthropic_bridge / gemini_bridge 的共享循环骨架。

两个原生桥接（Anthropic Messages API / Gemini streamGenerateContent）的
agentic 循环在「回合骨架」上完全同构——循环初始化、assistant 消息组装、
草稿流切换状态机、超限强制总结、终局收束逐字相同。本模块把这些片段
沉淀为公共实现；厂商差异只保留在各自桥接内的请求构造 / 流消费钩子里，
骨架级 bug（如草稿切换时序、超限总结语义）修复一处即两桥同时生效。

对外契约不变：
- ai.anthropic_bridge._agentic_loop_anthropic(client, model, messages, builder, ...)
- ai.gemini_bridge._agentic_loop_gemini_native(model, messages, builder, ...)
两函数返回 (final_content, final_usage, new_history_entries)，消息全部
保持 OpenAI 形状（见 agentic_loops.py 的边界转换约定）。
"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from typing import Any, Awaitable, Callable, Optional

from config import SUPPORTED_MODELS, get_sampling_params
from utils import get_logger
from chat_actions import start_chat_action, stop_chat_action
from core.messages import Message

from ai._constants import MAX_TOOL_CALLS
from ai.tool_summary import _tool_limit_summary
from ai.tool_call_loop import _run_tool_calls_and_append

if TYPE_CHECKING:
    # 仅供类型注解使用；运行时由 get_ai_response 统一包装后传入。
    from ai.draft_manager import DraftManager

logger = get_logger(__name__)


# 超过单轮工具调用上限后的强制总结指令（两 bridge 逐字共用）。
MAX_TOOL_CALLS_SYNTH_PROMPT = (
    f"System: Maximum tool calls ({MAX_TOOL_CALLS}) reached for this turn. "
    "Tool usage is now DISABLED. Please immediately summarize what you have "
    "successfully done so far, explicitly state what failed or what is left "
    "to do, and ask the user if they want to continue the operation in the "
    "next turn."
)


@dataclass
class BridgeLoopState:
    """原生桥接循环的回合状态（初始化语义与原两处实现逐字对齐）。"""

    loop_messages: list
    new_history_entries: list
    model_info: Any
    max_tokens: int
    sampling_params: dict
    final_content: Optional[str] = None
    final_usage: Any = None
    tool_call_count_ref: list = field(default_factory=lambda: [0])
    # 连续相同工具错误的熔断计数：key 是错误签名（首行，截 100 字符），
    # value 是连续命中次数。本轮 turn 内跨多次 _run_tool_calls_and_append
    # 调用共享同一份状态（每个 turn 由 init_bridge_loop_state 重新创建，
    # 不跨 turn 存活）。显式字段替代旧版借用 builder 对象做
    # setattr/getattr/vars() 反射存储的写法——熔断计数是"本轮工具循环"的
    # 状态，不属于 DraftManager（UI 渲染）的职责范围。
    error_streak: dict = field(default_factory=dict)


def init_bridge_loop_state(messages: list, journal: list | None, current_model: str) -> BridgeLoopState:
    """两条原生循环共用的初始化段（原 anthropic/gemini 各一份逐字相同）。"""
    loop_messages = list(messages)  # 内部 Message 列表，供 _run_tool_calls_and_append 复用
    new_history_entries = journal if journal is not None else []
    model_info = SUPPORTED_MODELS.get(current_model)
    max_tokens = model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192
    # 采样参数统一来自 config.py（含 per-model 覆盖），禁止在此硬编码。
    sampling_params = get_sampling_params(model_info)
    return BridgeLoopState(
        loop_messages=loop_messages,
        new_history_entries=new_history_entries,
        model_info=model_info,
        max_tokens=max_tokens,
        sampling_params=sampling_params,
    )


def make_switch_stream(builder: "DraftManager", cell: list) -> "Callable[[str], Awaitable[None]]":
    """草稿流切换状态机（原两 bridge 循环内逐字相同的 switch_stream 闭包）。

    ``cell`` 是单元素列表（[None] 或 [当前流类型]），代替闭包的
    nonlocal 变量；返回的协程函数语义与原实现完全一致：同一流类型
    幂等返回；切换前结束当前流，并在"此前确有流"时触发回合中途的
    块边界换草稿检查点。

    解耦改造：检查点为非阻塞事件（``on_stream_block_closed``）——
    真正是否切换仍由 DraftManager 内的容量阈值决定；未达阈值时无
    额外开销；满容量时由后台任务执行滚动，Agent 不等待 UI（§8）；
    滚动换血期间到达的事件经 DraftEventBuffer 缓冲后回放（§9）。
    若本轮已有未收束的工具组，安全点守卫会推迟滚动到 tool.end，
    从而不会把工具卡片拆散（历史问题1）。
    """

    async def switch_stream(target: str) -> None:
        if cell[0] == target:
            return
        ended = cell[0]
        builder.end_stream()
        # 块边界换草稿检查点①②（非阻塞事件）：一个思考块或文本块刚刚
        # 闭合、下一个块尚未开启，此刻 HTML 正好停在完整外层块边界上，
        # 是回合中途最安全的切换时机（不必再等整批工具结果回来）。
        if ended is not None:
            builder.on_stream_block_closed(ended)
        if target == "reasoning":
            builder.begin_stream_reasoning()
        elif target == "content":
            builder.begin_stream_text()
        cell[0] = target

    return switch_stream


def finish_open_tool_group(builder: "DraftManager") -> None:
    """若最后一个工具组尚未收束则 finish_group（原两循环共 6 处守卫）。"""
    if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
        builder.finish_group(len(builder._tool_groups) - 1)


def append_assistant_message(
    loop_messages: list,
    new_history_entries: list,
    content_acc: str,
    tool_calls_list: list,
    reasoning_acc: str,
) -> Message:
    """把本轮 assistant 消息组装为内部 Message，写入请求消息与历史双列表。

    重构说明：旧版组装 OpenAI 形状 dict（含 reasoning_content / tool_calls
    wire 字段）；现在组装为内部 Message（TextBlock / ReasoningBlock /
    ToolCallBlock），协议形状由各适配器在出站时渲染。tool_calls_list 仍是
    流式累积产出的 OpenAI wire 形状（由 Message.assistant_with_tool_calls
    解析为结构化 ToolCallBlock）。
    """
    assistant_msg = Message.assistant_with_tool_calls(
        content_acc or "", tool_calls_list, reasoning_acc,
    )
    loop_messages.append(assistant_msg)
    new_history_entries.append(assistant_msg)
    return assistant_msg


async def run_tool_batch(
    builder: "DraftManager",
    tool_calls_list: list,
    loop_messages: list,
    new_history_entries: list,
    tool_call_count_ref: list,
    api_label: str,
    tools: list,
    error_streak: Optional[dict] = None,
) -> str:
    """执行工具批次，随后触发 tool.end 安全点（非阻塞）。

    解耦改造（§8）：工具结果已全部写入 loop_messages / 历史（即已进入
    conversation context），调用方立即发起下一轮 LLM 请求；满容量时
    草稿滚动由 DraftManager 在 tool.end 安全点后台执行，不再阻塞 Agent。

    ``error_streak``：连续相同工具错误的熔断计数状态，由调用方传入的
    ``BridgeLoopState.error_streak``（或等价的本轮字典）在多轮之间共享；
    省略时 ``_run_tool_calls_and_append`` 内部临时创建一个一次性字典，
    熔断退化为\"仅本批次内生效\"（不建议——调用方应始终传入跨轮共享的
    字典）。
    """
    status = await _run_tool_calls_and_append(
        tool_calls_list, loop_messages, new_history_entries,
        tool_call_count_ref, api_label, builder, chat_id=builder.chat_id,
        tools=tools, error_streak=error_streak,
    )
    builder.on_tool_batch_end()
    return status


async def over_limit_final_summary(
    builder: "DraftManager",
    new_history_entries: list,
    *,
    api_label: str,
    loop_name: str,
    build_synth_request: Callable[[dict], Any],
    stream_synth: Callable[[Any], Any],
    postprocess: Optional[Callable[[str], str]] = None,
) -> str:
    """超限强制总结骨架（原两循环 ~80% 相同的 50 行收敛为一份）。

    流程：追加系统指令（禁用工具面）→ 流式输出最终总结（实时可见）→
    空内容兜底 _tool_limit_summary → 写入历史 → 收束工具组 → 结束旧草稿。

    Args:
        build_synth_request: 接收合成指令 user 消息，返回厂商请求描述符
            （anthropic 为 (system, messages) 元组；gemini 为请求 body dict）。
        stream_synth: 消费厂商流，逐块调用 builder.append_stream_delta，
            返回累积的合成文本。
        postprocess: 厂商侧文本后处理（gemini 剥离 textual tool calls 并
            replace_trailing_text；anthropic 无此步骤传 None）。
    """
    final_content = ""
    try:
        await start_chat_action(builder.chat_id, "typing")
        builder.begin_stream_text()
        synth_text = ""
        synth_text += await stream_synth(build_synth_request(
            Message.user_text(MAX_TOOL_CALLS_SYNTH_PROMPT)))
        raw_synth_content = builder.end_stream_text() or synth_text
        # 文本块结束时检查是否需要切换草稿（终局：同步收束旧段）
        if raw_synth_content:
            await builder.finalize_turn()
        final_content = postprocess(raw_synth_content) if postprocess is not None else raw_synth_content
        if postprocess is not None and final_content != raw_synth_content:
            builder.replace_trailing_text(raw_synth_content, final_content)
        if not final_content:
            final_content = _tool_limit_summary()
            builder.add_text(final_content)
    except Exception as synth_err:
        logger.warning(f"[{api_label}] 合成流失败: {synth_err}")
        try:
            builder.end_stream_text()
        except Exception:
            logger.debug(f"{loop_name} 内部忽略的异常", exc_info=True)
        final_content = _tool_limit_summary()
        builder.add_text(final_content)
    finally:
        await stop_chat_action(builder.chat_id, "typing")
    new_history_entries.append(Message.assistant_text(final_content or ""))
    finish_open_tool_group(builder)
    # 工具上限总结是终局回复；同步结束旧草稿，不创建新草稿。
    await builder.finalize_turn()
    return final_content


async def ensure_final_content(builder: "DraftManager", new_history_entries: list, final_content: Optional[str]) -> str:
    """轮次耗尽 / 空终局兜底：写入 _tool_limit_summary 并收束（逐字共用）。"""
    if final_content is None:
        final_content = _tool_limit_summary()
        builder.add_text(final_content)
        new_history_entries.append(Message.assistant_text(final_content))
        finish_open_tool_group(builder)
        # 轮次数耗尽后的兜底文本没有后续轮次：同步结束旧草稿，不创建新草稿。
        await builder.finalize_turn()
    return final_content

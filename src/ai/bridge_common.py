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
from typing import Any, Callable, Optional

from config import SUPPORTED_MODELS, get_sampling_params
from utils import get_logger
from chat_actions import start_chat_action, stop_chat_action

from ai._constants import MAX_TOOL_CALLS
from ai.tool_summary import _tool_limit_summary
from ai.tool_call_loop import _run_tool_calls_and_append

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


def init_bridge_loop_state(messages: list, journal: list | None, current_model: str) -> BridgeLoopState:
    """两条原生循环共用的初始化段（原 anthropic/gemini 各一份逐字相同）。"""
    loop_messages = list(messages)  # OpenAI 形状，供 _run_tool_calls_and_append 复用
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


def make_switch_stream(builder, cell: list):
    """草稿流切换状态机（原两 bridge 循环内逐字相同的 switch_stream 闭包）。

    ``cell`` 是单元素列表（[None] 或 [当前流类型]），代替闭包的
    nonlocal 变量；返回的协程函数语义与原实现完全一致：同一流类型
    幂等返回；切换前结束当前流，并在"此前确有流"时触发回合中途的
    块边界换草稿检查点。
    """

    async def switch_stream(target: str) -> None:
        if cell[0] == target:
            return
        ended = cell[0]
        builder.end_stream()
        # 块边界换草稿检查点①②：一个思考块或文本块刚刚闭合、下一个块
        # 尚未开启，此刻 HTML 正好停在完整外层块边界上，是回合中途最
        # 安全的切换时机（不必再等整批工具结果回来）。
        # 真正是否切换仍由 rollover_at_turn_boundary 内的容量阈值决定；
        # 未达阈值时立即返回 False，热路径无额外开销。
        # 若本轮已有未收束的工具组，函数内的守卫会拒绝滚动，
        # 从而不会把工具卡片拆散（历史问题1）。
        if ended is not None:
            await builder.rollover_at_turn_boundary(start_next_draft=True)
        if target == "reasoning":
            builder.begin_stream_reasoning()
        elif target == "content":
            builder.begin_stream_text()
        cell[0] = target

    return switch_stream


def finish_open_tool_group(builder) -> None:
    """若最后一个工具组尚未收束则 finish_group（原两循环共 6 处守卫）。"""
    if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
        builder.finish_group(len(builder._tool_groups) - 1)


def append_assistant_message(
    loop_messages: list,
    new_history_entries: list,
    content_acc: str,
    tool_calls_list: list,
    reasoning_acc: str,
) -> dict:
    """把本轮 assistant 消息组装为 OpenAI 形状，写入请求消息与历史双列表。"""
    assistant_msg: dict = {"role": "assistant", "content": content_acc or None}
    if tool_calls_list:
        assistant_msg["tool_calls"] = tool_calls_list
    if reasoning_acc:
        assistant_msg["reasoning_content"] = reasoning_acc
    loop_messages.append(assistant_msg)
    new_history_entries.append(assistant_msg)
    return assistant_msg


async def run_tool_batch(
    builder,
    tool_calls_list: list,
    loop_messages: list,
    new_history_entries: list,
    tool_call_count_ref: list,
    api_label: str,
    tools: list,
) -> str:
    """执行工具批次，随后滚动创建新草稿（工具批次后仍会继续请求模型）。"""
    status = await _run_tool_calls_and_append(
        tool_calls_list, loop_messages, new_history_entries,
        tool_call_count_ref, api_label, builder, chat_id=builder.chat_id,
        tools=tools,
    )
    await builder.rollover_at_turn_boundary(start_next_draft=True)
    return status


async def over_limit_final_summary(
    builder,
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
            {"role": "user", "content": MAX_TOOL_CALLS_SYNTH_PROMPT}))
        raw_synth_content = builder.end_stream_text() or synth_text
        # 文本块结束时检查是否需要切换草稿
        if raw_synth_content:
            await builder.rollover_at_turn_boundary(start_next_draft=False)
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
    new_history_entries.append({"role": "assistant", "content": final_content or ""})
    finish_open_tool_group(builder)
    # 工具上限总结是终局回复；结束旧草稿，不创建新草稿。
    await builder.rollover_at_turn_boundary(start_next_draft=False)
    return final_content


async def ensure_final_content(builder, new_history_entries: list, final_content: Optional[str]) -> str:
    """轮次耗尽 / 空终局兜底：写入 _tool_limit_summary 并收束（逐字共用）。"""
    if final_content is None:
        final_content = _tool_limit_summary()
        builder.add_text(final_content)
        new_history_entries.append({"role": "assistant", "content": final_content})
        finish_open_tool_group(builder)
        # 轮次数耗尽后的兜底文本没有后续轮次：结束旧草稿，但不创建新草稿。
        await builder.rollover_at_turn_boundary(start_next_draft=False)
    return final_content

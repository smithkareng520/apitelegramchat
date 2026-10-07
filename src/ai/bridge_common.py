# -*- coding: utf-8 -*-
"""ai bridge 公共基座：提供原生桥接共用的回合与工具循环骨架。"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from typing import Any, Awaitable, Callable, Optional

from config import SUPPORTED_MODELS, get_sampling_params
from utils import get_logger
from chat_actions import start_chat_action, stop_chat_action
from core.messages import Message, ReasoningBlock, TextBlock
from turn_recovery import LIVE_STREAM_FLAG, SYNTHETIC_ASSISTANT_FLAG

from ai._constants import MAX_TOOL_CALLS
from ai.tool_summary import _tool_limit_summary
from ai.tool_call_loop import _run_tool_calls_and_append
from ai.json_repair import _finish_reason_cut_info

if TYPE_CHECKING:
    # 仅供类型注解使用；运行时由 get_ai_response 统一包装后传入。
    from ai.draft_manager import DraftManager

logger = get_logger(__name__)


# 超过单轮工具调用上限后的强制总结指令。
MAX_TOOL_CALLS_SYNTH_PROMPT = (
    f"System: Maximum tool calls ({MAX_TOOL_CALLS}) reached for this turn. "
    "Tool usage is now DISABLED. Please immediately summarize what you have "
    "successfully done so far, explicitly state what failed or what is left "
    "to do, and ask the user if they want to continue the operation in the "
    "next turn."
)


@dataclass
class BridgeLoopState:
    """原生桥接循环的回合状态。"""

    loop_messages: list
    new_history_entries: list
    model_info: Any
    max_tokens: int
    sampling_params: dict
    final_content: Optional[str] = None
    final_usage: Any = None
    tool_call_count_ref: list = field(default_factory=lambda: [0])
    # key 为错误首行（最多 100 字符），仅在当前 turn 内累计。
    error_streak: dict = field(default_factory=dict)


def init_bridge_loop_state(messages: list, journal: list | None, current_model: str) -> BridgeLoopState:
    """初始化原生桥接循环的共享状态。"""
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
    """切换草稿流；仅在已有流结束后触发非阻塞块边界检查点。"""

    async def switch_stream(target: str) -> None:
        if cell[0] == target:
            return
        ended = cell[0]
        builder.end_stream()
        # 在完整块边界触发非阻塞检查点，避免拆散当前块。
        if ended is not None:
            builder.on_stream_block_closed(ended)
        if target == "reasoning":
            builder.begin_stream_reasoning()
        elif target == "content":
            builder.begin_stream_text()
        cell[0] = target

    return switch_stream


def finish_open_tool_group(builder: "DraftManager") -> None:
    """收束最后一个未完成的工具组。"""
    if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
        builder.finish_group(len(builder._tool_groups) - 1)


class LiveAssistantSlot:
    """流式 assistant 的 journal 占位；打断时保留已产出的内容。

    占位只同步文本和思考，tool_calls 等流正常结束后再写入；
    ``LIVE_STREAM_FLAG`` 用于区分未定稿消息。
    """

    __slots__ = ("_journal", "_msg")

    def __init__(self, journal: list) -> None:
        self._journal = journal
        # 占位只进 journal；正常结束后由 finalize 追加到请求消息。
        self._msg = Message(role="assistant", blocks=[], meta={LIVE_STREAM_FLAG: True})
        journal.append(self._msg)

    @property
    def message(self) -> Message:
        """占位消息本体（journal 持有同一对象，原地更新即时可见）。"""
        return self._msg

    def sync(self, content_acc: str, reasoning_acc: str) -> None:
        """把当前累积的文本/思考快照原地写入占位消息（幂等）。

        调用时机与 ``RichMessageBuilder._stream_buffer`` 的更新对齐——
        同一份 delta，草稿/历史两条消费管线同步更新。每次重建至多两个
        内容块（ReasoningBlock / TextBlock），成本 O(1)，远低于增量字符串
        累积本身；块序与 ``Message.assistant_with_tool_calls`` 一致
        （思考在前、文本在后），保证打断路径与正常路径写出的消息形状
        逐字段相同。
        """
        blocks: list = []
        if reasoning_acc:
            blocks.append(ReasoningBlock(reasoning_acc))
        if content_acc:
            blocks.append(TextBlock(content_acc))
        self._msg.blocks = blocks

    def finalize(
        self,
        loop_messages: list,
        content_acc: str,
        tool_calls_list: list,
        reasoning_acc: str,
    ) -> Message:
        """流正常结束：原地补全占位消息（tool_calls 等），并追加进请求消息列表。

        journal 中的占位消息原地升级为完整消息，并将同一对象加入 loop_messages，
        避免正常路径产生重复 assistant 消息。
        """
        final = Message.assistant_with_tool_calls(
            content_acc or "", tool_calls_list, reasoning_acc,
        )
        self._msg.blocks = final.blocks
        # 定稿后移除直播标记，避免打断裁剪再次处理。
        self._msg.meta.pop(LIVE_STREAM_FLAG, None)
        loop_messages.append(self._msg)
        return self._msg


class MediaProgressSlot:
    """一次性媒体调用的 journal 进度占位。

    成功时原地定稿，失败时移除；取消时保留占位供打断恢复。
    """

    __slots__ = ("_journal", "_msg")

    def __init__(self, journal: Optional[list], progress_text: str) -> None:
        self._journal = journal
        # 合成占位标记：文本不是流式渲染产物、从未进入渲染游标计数——
        # trim_interrupted_stream 计算前文合计时排除本条，避免挤压
        # 后续直播占位的截断预算（媒体轮之后通常还有文本总结轮）。
        self._msg = Message(
            role="assistant",
            blocks=[TextBlock(progress_text)] if progress_text else [],
            meta={SYNTHETIC_ASSISTANT_FLAG: True},
        )
        if journal is not None:
            journal.append(self._msg)

    @property
    def message(self) -> Message:
        """占位消息本体（journal 持有同一对象，原地更新即时可见）。"""
        return self._msg

    def complete(self, final_text: str) -> list:
        """生成成功：占位原地更新为最终历史内容，返回 new_entries 列表。

        必须返回 journal 本体（而非新建 ``[self._msg]``）：调用方
        update_conversation_and_ledger 会拿 new_msgs 调
        ``turn_recovery.note_turn_persisted``，后者按列表对象身份注销
        in-flight 登记；返回新列表则身份永不匹配，登记滞留注册表，
        回合结束后 drain_completed_turns 会把同一条消息二次持久化进
        历史（模型看到重复的图片记录）。journal 为 None 时（无登记的
        独立媒体轮）退回新建列表保底。
        """
        self._msg.set_text(final_text)
        return self._journal if self._journal is not None else [self._msg]

    def drop(self) -> None:
        """生成失败：整体移除占位（幂等；保持失败轮替换语义不变）。"""
        if self._journal is None:
            return
        try:
            self._journal.remove(self._msg)
        except ValueError:
            pass  # 已被移除（重复 drop / 并发保全快照后原列表被清理）


# 非流式调用所需的最小 OpenAI 风格响应对象。
class SimpleFunctionCall:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class SimpleToolCall:
    def __init__(self, id_: str, name: str, arguments: str) -> None:
        self.id = id_
        self.function = SimpleFunctionCall(name, arguments)


class SimpleMessage:
    """模拟 OpenAI SDK 的 resp.choices[0].message 接口（仅 .content /
    .tool_calls 两个属性）。"""

    def __init__(self, content: str, tool_calls: list) -> None:
        self.content = content
        self.tool_calls = tool_calls


class SimpleChoice:
    def __init__(self, message: "SimpleMessage") -> None:
        self.message = message


class SimpleResponse:
    def __init__(self, choices: list, usage: Any = None) -> None:
        self.choices = choices
        self.usage = usage


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
    """执行工具批次并触发非阻塞的 tool.end 安全点。

工具结果先写入请求消息和历史，再进入下一轮模型调用。"""
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
    build_synth_request: Callable[[Any], Any],
    stream_synth: Callable[[Any], Any],
    postprocess: Optional[Callable[[str], str]] = None,
) -> str:
    """达到工具上限后流式生成最终总结并收束当前草稿。

过程会禁用工具、写入历史，并在空内容时使用兜底文本。"""
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


# 纯文本终局在输出上限导致截断时追加提示；复用统一的 finish_reason 判定。
_TRUNCATION_NOTICE = "\n\n_（回答因达到输出长度上限被截断，如需继续请回复“继续”。）_"


def append_truncation_notice_if_needed(
        builder: "DraftManager", content_acc: str,
        stream_finish_reason: Optional[str]) -> str:
    """纯文本终局在输出上限导致截断时追加提示。

未触发条件时原样返回 content_acc。"""
    if not content_acc:
        return content_acc
    is_cut, cause = _finish_reason_cut_info(stream_finish_reason)
    if not (is_cut and "output token limit" in cause):
        return content_acc
    builder.add_text(_TRUNCATION_NOTICE)
    return content_acc + _TRUNCATION_NOTICE


async def ensure_final_content(builder: "DraftManager", new_history_entries: list, final_content: Optional[str]) -> str:
    """轮次耗尽或空终局时写入兜底文本并收束草稿。"""
    if final_content is None:
        final_content = _tool_limit_summary()
        builder.add_text(final_content)
        new_history_entries.append(Message.assistant_text(final_content))
        finish_open_tool_group(builder)
        # 轮次数耗尽后的兜底文本没有后续轮次：同步结束旧草稿，不创建新草稿。
        await builder.finalize_turn()
    return final_content

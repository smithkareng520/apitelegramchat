# tool_visibility.py
"""出站历史中工具调用痕迹的插拔过滤器（纯函数，只改出站副本）。

本模块只做两件事，两者都围绕同一核心动作——把出站历史副本中的
assistant tool_calls 与配对 role=tool 消息成对拔除：

1. 开关维度插拔（apply_tool_visibility）
   静默专属工具 ``deliver_reply`` 只在 /show off（静默）回合的工具面
   里暴露（模型通过 send 布尔参数决定是否发送；send 缺省值按事件源
   区分——静默 USER 回合默认 true，静默 TIMER 回合默认 false，因此
   /show on 下模型看不到该工具也就不会产生除草稿外的单独发送）。
   非静默回合除了不提供工具定义（见 ai_handlers._call_api），出站
   历史副本中已有的调用痕迹也一并拔除，避免模型看到并模仿调用一个
   当前不可用的工具；回到静默回合时痕迹在原位置原样插回。

2. 能力维度全清（strip_tool_traces）
   ``supports_tools=False`` 的模型（图像模型等）切进一个充满工具痕迹
   的对话时，出站历史里的痕迹原样透传会出问题：严格网关（Anthropic
   原生等，要求消息含 tool_use/tool_result 块时请求必须声明 tools）
   直接 400；宽松网关虽然接受，但痕迹照常占上下文并诱导模型模仿输出
   文本形态的工具调用（且与 _NO_TOOLS_SECTION 的系统提示自相矛盾）。
   全量 DROP：assistant 消息剔除全部 ToolCallBlock（文本保留）、
   role=tool 消息整条移除、剔除后既无文本也无剩余调用的 assistant
   空壳整条丢弃。

三条硬性保证
============

1. **只改出站副本，绝不改持久历史**：需要改写的消息一律重建新 Message。
2. **结构合法性**：被移除的 tool_call 与其配对 tool 消息总是成对处理，
   出站消息里不存在悬空 ``tool_call_id``（否则多数供应商直接 400）。
3. **确定性**：同一份历史在同一开关组合下的改写结果逐字节一致，
   隐式前缀缓存不会因本模块而额外退化。

注入点：ai_handlers.get_ai_response（apply_tool_visibility 之后、
strip_tool_traces 之后、_append_history_async 之前），三条协议路径
（openai_chat / anthropic_messages / gemini_native）共用该入口，
一处清理全覆盖。环境变量 ``TOOL_VISIBILITY_FILTER=false`` 可整体
关闭（等价于拔掉本模块）。
"""

from __future__ import annotations

import os
from core.messages import Message, TextBlock, ToolCallBlock
from typing import Iterable, Optional

__all__ = [
    "SILENT_ONLY_TOOLS",
    "apply_tool_visibility",
    "strip_tool_traces",
]


# =====================================================================
# 开关
# =====================================================================
def _env_flag(name: str, default: bool = True) -> bool:
    """与 proactive._env_flag 同语义的本地实现（避免跨模块私有导入）。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# 总开关：false = 整个过滤器直通（等价于拔掉本模块）。
TOOL_VISIBILITY_FILTER = _env_flag("TOOL_VISIBILITY_FILTER", True)

# 静默专属工具（开关维度插拔）：仅在 /show off（静默）回合的工具面里
# 暴露。非静默回合由 get_ai_response 通过
# ``apply_tool_visibility(..., hidden_tools=SILENT_ONLY_TOOLS)`` 把这些
# 工具在出站历史副本中的调用痕迹整体拔除；静默回合不传 hidden_tools，
# 痕迹在原位置原样保留（插回原位置）。持久历史从不被改动。
SILENT_ONLY_TOOLS: frozenset[str] = frozenset({"deliver_reply"})


# =====================================================================
# 核心：出站消息改写（纯函数，绝不原地修改入参）
# =====================================================================
def apply_tool_visibility(
    messages: list,
    hidden_tools: Optional[Iterable[str]] = None,
) -> list:
    """把 ``hidden_tools`` 中工具的调用痕迹从出站消息列表中拔除。

    纯函数：返回新列表；未被改写的消息原样引用（零拷贝），被改写的
    一律重建新 Message——绝不污染调用方持有的持久历史。无 hidden_tools
    时直接返回原列表（零开销直通路径）。

    被拔除的 tool_call 与其配对的 tool 结果消息总是成对处理，出站
    消息里不存在悬空 ``tool_call_id``。
    """
    if not TOOL_VISIBILITY_FILTER or not messages:
        return messages

    targets = {
        name for name in (hidden_tools or ())
        if isinstance(name, str) and name
    }
    if not targets:
        return messages

    # Pass 1：改写含目标工具调用的 assistant 消息，登记被隐藏的 tool_call_id。
    rewritten: list = []
    hidden_call_ids: set[str] = set()
    for msg in messages:
        if not isinstance(msg, Message):
            rewritten.append(msg)
            continue
        calls = msg.tool_calls()
        if msg.role != "assistant" or not calls:
            rewritten.append(msg)
            continue

        kept_calls: list[ToolCallBlock] = []
        touched = False
        for tc in calls:
            if tc.name in targets:
                if tc.id:
                    hidden_call_ids.add(tc.id)
                touched = True
                continue
            kept_calls.append(tc)

        if not touched:
            rewritten.append(msg)
            continue

        # 重建新 Message（绝不原地改动持久历史对象）。
        new_msg = Message(role=msg.role, blocks=list(kept_calls) + [
            b for b in msg.blocks if not isinstance(b, ToolCallBlock)
        ], name=msg.name, meta=dict(msg.meta))

        # 整条消息折叠后既无文本也无剩余调用：丢弃空壳，避免产生
        # content=None 且无 tool_calls 的非法 assistant 消息。
        if not new_msg.tool_calls():
            has_text = any(
                isinstance(b, TextBlock) and b.text for b in new_msg.blocks
            )
            if not has_text:
                continue
        rewritten.append(new_msg)

    # Pass 2：移除与被隐藏调用配对的 tool 结果消息，保证配对完整性。
    if not hidden_call_ids:
        return rewritten
    out: list = []
    for m in rewritten:
        if isinstance(m, Message) and m.role == "tool":
            tr = m.tool_result_block()
            if tr is not None and tr.tool_call_id in hidden_call_ids:
                continue
        out.append(m)
    return out


def strip_tool_traces(messages: list) -> list:
    """把出站消息列表中的全部工具调用痕迹 DROP（能力维度过滤）。

    纯函数：返回新列表；未被改写的消息原样引用（零拷贝），被改写的
    一律重建新 Message——绝不污染调用方持有的持久历史。无工具痕迹时
    直接返回原列表（零开销直通路径）。改写是确定性的（同一输入逐字节
    同输出），隐式前缀缓存不因本函数额外退化。

    与 ``apply_tool_visibility`` 的分工：后者按"工具名"做开关维度的
    选择性插拔；本函数按"模型能力"做全量清除。调用顺序：先
    apply_tool_visibility（选择性），后 strip_tool_traces（全量，语义
    上后者包含前者）。
    """
    if not TOOL_VISIBILITY_FILTER or not messages:
        return messages

    # 预扫描：是否存在工具痕迹（零开销直通路径）。
    has_traces = False
    for m in messages:
        if isinstance(m, Message) and (
            m.role == "tool" or (m.role == "assistant" and m.tool_calls())
        ):
            has_traces = True
            break
    if not has_traces:
        return messages

    out: list = []
    for m in messages:
        if not isinstance(m, Message):
            out.append(m)
            continue
        if m.role == "tool":
            # role=tool 整条移除（结果随依附的 tool_call 一起消失）。
            continue
        if m.role == "assistant" and m.tool_calls():
            # 重建不含 ToolCallBlock 的 assistant（保留文本/多模态/
            # 思考块）；meta 原样保留（内部标记永不进出站，由渲染
            # 层结构性保证）。
            kept_blocks = [
                b for b in m.blocks if not isinstance(b, ToolCallBlock)
            ]
            has_text = any(
                isinstance(b, TextBlock) and b.text for b in kept_blocks
            )
            if not has_text:
                # 只有工具调用没有正文：剔除后是空壳，整条丢弃。
                continue
            out.append(Message(
                role="assistant",
                blocks=kept_blocks,
                name=m.name,
                meta=dict(m.meta),
            ))
            continue
        out.append(m)
    return out

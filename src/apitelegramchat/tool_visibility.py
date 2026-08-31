# tool_visibility.py
"""按事件源（USER / TIMER）控制历史中工具调用的可见性——可拔插的出站消息过滤器。

现状（本轮重构）
================

``send_message_to_user`` 工具已随 TIMER 静默机制整体移除；替代它的
``message_user`` 在 USER 与 TIMER 两类回合的工具面里都存在，历史中的
调用痕迹无需再按事件源折叠——因此事件源注册表为空。

但静默专属工具 ``deliver_reply`` 需要按 ``/show`` 开关做**历史上下文
插拔**（见 ``SILENT_ONLY_TOOLS``）：它只在静默回合（/show off）的工具面
里暴露（模型通过 send 布尔参数决定是否发送；send 缺省值按事件源区分
——静默 USER 回合默认 true（不填即发送，收尾有兜底），静默 TIMER
回合默认 false，因此 /show on 下模型看不到该工具也就不会产生除草稿
外的单独发送）；非静默回合除了不提供工具定义，出站历史副本中已有的
调用痕迹（assistant 的 tool_calls 与配对的 tool 消息）也一并拔除，
避免模型看到并模仿调用一个当前不可用的工具；回到静默回合时痕迹在
原位置原样插回（持久历史从不被改动，插拔只作用于出站副本）。

事件源维度的可拔插机制保留，供未来需要按事件源隐藏某个工具时使用：
在 ``TOOL_VISIBILITY_RULES`` 加一行规则即可。

三条硬性保证（机制不变）
========================

1. **只改出站副本，绝不改持久历史**：需要改写的消息一律深拷贝。
2. **结构合法性**：被移除/折叠的 tool_call 与其配对 tool 消息总是成对处理，
   出站消息里不存在悬空 ``tool_call_id``（否则多数供应商直接 400）。
3. **确定性**：同一份历史在同一事件源下的改写结果逐字节一致，隐式前缀
   缓存不会因本模块而额外退化。

可拔插设计
==========

- 注册表 ``TOOL_VISIBILITY_RULES`` 是事件源维度的唯一扩展点：要隐藏
  某个工具，加一行规则即可；删掉对应条目即恢复直通。
- 每条规则分别指定 ``user_turn`` / ``timer_turn`` 两个方向的模式，互不影响。
- ``hidden_tools`` 参数是开关维度的扩展点：与事件源无关、两个方向都
  DROP 的工具集合（``SILENT_ONLY_TOOLS`` 即由此驱动）。
- 环境变量 ``TOOL_VISIBILITY_FILTER=false`` 可整体关闭（等价于拔掉本模块）。
- shadow 摘要文案通过规则的 ``shadow_note_builder`` 注入。
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

__all__ = [
    "VISIBILITY_KEEP",
    "VISIBILITY_DROP",
    "VISIBILITY_SHADOW",
    "ToolVisibilityRule",
    "TOOL_VISIBILITY_RULES",
    "SILENT_ONLY_TOOLS",
    "apply_tool_visibility",
]


# =====================================================================
# 开关与模式
# =====================================================================
def _env_flag(name: str, default: bool = True) -> bool:
    """与 proactive._env_flag 同语义的本地实现（避免跨模块私有导入）。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# 总开关：false = 整个过滤器直通（等价于拔掉本模块）。
TOOL_VISIBILITY_FILTER = _env_flag("TOOL_VISIBILITY_FILTER", True)

VISIBILITY_KEEP = "keep"
VISIBILITY_DROP = "drop"
VISIBILITY_SHADOW = "shadow"

_VALID_MODES = (VISIBILITY_KEEP, VISIBILITY_DROP, VISIBILITY_SHADOW)


# =====================================================================
# 规则注册表（可拔插扩展点）
# =====================================================================
def _default_shadow_note(tool_call: dict, tool_result: Optional[dict]) -> str:
    """通用 shadow 摘要：不解析工具语义，只说明"这里发生过一次已隐藏的调用"。"""
    return f"（历史记录：此处曾有一次 {tool_call.get('function', {}).get('name', 'unknown')} 调用，已按可见性规则隐藏）"


@dataclass(frozen=True)
class ToolVisibilityRule:
    """单个工具在两类事件源下的可见性规则。

    - ``user_turn`` / ``timer_turn``：各自方向的处理模式
      （keep / drop / shadow），TIMER 方向建议保持 keep——后台回合本来
      就需要看到完整调用史（edit/delete 依赖历史 message_id）。
    - ``shadow_note_builder``：shadow 模式的摘要生成器，入参为原始
      tool_call 与配对的 tool 结果消息（可能为 None）。
    """

    tool_name: str
    user_turn: str = VISIBILITY_KEEP
    timer_turn: str = VISIBILITY_KEEP
    shadow_note_builder: Callable[[dict, Optional[dict]], str] = field(
        default=_default_shadow_note
    )

    def __post_init__(self):
        for label, mode in (("user_turn", self.user_turn), ("timer_turn", self.timer_turn)):
            if mode not in _VALID_MODES:
                raise ValueError(
                    f"ToolVisibilityRule[{self.tool_name}].{label} 非法模式: {mode!r}，"
                    f"可选 {_VALID_MODES}"
                )


# 扩展点：新增需要按事件源隐藏的工具，在这里加一行规则即可。
# 当前为空：message_user 在 USER / TIMER 两类回合都合法存在，无需折叠。
TOOL_VISIBILITY_RULES: dict[str, ToolVisibilityRule] = {}

# 静默专属工具（开关维度插拔，与事件源无关）：仅在 /show off（静默）
# 回合的工具面里暴露。非静默回合由 get_ai_response 通过
# ``apply_tool_visibility(..., hidden_tools=SILENT_ONLY_TOOLS)`` 把这些
# 工具在出站历史副本中的调用痕迹（assistant 的 tool_calls 与配对的
# tool 消息成对）整体拔除；静默回合不传 hidden_tools，痕迹在原位置
# 原样保留（插回原位置）。持久历史从不被改动。
SILENT_ONLY_TOOLS: frozenset[str] = frozenset({"deliver_reply"})

# =====================================================================
# 核心：出站消息改写（纯函数，绝不原地修改入参）
# =====================================================================
def _rule_mode_for(rule: ToolVisibilityRule, event_source: str) -> str:
    return rule.timer_turn if str(event_source).upper() == "TIMER" else rule.user_turn


def _call_tool_name(tool_call: dict) -> Optional[str]:
    try:
        name = tool_call["function"]["name"]
        return name if isinstance(name, str) and name else None
    except (KeyError, TypeError):
        return None


def _merge_note_into_content(content, note: str):
    """把 shadow 摘要并入 assistant content（兼容 str / None / 多模态 list）。"""
    if content is None or content == "":
        return note
    if isinstance(content, str):
        return f"{content}\n{note}"
    if isinstance(content, list):
        merged = copy.deepcopy(content)
        merged.append({"type": "text", "text": note})
        return merged
    # 未知形状：退化为拼接字符串，保证信息不丢。
    return f"{content}\n{note}"


def apply_tool_visibility(
    messages: list,
    event_source: str = "USER",
    hidden_tools: Optional[Iterable[str]] = None,
) -> list:
    """按事件源 + 开关改写出站消息列表。

    纯函数：返回新列表；未被改写的消息原样引用（零拷贝），被改写的一律
    深拷贝——绝不污染调用方持有的持久历史。无活跃规则且无 hidden_tools
    时直接返回原列表（零开销直通路径）。

    ``hidden_tools``：与事件源无关、一律 DROP 的工具名集合（如非静默
    回合的 ``SILENT_ONLY_TOOLS``）。被拔除的 tool_call 与其配对的 tool
    结果消息总是成对处理，出站消息里不存在悬空 ``tool_call_id``；
    同名的事件源注册表规则优先于 hidden_tools。
    """
    if not TOOL_VISIBILITY_FILTER or not messages:
        return messages

    # 解析本事件源下实际生效的规则（mode != keep 才需要动手）
    active_rules: dict[str, tuple[ToolVisibilityRule, str]] = {}
    for rule in TOOL_VISIBILITY_RULES.values():
        mode = _rule_mode_for(rule, event_source)
        if mode != VISIBILITY_KEEP:
            active_rules[rule.tool_name] = (rule, mode)
    # 开关维度插拔：hidden_tools 一律按 DROP 处理，两个事件源方向相同。
    for name in (hidden_tools or ()):
        if isinstance(name, str) and name and name not in active_rules:
            active_rules[name] = (
                ToolVisibilityRule(
                    tool_name=name,
                    user_turn=VISIBILITY_DROP,
                    timer_turn=VISIBILITY_DROP,
                ),
                VISIBILITY_DROP,
            )
    if not active_rules:
        return messages

    # 预索引：tool_call_id -> 配对 tool 结果消息（shadow 摘要要用，
    # 例如从 "已发送（message_id=123）" 里恢复 message_id）。
    tool_results_by_id: dict[str, dict] = {}
    for msg in messages:
        if (
            isinstance(msg, dict)
            and msg.get("role") == "tool"
            and isinstance(msg.get("tool_call_id"), str)
        ):
            tool_results_by_id[msg["tool_call_id"]] = msg

    # Pass 1：改写含目标工具调用的 assistant 消息，登记被隐藏的 tool_call_id。
    rewritten: list = []
    hidden_call_ids: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            rewritten.append(msg)
            continue
        tool_calls = msg.get("tool_calls")
        if msg.get("role") != "assistant" or not isinstance(tool_calls, list) or not tool_calls:
            rewritten.append(msg)
            continue

        kept_calls: list = []
        shadow_specs: list[tuple[ToolVisibilityRule, dict]] = []
        touched = False
        for tc in tool_calls:
            if not isinstance(tc, dict):
                kept_calls.append(tc)
                continue
            name = _call_tool_name(tc)
            entry = active_rules.get(name) if name else None
            if entry is None:
                kept_calls.append(tc)
                continue
            rule, mode = entry
            tc_id = tc.get("id")
            if isinstance(tc_id, str) and tc_id:
                hidden_call_ids.add(tc_id)
            touched = True
            if mode == VISIBILITY_DROP:
                continue
            shadow_specs.append((rule, tc))  # shadow：折叠为文本摘要

        if not touched:
            rewritten.append(msg)
            continue

        # 深拷贝后再改写：tool_calls 与存储共享引用，禁止原地改动。
        new_msg = copy.deepcopy({k: v for k, v in msg.items() if k != "tool_calls"})
        if kept_calls:
            new_msg["tool_calls"] = copy.deepcopy(kept_calls)
        if shadow_specs:
            notes = [
                rule.shadow_note_builder(
                    tc, tool_results_by_id.get(tc.get("id") or "")
                )
                for rule, tc in shadow_specs
            ]
            new_msg["content"] = _merge_note_into_content(
                new_msg.get("content"), "\n".join(notes)
            )

        # 整条消息折叠后既无文本也无剩余调用：丢弃空壳，避免产生
        # content=None 且无 tool_calls 的非法 assistant 消息。
        if not new_msg.get("tool_calls"):
            content = new_msg.get("content")
            if content is None or content == "" or content == []:
                continue
        rewritten.append(new_msg)

    # Pass 2：移除与被隐藏调用配对的 tool 结果消息，保证配对完整性。
    if not hidden_call_ids:
        return rewritten
    return [
        m
        for m in rewritten
        if not (
            isinstance(m, dict)
            and m.get("role") == "tool"
            and m.get("tool_call_id") in hidden_call_ids
        )
    ]

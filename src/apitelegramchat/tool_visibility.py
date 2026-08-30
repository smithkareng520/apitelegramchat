# tool_visibility.py
"""按事件源（USER / TIMER）控制历史中工具调用的可见性——可拔插的出站消息过滤器。

背景与问题
==========
``proactive.py`` 的 ``send_message_to_user`` 只在 TIMER（后台唤醒）回合的工具面
里暴露，但统一上下文会把 TIMER 回合产生的 ``assistant.tool_calls`` 与配对
``tool`` 结果消息原样沉淀进 ``conversation_history``。用户主动发消息（USER
回合）时，这段历史会原样发给模型：模型看到"历史里这个工具被成功调用过"的
示范，即使本轮 ``tools`` 列表并不包含它，也会高概率模仿调用（OpenAI 兼容
网关对 tools 白名单之外的调用名不做拦截），随后命中
``execute_send_message_to_user`` 的守卫分支。旧版守卫文案以"失败"开头并指示
"请直接把回复内容写给用户即可"，在多步任务中途会被模型理解成"立即收尾
输出"，把进行中的任务打断成提前终止。

本模块在**请求构建**这一侧做改写（挂载点见 ``ai_handlers.get_ai_response``），
按事件源把指定工具的历史痕迹转换形态：

- ``keep``：原样保留（默认，零改动、零开销）；
- ``drop``：tool_call 连同配对 tool 结果一起从出站消息中移除，语义不留痕；
- ``shadow``：把 tool_call 折叠成一条普通 assistant 文本摘要——保留"我此前
  给用户主动发过什么"的上下文连续性（用户后续说"你上次发的那条"时模型仍
  知道），但消除工具调用形状，不再给模型提供可模仿的调用示范；摘要里刻意
  不出现工具名字面量，避免字符串级别的再诱导。

三条硬性保证
============
1. **只改出站副本，绝不改持久历史**：需要改写的消息一律深拷贝。注意
   ``select_request_context`` 返回的是浅拷贝，``tool_calls`` 列表与存储共享
   引用——原地改动会污染 ``conversation_history``，本模块禁止这种行为。
2. **结构合法性**：被移除/折叠的 tool_call 与其配对 tool 消息总是成对处理，
   出站消息里不存在悬空 ``tool_call_id``（否则多数供应商直接 400）。
3. **确定性**：同一份历史在同一事件源下的改写结果逐字节一致。USER 与 TIMER
   两条请求序列各自保持稳定前缀，隐式前缀缓存（DeepSeek/GLM/Gemini 等）在
   各序列内部持续命中；USER 与 TIMER 之间本就因工具面和追加 system 注记
   不同而无法共享缓存，本模块不引入额外退化。

可拔插设计
==========
- 注册表 ``TOOL_VISIBILITY_RULES`` 是唯一扩展点：要隐藏别的工具，加一行
  规则即可；删掉对应条目即恢复旧行为。
- 每条规则分别指定 ``user_turn`` / ``timer_turn`` 两个方向的模式，互不影响；
  TIMER 方向默认 ``keep``，因此 TIMER 回合走零开销直通路径。
- 环境变量 ``TOOL_VISIBILITY_FILTER=false`` 可整体关闭（等价于拔掉本模块，
  与旧版行为逐字节一致）。
- shadow 摘要文案通过规则的 ``shadow_note_builder`` 注入，不同工具可以定制
  各自的摘要生成逻辑。
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

__all__ = [
    "VISIBILITY_KEEP",
    "VISIBILITY_DROP",
    "VISIBILITY_SHADOW",
    "ToolVisibilityRule",
    "TOOL_VISIBILITY_RULES",
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


def _proactive_shadow_note(tool_call: dict, tool_result: Optional[dict]) -> str:
    """send_message_to_user 专用摘要：保留"发过什么"的语义，隐去工具调用形状。

    刻意不输出工具名字面量：模型对历史中的字符串同样敏感，摘要里出现
    "send_message_to_user" 会重新构成字符串级诱导。
    """
    args: dict = {}
    raw_args = (tool_call.get("function") or {}).get("arguments")
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            args = {}
    elif isinstance(raw_args, dict):
        args = raw_args

    action = str(args.get("action") or "send").strip().lower()
    content = args.get("content")

    # 从工具结果文本里恢复 message_id（如 "已发送（message_id=123）：…"），
    # 让摘要保留可追溯性；解析失败不影响摘要生成。
    message_id = None
    if tool_result is not None:
        m = re.search(r"message_id[=＝]\s*(\d+)", str(tool_result.get("content") or ""))
        if m:
            message_id = m.group(1)

    snippet = ""
    if isinstance(content, str) and content.strip():
        snippet = content.strip()
        if len(snippet) > 80:
            snippet = snippet[:80] + "…"

    mid_suffix = f"（message_id={message_id}）" if message_id else ""
    if action == "send":
        if snippet:
            return f"（后台巡检记录：此前已主动向用户发送过消息「{snippet}」{mid_suffix}）"
        return f"（后台巡检记录：此前曾主动向用户发送过消息{mid_suffix}）"
    if action == "edit":
        return f"（后台巡检记录：此前曾编辑过一条主动消息{mid_suffix}）"
    if action == "delete":
        return f"（后台巡检记录：此前曾撤回过一条主动消息{mid_suffix}）"
    return "（后台巡检记录：此前曾通过主动消息通道与用户交互）"


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
TOOL_VISIBILITY_RULES: dict[str, ToolVisibilityRule] = {
    "send_message_to_user": ToolVisibilityRule(
        tool_name="send_message_to_user",
        user_turn=VISIBILITY_SHADOW,
        timer_turn=VISIBILITY_KEEP,
        shadow_note_builder=_proactive_shadow_note,
    ),
}


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
    messages: list, event_source: str = "USER"
) -> list:
    """按事件源改写出站消息列表。

    纯函数：返回新列表；未被改写的消息原样引用（零拷贝），被改写的一律
    深拷贝——绝不污染调用方持有的持久历史。无活跃规则时直接返回原列表
    （TIMER 回合的零开销直通路径）。
    """
    if not TOOL_VISIBILITY_FILTER or not messages:
        return messages

    # 解析本事件源下实际生效的规则（mode != keep 才需要动手）
    active_rules: dict[str, tuple[ToolVisibilityRule, str]] = {}
    for rule in TOOL_VISIBILITY_RULES.values():
        mode = _rule_mode_for(rule, event_source)
        if mode != VISIBILITY_KEEP:
            active_rules[rule.tool_name] = (rule, mode)
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

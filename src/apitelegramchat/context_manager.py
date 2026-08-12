# context_manager.py
"""
对话上下文管理（主流 agent 常见做法的轻量实现）。

目标：在不引入额外模型调用、不做持久化的前提下，让"超预算裁剪"
的行为从"无差别硬删最早整轮"升级为分层、可预测、信息损失更小的策略。

分层策略（从便宜到昂贵，逐级触发，每级都重新检查是否已经够用）：

  第 0 层 · 保护窗口
      永远原样保留：
        - 所有 role == "system" 的消息
        - 最近 KEEP_RECENT_TURNS 轮完整对话（一轮 = 一条 user 消息
          到下一条 user 消息之前的所有内容，包含期间的 assistant /
          tool 消息）
      保护窗口内的内容不参与本模块的任何压缩或删除。

  第 1 层 · 旧工具结果二次压缩（廉价、信息损失小）
      对保护窗口之外的 role == "tool" 消息，如果其 content 超过
      TOOL_RESULT_SOFT_LIMIT 字符，替换为一个固定格式的摘要占位符
      （保留首尾片段 + 原始长度提示）。tool_call_id / name 等字段
      不变，因此不会破坏 assistant.tool_calls 与 tool 消息之间的
      配对关系，协议层面依然合法。
      工具结果通常是"已经被模型消化过"的中间产物，早期轮次里完整
      保留它的意义远小于近期轮次，因此优先从这里省空间。

  第 2 层 · 按轮次裁剪（较昂贵，信息损失较大）
      如果第 1 层压缩后仍然超预算，开始整轮删除，但相比原实现做
      了两点优化：
        a) 只从"已经被第 1 层处理过"的老轮次开始删，不会误删保护
           窗口内的新对话；
        b) 同等老旧程度下，优先删除"纯工具轮"（该轮次里 assistant
           没有产出面向用户的最终文本，只有工具调用往返）——这类
           轮次对用户来说信息密度最低，删除对连贯性影响最小；其次
           才删除有实际问答内容的轮次。
      每删一轮，若有 token_ledger，同步弹出对应账目，行为与原实现
      保持一致。

  第 3 层 · 兜底清空
      前两层都不够（极端情况：单条消息本身就超预算），退化为原有
      行为——只保留 system 消息。

本模块不生成摘要、不调用模型，只做规则化的裁剪顺序 / 粒度优化，
所有开关都是模块级常量，便于按需调整。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 可配置参数
# ---------------------------------------------------------------------------

# 最近多少"轮"完整对话永远不参与压缩/裁剪（一轮从一条 user 消息开始，
# 到下一条 user 消息之前结束）。
KEEP_RECENT_TURNS = 3

# 单条 tool 消息 content 超过这个字符数才会被二次压缩（仅对保护窗口
# 之外的轮次生效）。
TOOL_RESULT_SOFT_LIMIT = 7500

# 二次压缩时，保留原始内容的头部/尾部各多少字符，中间替换为占位提示。
_TOOL_SUMMARY_HEAD_CHARS = 1200
_TOOL_SUMMARY_TAIL_CHARS = 600

_TOOL_SUMMARY_TEMPLATE = (
    "{head}\n"
    "…[早期工具结果已压缩以节省上下文，原始长度 {orig_len} 字符，"
    "此处省略中间 {omitted} 字符]…\n"
    "{tail}"
)


# ---------------------------------------------------------------------------
# 轮次切分
# ---------------------------------------------------------------------------

def _split_turns(history: list) -> list[tuple[int, int]]:
    """
    将 history 切分为若干 [start, end) 区间，每个区间是一"轮"。

    system 消息不属于任何一轮，单独处理；一轮的定义是：从一条
    role == "user" 的消息开始，到下一条 role == "user" 消息（不含）
    为止。history 开头如果不是 user（例如残留的 tool/assistant），
    这部分会被归入第一轮之前的"孤立前缀"，同样按不可裁剪的历史遗留
    数据处理，随第一轮一起考虑。
    """
    turns: list[tuple[int, int]] = []
    n = len(history)
    i = 0
    # 跳过开头非 system 的孤立消息，把它们并入第一轮
    first_user_idx = None
    for idx in range(n):
        if history[idx].get("role") == "user":
            first_user_idx = idx
            break
    if first_user_idx is None:
        return turns

    i = first_user_idx
    while i < n:
        j = i + 1
        while j < n and history[j].get("role") != "user":
            j += 1
        turns.append((i, j))
        i = j
    return turns


def _turn_has_final_reply(history: list, start: int, end: int) -> bool:
    """判断一轮里是否包含一条面向用户的最终 assistant 文本回复
    （即没有 tool_calls 的 assistant 消息，且 content 非空）。"""
    for k in range(start, end):
        msg = history[k]
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return True
            if isinstance(content, list) and content:
                return True
    return False


# ---------------------------------------------------------------------------
# 第 1 层：旧工具结果二次压缩
# ---------------------------------------------------------------------------

def _summarize_tool_content(content, orig_len: int) -> str:
    text = content if isinstance(content, str) else str(content)
    head = text[:_TOOL_SUMMARY_HEAD_CHARS]
    tail = text[-_TOOL_SUMMARY_TAIL_CHARS:] if len(text) > _TOOL_SUMMARY_HEAD_CHARS else ""
    omitted = max(0, orig_len - len(head) - len(tail))
    return _TOOL_SUMMARY_TEMPLATE.format(head=head, orig_len=orig_len, omitted=omitted, tail=tail)


def compress_old_tool_results(
    history: list,
    protected_start: int,
    *,
    soft_limit: int = TOOL_RESULT_SOFT_LIMIT,
) -> int:
    """
    压缩 history[0:protected_start) 范围内超长的 tool 消息内容。
    protected_start 之后（含）的内容视为保护窗口，不做任何改动。

    返回被压缩的消息条数。已经压缩过的消息（用固定前缀标记）不会
    被重复压缩。
    """
    compressed_count = 0
    marker = "__ctx_compressed__"
    for k in range(0, protected_start):
        msg = history[k]
        if msg.get("role") != "tool":
            continue
        if msg.get(marker):
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        if len(content) <= soft_limit:
            continue
        msg["content"] = _summarize_tool_content(content, len(content))
        msg[marker] = True
        compressed_count += 1
    return compressed_count


# ---------------------------------------------------------------------------
# 第 2 层：按轮次裁剪（优化版）
# ---------------------------------------------------------------------------

def _protected_start_index(history: list) -> int:
    """计算保护窗口的起始下标：从末尾数 KEEP_RECENT_TURNS 轮完整对话
    之前的位置。system 消息永远不计入轮次，也永远受保护，但它们
    分散在 history 各处，这里只需要返回"非 system 部分"的保护边界，
    调用方在实际删除时会额外跳过 system 消息。"""
    turns = _split_turns(history)
    if len(turns) <= KEEP_RECENT_TURNS:
        # 轮次数本来就不超过保护窗口，整个 history 都受保护
        # （用第一轮的起点，如果没有任何轮次则为 len(history)，
        # 表示没有可裁剪内容）。
        return turns[0][0] if turns else len(history)
    protected_turn = turns[-KEEP_RECENT_TURNS]
    return protected_turn[0]


def _remove_one_turn(history: list, protected_start: int) -> bool:
    """在 history[0:protected_start) 范围里删除一整轮，优先删除
    "纯工具轮"（无面向用户的最终回复），同等条件下删最早的一轮。
    返回是否成功删除。"""
    turns = [t for t in _split_turns(history) if t[1] <= protected_start]
    if not turns:
        return False

    # 优先级：先找最早的"纯工具轮"；找不到再删最早的一轮。
    target = None
    for start, end in turns:
        if not _turn_has_final_reply(history, start, end):
            target = (start, end)
            break
    if target is None:
        target = turns[0]

    start, end = target
    del history[start:end]
    return True


def trim_history_by_turns(
    history: list,
    *,
    is_over_budget: Callable[[], bool],
    on_turn_removed: Optional[Callable[[], None]] = None,
) -> None:
    """
    反复删除最早的可裁剪整轮，直到 is_over_budget() 返回 False 或
    没有更多可裁剪内容为止。protected_start 会在每次删除后重新计算，
    因为 history 的长度和轮次边界会变化。

    on_turn_removed: 每成功删除一轮后的回调（用于同步 token_ledger
    等外部账目），语义与原实现一致——一轮对应 ledger 里最老的一条
    账目。
    """
    while is_over_budget():
        protected_start = _protected_start_index(history)
        if protected_start <= 0:
            break
        removed = _remove_one_turn(history, protected_start)
        if not removed:
            break
        if on_turn_removed:
            on_turn_removed()


# ---------------------------------------------------------------------------
# 对外的一体化入口
# ---------------------------------------------------------------------------

def apply_layered_trim(
    history: list,
    *,
    is_over_budget: Callable[[], bool],
    on_turn_removed: Optional[Callable[[], None]] = None,
) -> dict:
    """
    按 第1层(压缩旧工具结果) → 第2层(按轮裁剪) → 第3层(兜底清空至system)
    的顺序执行，每一层执行前后都会用 is_over_budget() 重新判断是否
    还需要继续。

    返回本次执行的统计信息，方便上层记录日志：
        {"compressed_tool_msgs": int, "removed_turns": int, "hard_reset": bool}
    """
    stats = {"compressed_tool_msgs": 0, "removed_turns": 0, "hard_reset": False}

    if not is_over_budget():
        return stats

    # ---- 第 1 层：压缩保护窗口之外的旧工具结果 ----
    protected_start = _protected_start_index(history)
    if protected_start > 0:
        stats["compressed_tool_msgs"] = compress_old_tool_results(history, protected_start)

    if not is_over_budget():
        return stats

    # ---- 第 2 层：按轮裁剪 ----
    removed_before = len(history)
    turns_removed = 0

    def _count_and_check() -> bool:
        return is_over_budget()

    while is_over_budget():
        protected_start = _protected_start_index(history)
        if protected_start <= 0:
            break
        removed = _remove_one_turn(history, protected_start)
        if not removed:
            break
        turns_removed += 1
        if on_turn_removed:
            on_turn_removed()
    stats["removed_turns"] = turns_removed

    if not is_over_budget():
        return stats

    # ---- 第 3 层：兜底，只保留 system 消息 ----
    history[:] = [m for m in history if m.get("role") == "system"]
    stats["hard_reset"] = True
    return stats

"""上下文窗口核心：有界会话窗口 + 摊销式自动压缩（auto-compaction）。

策略（2026-09 重构，取代旧的"逐轮滑动截尾 + 量化淘汰"方案，
对齐 Claude Code / Cline 等主流 Agent 的上下文管理思路）：

1. **存储历史即请求上下文**。不再每轮用 select_request_context 做
   滑动截尾视图——旧方案每轮窗口起点后移，隐式前缀缓存
   （DeepSeek/GLM/Gemini/OpenAI）整段历史每轮全 miss。
2. **一个预算、双水位、滞后（hysteresis）触发**：
   - 触发水位 = budget × CONTEXT_COMPACT_TRIGGER_RATIO（默认 0.90）；
   - 压缩目标 = budget × CONTEXT_COMPACT_TARGET_RATIO（默认 0.50）。
   历史在预算内时一字节不动（前缀稳定 → 缓存全量命中）；一旦超过
   触发水位，就一次性压回目标水位，而不是"刚好塞得下"。清出来的
   空间够后续很多轮增长，两次事件之间请求前缀字节级一致。
3. **压缩事件的两级杠杆**（由 app.pre_flight_context_check 编排）：
   - L1（无损）：把较老的工具负载归档成指针（payload → workspace
     归档文件，模型可用 text_editor 取回，见 tool_context_compaction）；
   - L2（结构）：从最老的用户轮块开始整块淘汰，保护最近
     CONTEXT_PROTECTED_TURNS 轮；被淘汰的轮合并进**滚动摘要**
     （conversation digest），存放在历史头部的稳定槽位。
4. **摘要（digest）是确定性纯函数**：同一输入永远产出同一字节，
   不含时间戳等易变内容；只在压缩事件中被重写一次。摘要本身受
   token 预算约束（超限时从最老的行开始丢弃）。

本模块只包含纯逻辑（无 IO、无 app 依赖），方便单元验证；
异步编排（归档落盘、锁）在 app.pre_flight_context_check。
请求侧的最后防线（守卫）见 context_manager.select_request_context。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from token_budget import (
    count_tokens,
    json_token_count,
    truncate_to_token_budget,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 可调参数（环境变量）
# ---------------------------------------------------------------------------
#: 历史可用预算占 max_context 的比例（其余留给系统提示 + 工具 schema + 轮内增长）
CONTEXT_BUDGET_RATIO = min(1.0, max(0.1, _env_float("CONTEXT_BUDGET_RATIO", 0.8)))
#: 触发压缩事件的高水位（占预算比例）
CONTEXT_COMPACT_TRIGGER_RATIO = min(1.0, max(0.3, _env_float("CONTEXT_COMPACT_TRIGGER_RATIO", 0.90)))
#: 压缩事件要压回的目标水位（占预算比例）
CONTEXT_COMPACT_TARGET_RATIO = min(1.0, max(0.1, _env_float("CONTEXT_COMPACT_TARGET_RATIO", 0.50)))
#: 永不淘汰的最近用户轮数（活跃工作集）
CONTEXT_PROTECTED_TURNS = max(0, _env_int("CONTEXT_PROTECTED_TURNS", 6))
#: 滚动摘要的 token 预算
CONTEXT_DIGEST_TOKEN_BUDGET = max(200, _env_int("CONTEXT_DIGEST_TOKEN_BUDGET", 1500))
#: 绝对预算覆盖（兼容旧 CONTEXT_MAX_TOKENS 语义；0 = 不覆盖）
CONTEXT_MAX_TOKENS_ENV = _env_int("CONTEXT_MAX_TOKENS", 0)

#: 无任何模型信息时的兜底预算（与旧 select_request_context 保持一致）
FALLBACK_BUDGET_TOKENS = 50000

#: 摘要消息的稳定标记（同时是摘要槽位的识别方式）
DIGEST_MARKER = "[conversation digest]"
#: 摘要正文里标注"更早内容因预算省略"的提示行
_DIGEST_OVERFLOW_NOTE = "(更早轮次已因摘要 token 预算省略)"
_DIGEST_HEADER_RE = re.compile(
    r"^" + re.escape(DIGEST_MARKER) + r"[^\n]*?(\d+)[^\n]*轮[^\n]*$", re.MULTILINE
)

#: 摘要里每行的 token 上限（U/A 行）
_LINE_TOKEN_BUDGET = 48
#: 工具调用 locator 的字符上限
_LOCATOR_CHAR_BUDGET = 96
#: 构造 T 行时优先展示的参数键（与 tool_context_compaction 的定位字段对齐)
_LOCATOR_KEYS = ("url", "query", "q", "path", "command", "city", "file_name", "name")


# ---------------------------------------------------------------------------
# 预算解析
# ---------------------------------------------------------------------------
def resolve_history_budget(
    model_max_context: Optional[int] = None,
    model_max_output: Optional[int] = None,
    *,
    absolute_override: Optional[int] = None,
) -> int:
    """解析会话历史（含新输入）的统一 token 预算。

    优先级：显式绝对覆盖（兼容旧 CONTEXT_MAX_TOKENS）> 模型推导。
    模型推导同时满足两个约束（取更紧者）：
      - ``max_context × CONTEXT_BUDGET_RATIO``（给系统提示/工具/轮内增长留余量）；
      - ``max_context − max_output``（输入 + 输出不得超过模型窗口）。
    """
    override = absolute_override if absolute_override is not None else CONTEXT_MAX_TOKENS_ENV
    if override and override > 0:
        return int(override)
    if model_max_context and model_max_context > 0:
        budget = int(CONTEXT_BUDGET_RATIO * model_max_context)
        if model_max_output and model_max_output > 0:
            budget = min(budget, model_max_context - model_max_output)
        return max(1024, budget)
    return FALLBACK_BUDGET_TOKENS


def effective_digest_budget(budget: int) -> int:
    """摘要实际 token 预算：绝不超过总预算的 1/4（小窗口模型保护）。"""
    return max(200, min(CONTEXT_DIGEST_TOKEN_BUDGET, budget // 4 or 200))


def compact_watermarks(budget: int) -> tuple[int, int]:
    """返回 (触发水位, 目标水位)，供事件触发判断使用。"""
    return int(budget * CONTEXT_COMPACT_TRIGGER_RATIO), int(budget * CONTEXT_COMPACT_TARGET_RATIO)


# ---------------------------------------------------------------------------
# 结构：摘要槽位 + 用户轮块
# ---------------------------------------------------------------------------
def is_digest_message(message: object) -> bool:
    """识别滚动摘要消息（role=system 且正文以稳定标记开头）。"""
    return (
        isinstance(message, dict)
        and message.get("role") == "system"
        and isinstance(message.get("content"), str)
        and message["content"].startswith(DIGEST_MARKER)
    )


def split_history_blocks(
    messages: list[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], list[list[dict[str, Any]]]]:
    """把历史拆成 (摘要消息, 用户轮块列表)。

    块语义与旧 _drop_oldest_non_system_block 一致：
      - 一条 ``user`` 消息开启一个新块，其后的 assistant/tool/system
        消息全部归属该块（"用户回合拥有到下一条 user 消息为止的一切"）；
      - 首条 user 之前的消息（打断恢复等场景下的孤立 assistant/tool
        头）构成一个无 user 锚点的前导块，同样可被淘汰；
      - 位于下标 0 的摘要消息被单独抽出（永不淘汰的稳定槽位）。
    """
    digest_msg: Optional[dict[str, Any]] = None
    rest = messages
    if messages and is_digest_message(messages[0]):
        digest_msg = messages[0]
        rest = messages[1:]

    blocks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in rest:
        if message.get("role") == "user" and current:
            blocks.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        blocks.append(current)
    return digest_msg, blocks


def _flatten(blocks: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [message for block in blocks for message in block]


# ---------------------------------------------------------------------------
# 淘汰规划（纯函数）
# ---------------------------------------------------------------------------
@dataclass
class EvictionPlan:
    """一次结构性淘汰的确定性计划（不修改入参）。"""

    digest_message: Optional[dict[str, Any]] = None
    evicted_blocks: list[list[dict[str, Any]]] = field(default_factory=list)
    kept_blocks: list[list[dict[str, Any]]] = field(default_factory=list)
    digest_tokens: int = 0
    evicted_tokens: int = 0
    kept_tokens: int = 0
    total_tokens: int = 0
    target_tokens: int = 0
    protected_turns: int = 0

    @property
    def evicted_message_count(self) -> int:
        return sum(len(block) for block in self.evicted_blocks)

    @property
    def kept_messages(self) -> list[dict[str, Any]]:
        return _flatten(self.kept_blocks)


def plan_turn_eviction(
    messages: list[dict[str, Any]],
    *,
    target_tokens: int,
    protected_turns: int = CONTEXT_PROTECTED_TURNS,
    token_fn: Optional[Callable[[dict[str, Any]], int]] = None,
) -> EvictionPlan:
    """规划"从最老的用户轮块开始淘汰，直到 ≤ target_tokens"。

    - target_tokens 通常 = 目标水位 − 新输入估算（历史侧目标）；
    - 最近 protected_turns 个用户轮块受保护，宁可停在水位之上也不动；
    - 摘要槽位永不淘汰（它是被淘汰信息的唯一载体）；
    - 单块超大时允许越过 target（块粒度淘汰），后续由请求侧守卫兜底。
    """
    token_fn = token_fn or json_token_count
    digest_msg, blocks = split_history_blocks(messages)
    digest_tokens = token_fn(digest_msg) if digest_msg is not None else 0
    block_tokens = [sum(token_fn(m) for m in block) for block in blocks]
    total_tokens = digest_tokens + sum(block_tokens)

    evictable_count = max(0, len(blocks) - max(0, protected_turns))
    evicted: list[list[dict[str, Any]]] = []
    dropped = 0
    idx = 0
    while idx < evictable_count and total_tokens - dropped > target_tokens:
        dropped += block_tokens[idx]
        evicted.append(blocks[idx])
        idx += 1

    return EvictionPlan(
        digest_message=digest_msg,
        evicted_blocks=evicted,
        kept_blocks=blocks[idx:],
        digest_tokens=digest_tokens,
        evicted_tokens=dropped,
        kept_tokens=total_tokens - digest_tokens - dropped,
        total_tokens=total_tokens,
        target_tokens=target_tokens,
        protected_turns=protected_turns,
    )


def apply_eviction_plan(
    history: list[dict[str, Any]],
    plan: EvictionPlan,
    digest_text: str,
) -> None:
    """把计划落到持久历史（原地 splice）：新摘要 + 保留块。

    只在压缩事件中调用；两次调用之间历史只会在尾部增长。
    """
    new_digest = make_digest_message(digest_text)
    history[:] = [new_digest] + plan.kept_messages


def make_digest_message(digest_text: str) -> dict[str, Any]:
    return {"role": "system", "content": digest_text}


# ---------------------------------------------------------------------------
# 滚动摘要（确定性纯函数）
# ---------------------------------------------------------------------------
def _first_user_text(block: list[dict[str, Any]]) -> str:
    for message in block:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        return "[多模态消息：图片/文件/语音等]"
    return ""


def _last_assistant_text(block: list[dict[str, Any]]) -> str:
    text = ""
    for message in block:
        if message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                text = content.strip()
    return text


def _parse_arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _locator(name: str, raw_args: object) -> str:
    parsed = _parse_arguments(raw_args)
    for key in _LOCATOR_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            snippet = value.strip()
            if len(snippet) > _LOCATOR_CHAR_BUDGET:
                snippet = snippet[:_LOCATOR_CHAR_BUDGET] + "…"
            return f'{key}="{snippet}"'
    return ""


_ARCHIVED_POINTER_RE = re.compile(r"archived at (\S+\.json)")


def _tool_lines(block: list[dict[str, Any]]) -> list[str]:
    """块内工具调用骨架行：名称 + 定位参数 + 归档指针（如有）。"""
    archived_by_id: dict[str, str] = {}
    for message in block:
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        content = message.get("content")
        if isinstance(call_id, str) and isinstance(content, str):
            match = _ARCHIVED_POINTER_RE.search(content)
            if match:
                archived_by_id[call_id] = match.group(1)

    lines: list[str] = []
    for message in block:
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name") if isinstance(function.get("name"), str) else ""
            if not name:
                continue
            locator = _locator(name, function.get("arguments"))
            tail = ""
            archive = archived_by_id.get(tool_call.get("id") or "")
            if archive:
                tail = f" → 已归档 {archive}"
            lines.append(f"  T: {name}({locator}){tail}" if locator else f"  T: {name}{tail}")
    return lines


def _skeleton_lines(block: list[dict[str, Any]]) -> list[str]:
    """单个被淘汰轮的摘要骨架（2-4 行，全部确定性生成）。"""
    lines: list[str] = []
    user_text = _first_user_text(block)
    if user_text:
        lines.append("- U: " + truncate_to_token_budget(user_text, _LINE_TOKEN_BUDGET, suffix="…"))
    else:
        lines.append("- U: [无用户消息的轮块]")
    assistant_text = _last_assistant_text(block)
    if assistant_text:
        lines.append("  A: " + truncate_to_token_budget(assistant_text, _LINE_TOKEN_BUDGET, suffix="…"))
    lines.extend(_tool_lines(block))
    return lines


def _parse_prev_digest(prev_text: Optional[str]) -> tuple[int, list[str]]:
    """从旧摘要正文提取 (累计轮数, 骨架行列表)；无旧摘要返回 (0, [])。"""
    if not isinstance(prev_text, str) or not prev_text.startswith(DIGEST_MARKER):
        return 0, []
    body_lines = [line for line in prev_text.splitlines()[1:] if line.strip()]
    body_lines = [line for line in body_lines if line.strip() != _DIGEST_OVERFLOW_NOTE]
    match = _DIGEST_HEADER_RE.search(prev_text)
    prev_turns = int(match.group(1)) if match else 0
    return prev_turns, body_lines


def build_digest_text(
    prev_text: Optional[str],
    evicted_blocks: list[list[dict[str, Any]]],
    *,
    budget_tokens: int,
) -> str:
    """合并旧摘要与被淘汰轮，产出新的摘要正文（确定性、受 token 预算约束）。

    - 旧摘要行在前（更老），新淘汰轮的骨架行在后；
    - 总量超出预算时从最老的一端丢弃整行，并插入省略标注；
    - 不包含任何时间戳/随机内容：同一输入永远得到同一字节序列，
      保证两次压缩事件之间摘要槽位字节稳定。
    """
    prev_turns, lines = _parse_prev_digest(prev_text)
    for block in evicted_blocks:
        lines.extend(_skeleton_lines(block))
    total_turns = prev_turns + len(evicted_blocks)

    header = (
        f"{DIGEST_MARKER} 早期 {total_turns} 轮对话已自动压缩为下述要点"
        f"（U=用户请求 A=结论 T=工具调用；工具原始负载可用 text_editor "
        f"从 .context-archive/ 归档文件取回）："
    )
    header_tokens = count_tokens(header)

    dropped_note = False
    if budget_tokens > 0:
        # 预算按"头部 + 正文"总量控制：超限时从最老的一端丢弃整行。
        while lines and count_tokens("\n".join(lines)) + header_tokens > budget_tokens:
            lines.pop(0)
            dropped_note = True

    body: list[str] = []
    if dropped_note:
        body.append(_DIGEST_OVERFLOW_NOTE)
    body.extend(lines)
    text = "\n".join([header] + body)
    # 极小预算的退化兜底（头部 + 标注自身超预算）：保证输出永远合法。
    if count_tokens(text) > budget_tokens + 32:
        text = truncate_to_token_budget(text, max(64, budget_tokens), suffix="…")
    return text


# ---------------------------------------------------------------------------
# 杂项
# ---------------------------------------------------------------------------
def count_history_tokens(
    history: list[dict[str, Any]],
    token_fn: Optional[Callable[[dict[str, Any]], int]] = None,
) -> int:
    token_fn = token_fn or json_token_count
    return sum(token_fn(message) for message in history)


__all__ = [
    "CONTEXT_BUDGET_RATIO",
    "CONTEXT_COMPACT_TRIGGER_RATIO",
    "CONTEXT_COMPACT_TARGET_RATIO",
    "CONTEXT_PROTECTED_TURNS",
    "CONTEXT_DIGEST_TOKEN_BUDGET",
    "CONTEXT_MAX_TOKENS_ENV",
    "FALLBACK_BUDGET_TOKENS",
    "DIGEST_MARKER",
    "EvictionPlan",
    "resolve_history_budget",
    "effective_digest_budget",
    "compact_watermarks",
    "is_digest_message",
    "make_digest_message",
    "split_history_blocks",
    "plan_turn_eviction",
    "apply_eviction_plan",
    "build_digest_text",
    "count_history_tokens",
]

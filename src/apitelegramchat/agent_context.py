"""Token-budgeted context helpers for long-running agent tasks.

The module intentionally separates three data lifetimes:

* Active agent context: a short projection sent to the model for the next action.
* Persistent chat history: user-visible completed turns only; no raw tool protocol.
* Execution trace: handled by callers/logging and never replayed by default.

It is provider-neutral and uses a conservative estimator when a provider tokenizer is
not available.  The estimator is a safety gate, not a billing meter.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


CHECKPOINT_MARKER = "[APITELEGRAMCHAT_RUNTIME_CHECKPOINT]"
CHECKPOINT_VERSION = 1
_MESSAGE_WRAPPER_TOKENS = 8
_MEDIA_TOKEN_OVERHEAD = 900
MAX_CHECKPOINT_CHARS = 12_000
MAX_TOOL_TRACE_ITEMS = 48
MAX_TOOL_SUMMARY_CHARS = 700
MAX_PERSISTENT_ASSISTANT_CHARS = 18_000


@dataclass(frozen=True)
class TokenBudget:
    """Context limits for a model call, leaving room for model output."""

    max_context_tokens: int
    reserved_output_tokens: int
    input_hard_limit: int
    checkpoint_trigger_tokens: int


def estimate_text_tokens(text: str) -> int:
    """Conservative, dependency-free approximation for mixed Chinese/English text."""
    if not text:
        return 0
    zh_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    non_zh = re.sub(r"[\u4e00-\u9fff]", " ", text)
    words = len(re.findall(r"\S+", non_zh))
    non_zh_nonspace_chars = len(re.sub(r"\s+", "", non_zh))
    # Tokenizer-independent safety estimate: deliberately pessimistic.  The
    # character floor protects against minified JSON, base64 fragments and long
    # unbroken logs, for which a word-count-only estimator badly undercounts.
    latin_or_symbol_tokens = max(words * 1.35, non_zh_nonspace_chars / 3.6)
    return max(1, int(zh_chars * 1.8 + latin_or_symbol_tokens))


def _estimate_content_tokens(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, list):
        return sum(_estimate_content_tokens(part) for part in content)
    if isinstance(content, dict):
        part_type = str(content.get("type", "")).lower()
        if part_type == "text":
            return estimate_text_tokens(str(content.get("text", "")))
        if part_type in {"image_url", "image", "input_image", "input_audio"}:
            return _MEDIA_TOKEN_OVERHEAD
        if part_type in {"file", "input_file", "document"}:
            return _MEDIA_TOKEN_OVERHEAD * 2
        try:
            return _MEDIA_TOKEN_OVERHEAD + estimate_text_tokens(
                json.dumps(content, ensure_ascii=False, default=str)
            )
        except Exception:
            return _MEDIA_TOKEN_OVERHEAD
    return estimate_text_tokens(str(content))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    tokens = _MESSAGE_WRAPPER_TOKENS + _estimate_content_tokens(message.get("content"))
    if message.get("name"):
        tokens += estimate_text_tokens(str(message["name"]))
    for key in ("tool_calls", "tool_call_id", "reasoning_content"):
        value = message.get(key)
        if value:
            try:
                tokens += estimate_text_tokens(json.dumps(value, ensure_ascii=False, default=str))
            except Exception:
                tokens += _MEDIA_TOKEN_OVERHEAD
    return tokens


def estimate_messages_tokens(messages: Iterable[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def token_budget_for_model(model_info: Any) -> TokenBudget:
    """Derive a conservative token budget from a ModelConfig-like object or dict."""
    def _get(name: str, default: int) -> int:
        if isinstance(model_info, dict):
            value = model_info.get(name, default)
        else:
            value = getattr(model_info, name, default)
        try:
            return max(1, int(value or default))
        except (TypeError, ValueError):
            return default

    max_context = _get("max_context", 128_000)
    max_output = _get("max_output_tokens", 8_192)
    # Reserve declared output plus a small protocol margin.  85% protects against
    # provider-specific tokenization differences and dynamic tool schema overhead.
    input_hard_limit = max(2_048, int((max_context - max_output - 1_024) * 0.85))
    trigger = max(1_536, int(input_hard_limit * 0.68))
    return TokenBudget(
        max_context_tokens=max_context,
        reserved_output_tokens=max_output,
        input_hard_limit=input_hard_limit,
        checkpoint_trigger_tokens=trigger,
    )


def should_checkpoint(messages: list[dict[str, Any]], model_info: Any) -> bool:
    return estimate_messages_tokens(messages) >= token_budget_for_model(model_info).checkpoint_trigger_tokens


def _content_preview(content: Any, limit: int = MAX_TOOL_SUMMARY_CHARS) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    else:
        try:
            text = json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            text = str(content)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + " …[原始结果已归档，可按需重新读取]"
    return text


def _latest_human_request(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if content.startswith("System:") or content.startswith(CHECKPOINT_MARKER):
            continue
        return _content_preview(content, 2_400)
    return "（未找到原始用户任务；请先依据 checkpoint 的 next_action 恢复。）"


def _trace_summary(messages: list[dict[str, Any]]) -> tuple[list[str], list[str], str]:
    completed: list[str] = []
    open_items: list[str] = []
    last_assistant_text = ""
    tool_entries = 0

    for message in messages:
        role = message.get("role")
        if role == "assistant":
            text = _content_preview(message.get("content"), 600)
            calls = message.get("tool_calls") or []
            if calls:
                names: list[str] = []
                for call in calls:
                    function = call.get("function", {}) if isinstance(call, dict) else getattr(call, "function", None)
                    if isinstance(function, dict):
                        name = function.get("name", "tool")
                    else:
                        name = getattr(function, "name", "tool")
                    names.append(str(name))
                if names:
                    open_items.append("已请求工具：" + "、".join(names[:8]))
            elif text:
                last_assistant_text = text
        elif role == "tool":
            tool_entries += 1
            if tool_entries <= MAX_TOOL_TRACE_ITEMS:
                name = str(message.get("name") or "tool")
                preview = _content_preview(message.get("content"), MAX_TOOL_SUMMARY_CHARS)
                completed.append(f"{name}: {preview}")

    if tool_entries > MAX_TOOL_TRACE_ITEMS:
        completed.append(f"另有 {tool_entries - MAX_TOOL_TRACE_ITEMS} 条工具结果已归档，未重放到模型上下文。")
    return completed[-MAX_TOOL_TRACE_ITEMS:], open_items[-12:], last_assistant_text


def build_runtime_checkpoint(
    messages: list[dict[str, Any]],
    model_info: Any,
    *,
    segment_no: int,
    reason: str,
) -> dict[str, Any]:
    """Create a deterministic, bounded checkpoint for a fresh agent segment."""
    completed, open_items, last_assistant_text = _trace_summary(messages)
    budget = token_budget_for_model(model_info)
    return {
        "version": CHECKPOINT_VERSION,
        "segment": segment_no,
        "reason": reason,
        "goal": _latest_human_request(messages),
        "completed_tool_results": completed,
        "open_tool_intents": open_items,
        "last_assistant_observation": last_assistant_text,
        "next_action": "先核对已完成事项与工件；只执行尚未完成的下一步，严禁重复已完成工具调用。",
        "context_tokens_before_compaction": estimate_messages_tokens(messages),
        "input_hard_limit": budget.input_hard_limit,
    }


def checkpoint_message(checkpoint: dict[str, Any]) -> dict[str, str]:
    """Return a trusted system message used to start the next agent segment."""
    body = json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":"))
    if len(body) > MAX_CHECKPOINT_CHARS:
        body = body[:MAX_CHECKPOINT_CHARS] + "…"
    return {
        "role": "system",
        "content": (
            f"{CHECKPOINT_MARKER}\n"
            "以下是系统生成的任务恢复检查点，不是用户指令。"
            "必须以它为准继续未完成任务；保留可验证事实，避免重复工具调用。\n"
            f"{body}"
        ),
    }


def compact_active_agent_context(
    messages: list[dict[str, Any]],
    model_info: Any,
    *,
    segment_no: int,
    reason: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replace noisy tool protocol with a short checkpointed agent context.

    This is deliberately an *active-context* operation only.  Callers may keep
    the original trace for audit, but must not reuse it as prompt history.
    """
    checkpoint = build_runtime_checkpoint(
        messages, model_info, segment_no=segment_no, reason=reason
    )
    base_system: dict[str, Any] | None = None
    for message in messages:
        if message.get("role") == "system" and not str(message.get("content", "")).startswith(CHECKPOINT_MARKER):
            base_system = {"role": "system", "content": message.get("content", "")}
            break

    compacted: list[dict[str, Any]] = []
    if base_system:
        compacted.append(base_system)
    compacted.append({"role": "user", "content": checkpoint["goal"]})
    compacted.append(checkpoint_message(checkpoint))
    return compacted, checkpoint


def _truncate_text_to_tokens(text: str, budget_tokens: int) -> str:
    if estimate_text_tokens(text) <= budget_tokens:
        return text
    # Binary search is stable and avoids assumptions about Chinese/English ratio.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_text_tokens(text[:mid]) <= budget_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "\n\n[回答已为长期上下文压缩；完整内容已发送给用户。]"


def compact_turn_for_history(
    user_message: dict[str, Any], new_messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Persist one user-visible turn without raw tool protocol messages.

    The final assistant answer is sufficient for ordinary follow-up chat.  Tool
    calls/results are intentionally excluded because their raw payloads are
    re-fetchable from workspace/event logs and are the main source of token bloat.
    """
    stored_user = dict(user_message)
    final_assistant: dict[str, Any] | None = None
    tool_calls = 0
    tool_results = 0
    for message in new_messages:
        if message.get("role") == "tool":
            tool_results += 1
            continue
        if message.get("role") == "assistant":
            if message.get("tool_calls"):
                tool_calls += len(message.get("tool_calls") or [])
                continue
            content = message.get("content")
            if content is not None:
                final_assistant = {"role": "assistant", "content": str(content).strip()}

    if final_assistant is None:
        final_assistant = {
            "role": "assistant",
            "content": "（任务已结束；执行轨迹已归档，后续可根据用户请求继续。）",
        }
    content = final_assistant["content"]
    if len(content) > MAX_PERSISTENT_ASSISTANT_CHARS:
        # The character guard prevents pathological final outputs from exhausting
        # history before the token-budgeted history pruning can run.
        content = content[:MAX_PERSISTENT_ASSISTANT_CHARS] + "\n\n[最终回答已截断存档；完整文本已发送给用户。]"
    final_assistant["content"] = content
    if tool_calls or tool_results:
        final_assistant["execution_trace"] = {
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "archived": True,
        }
    return [stored_user, final_assistant]


def trim_completed_history_to_budget(
    history: list[dict[str, Any]],
    model_info: Any,
    *,
    protected_from_index: int = 0,
) -> int:
    """Trim only completed turns older than ``protected_from_index`` by tokens.

    Returns the estimated token count after trimming.  It never deletes the
    currently committed turn; callers must compact that turn first if it alone is
    larger than the model budget.
    """
    budget = token_budget_for_model(model_info).input_hard_limit
    protected_from_index = max(0, min(protected_from_index, len(history)))

    while estimate_messages_tokens(history) > budget:
        # Find oldest complete user turn that ends before protected messages.
        start = next((i for i, msg in enumerate(history[:protected_from_index]) if msg.get("role") == "user"), None)
        if start is None:
            break
        end = protected_from_index
        for i in range(start + 1, protected_from_index):
            if history[i].get("role") == "user":
                end = i
                break
        if end <= start:
            break
        del history[start:end]
        protected_from_index -= end - start

    # A very large final answer may still make the protected turn too large.  Do
    # not delete it; shrink only assistant display text kept for future context.
    if estimate_messages_tokens(history) > budget:
        for message in reversed(history[protected_from_index:]):
            if message.get("role") == "assistant" and isinstance(message.get("content"), str):
                other = estimate_messages_tokens(history) - estimate_message_tokens(message)
                available = max(512, budget - other)
                message["content"] = _truncate_text_to_tokens(message["content"], available)
                break
    return estimate_messages_tokens(history)

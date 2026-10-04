"""OpenAI 原生 Responses API（/v1/responses）桥接层。

设计原则（官方 server-managed state，单一状态源）：
1. 多轮上下文的唯一载体是服务端 response chain：每个 chat 只保存一个
   ``previous_response_id``，成功创建 response 后原子推进链头。本地
   canonical history 只在 bootstrap（异常恢复）时全量重放；不再维护
   "水位 + 增量历史"镜像，也不再做 manual replay fallback。
2. 普通轮次：``input`` 只携带本回合新增的用户内容（``[user_item]``），
   ``previous_response_id`` 指向上一条成功 response。
3. 工具轮次：从 ``response.completed`` 的权威结构化 ``output`` 识别
   ``function_call``，执行工具后以 ``previous_response_id=response.id``
   + ``input=[function_call_output]`` 续链。工具调用 response 完全可能
   没有最终文本——绝不以 ``output_text == ""`` 判定 response 为空，
   也绝不从 raw_content / assistant 文本 / 历史切片推导 continuation。
4. 严格禁止空 input：调用 SDK 前做协议级 invariant，空 input 直接抛
   ``ResponsesProtocolError``；不用假消息骗过供应商。
5. ``instructions`` 不随 previous_response_id 继承：每轮都从当前
   canonical system prompt 显式传入。
6. 异常状态：只有 response 成功返回且携带有效 ``response.id`` 才推进
   链头；请求失败保持旧链头。previous_response_id 被服务端判定失效时，
   仅用 Responses API 做一次全量 bootstrap 重试；不存在 Chat
   Completions / Conversations fallback。回合中断（请求已出网）后
   显式断链，下一轮 bootstrap。

Responses API 的协议差异（input、function_call item、reasoning 参数、SSE
事件）仍由本模块负责边界转换，内部 agent/tool/history 骨架保持不变。
"""
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from utils import get_logger
from chat_actions import start_chat_action, stop_chat_action

from ai._constants import MAX_TOOL_CALLS
from ai.json_repair import (
    _JSON_REPAIR_NOTE_KEY,
    build_invalid_arguments_envelope,
    repair_json_arguments,
    repair_note_for_result,
)
from ai.tool_summary import (
    _generate_action_description,
    _generate_initial_tool_summary,
    _safe_parse_args,
)
from ai.bridge_common import (
    LiveAssistantSlot,
    SimpleChoice,
    SimpleMessage,
    SimpleResponse,
    SimpleToolCall,
    append_truncation_notice_if_needed,
    ensure_final_content,
    finish_open_tool_group,
    init_bridge_loop_state,
    make_switch_stream,
    over_limit_final_summary,
    run_tool_batch,
)
from ai.cache_usage import _log_cache_usage, usage_num
from ai.streaming import iter_async_stream
from ai.errors import AIResponseProtocolError, ResponsesProtocolError
from ai.response_events import (
    event_field,
    event_type,
    incomplete_reason,
    merge_function_call_item,
    plain_response_item,
    response_error_message,
    response_output_items,
)
from state import get_llm_session_key
import responses_state as _conv_state

if TYPE_CHECKING:
    from ai.draft_manager import DraftManager
    from openai import AsyncOpenAI

from core.messages import Message

logger = get_logger(__name__)


# =============================================================================
# Responses API protocol boundary
# =============================================================================
# Keep the bridge orchestration-focused. Wire-shape conversion lives in the
# provider-neutral helper so it can be exhaustively unit-tested without loading
# the whole agent runtime.
from ai.response_protocol import (
    attach_output_items_to_message,
    messages_to_responses_request,
    output_text_from_items,
    response_function_calls_to_chat,
    tools_to_responses,
)


def _convert_tools_to_responses(tools: Optional[list]) -> Optional[list]:
    return tools_to_responses(tools)


def _convert_messages_to_responses_input(
    messages: list,
    *,
    model: Optional[str] = None,
) -> tuple[str, list]:
    return messages_to_responses_request(messages, model=model)
# =============================================================================
# Responses Prompt Cache：稳定 key + GPT-5.6 原生缓存选项
# =============================================================================
def _responses_prompt_cache_key(api_label: str, model: str, chat_id: Any) -> str:
    """返回与项目现有 LLM session_id 完全相同的 Responses cache key。

    这里不再额外 hash / 改写 session：同一 Telegram 会话使用完全相同的
    ``tg-chat-{chat_id}-{epoch}`` 字符串，同时用于 OpenAI/OpenAI-compatible
    网关的 ``session_id`` 与 Responses API 的 ``prompt_cache_key``。

    ``api_label`` / ``model`` 参数保留在签名中，兼容旧调用点；故意不把它们
    拼进 key，避免模型切换破坏同一会话的缓存桶，也保证与 session_id 一致。
    """
    del api_label, model
    session_key = get_llm_session_key(chat_id if chat_id is not None else None)
    if session_key:
        # 当前 state.py 生成的 session key 远低于 Responses prompt_cache_key
        # 的长度限制；保留最后一道防线，防止未来格式演进导致请求被网关拒绝。
        return session_key[:64]
    return "tg-global"


_RESPONSES_TTL = "30m"


def _add_responses_cache_options(
    request_kwargs: dict[str, Any],
    *,
    api_label: str,
    model: str,
    chat_id: Any,
    enabled: bool = True,
) -> None:
    """注入稳定 key 和自动缓存（implicit 模式）。

    只发送 ``mode=implicit``，兼容只支持自动缓存的中转；implicit 模式
    本身就是按最长匹配前缀自动命中的缓存机制，不需要额外的显式尾部
    断点（是否打断点由模型级 supports_prompt_cache 开关决定）。
    """
    if not enabled:
        return
    request_kwargs["prompt_cache_key"] = _responses_prompt_cache_key(
        api_label, model, chat_id
    )
    # implicit 自动缓存；ttl 当前只有 30m 这一档。
    request_kwargs["prompt_cache_options"] = {
        "mode": "implicit",
        "ttl": _RESPONSES_TTL,
    }


# =============================================================================
# Responses response-chain 状态：只使用 previous_response_id（单一状态源）
# =============================================================================
@dataclass
class _TurnSyncContext:
    """一次 agent turn 内的 Responses 链状态。

    进入回合时 ``response_id`` 是 chat 级已提交链头（mode="chain"）；
    回合内每次收到 ``response.completed`` 后立即更新为该 response 的
    id——同一回合的工具续轮严格沿最新的 completed response 继续。回合
    收尾才把最终 id 提交进 chat 级链头（generation fencing）。
    ``dispatched`` 标记请求是否已出网：出网后异常必须显式断链。
    """

    vendor_key: str
    mode: str  # "chain" | "bootstrap"
    response_id: Optional[str]
    dispatched: bool = False
    rebootstrap_retried: bool = False


def _turn_input_messages(loop_messages: list) -> list:
    """链式正常轮次的新增 input：最后一条 assistant 消息之后的尾部。

    previous_response_id 链已经承载上一回合结束前的全部上下文，本回合
    的新 input 只能是本回合新增的内容（新 user 消息及其后追加的运行时
    system 提示——system 项由协议层汇入 instructions，不进 input）。
    工具续轮的 input 由 ``pending_tool_input_items`` 单独提供，绝不经过
    这里；bootstrap 轮走全量重放，也不经过这里。
    """
    last_assistant = -1
    for idx, msg in enumerate(loop_messages):
        if getattr(msg, "role", None) == "assistant":
            last_assistant = idx
    return list(loop_messages[last_assistant + 1:])


async def _begin_turn_sync(
    chat_id: Any,
    turn: Optional["_conv_state.TurnState"],
    model_info: Any,
    current_model: str,
) -> Optional[_TurnSyncContext]:
    """读取本回合的 Responses 链头（官方 server-managed state）。

    状态解析只依赖 chat 级单链头（vendor_key / model /
    previous_response_id），不扫描本地历史、不计算水位、不维护增量切片。
    链头缺失、模型或端点切换、链被显式作废时进入 bootstrap——这是明确
    的异常恢复路径，不是常规工作模式。
    """
    if chat_id is None or turn is None:
        return None
    try:
        state = await _conv_state.get_response_state(chat_id)
        vendor_key = _conv_state.derive_vendor_key(model_info)
        response_id, mode = state.resolve_chain(vendor_key, current_model)
        ctx = _TurnSyncContext(
            vendor_key=vendor_key, mode=mode, response_id=response_id,
        )
        turn.sync_ctx = ctx
        if mode == "chain":
            logger.info(
                "[openai_responses] chat=%s 续链 previous_response_id=%s vendor=%s",
                chat_id, response_id, vendor_key,
            )
        else:
            logger.info(
                "[openai_responses] chat=%s bootstrap Responses 链（原因=%s）",
                chat_id, mode,
            )
        return ctx
    except Exception:
        logger.warning(
            "[openai_responses] chat=%s Responses 链状态初始化失败",
            chat_id, exc_info=True,
        )
        raise


def _invalidate_on_interrupt(builder: Any, turn: Optional["_conv_state.TurnState"]) -> None:
    """Responses 请求已经发出后若本轮异常，立即断开 response chain。

    未出网时保留旧 chain 是安全的（服务端没有收到任何新内容）；一旦
    服务端已经接受本轮请求，服务端链与本地可能出现半轮差异，下一轮
    必须全量 bootstrap。
    """
    ctx = getattr(turn, "sync_ctx", None) if turn is not None else None
    if ctx is None or not getattr(ctx, "dispatched", False):
        return
    chat_id = getattr(builder, "chat_id", None)
    if chat_id is None:
        return
    try:
        _conv_state.invalidate_response_chain(chat_id, "turn_interrupted_midflight")
        logger.info(
            "[openai_responses] chat=%s 回合中断/异常，断开 response chain vendor=%s",
            chat_id, ctx.vendor_key,
        )
    except Exception:
        logger.debug("[openai_responses] 断开 Responses 链失败（忽略）", exc_info=True)


def _is_previous_response_error(exc: BaseException) -> bool:
    """只识别明确指向 ``previous_response_id`` 的 stale-chain 错误。

    Responses API 的普通 4xx（例如 ``input must be non-empty``、参数校验、
    工具参数错误、模型参数错误）都不能证明 previous_response_id 失效。
    只有错误本身明确描述 previous response / response 不存在时，才允许
    丢弃链并用 canonical history 做一次 Responses bootstrap。
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or status not in (400, 404):
        return False

    # OpenAI SDK / compatible gateway 可能把 detail 放在 body、response 或
    # exception 文本中；统一取可读字符串做严格的关键词判定。
    parts: list[str] = [str(exc)]
    for attr in ("body", "response"):
        value = getattr(exc, attr, None)
        if value is not None:
            try:
                parts.append(json.dumps(value, ensure_ascii=False, default=str))
            except Exception:
                parts.append(str(value))
    text = " ".join(parts).lower()

    stale_markers = (
        "previous_response_id",
        "previous response id",
        "previous response",
        "response not found",
        "response does not exist",
        "unknown response",
        "invalid previous response",
    )
    return any(marker in text for marker in stale_markers)


def _describe_request_shape(request_kwargs: dict[str, Any]) -> str:
    """400 诊断用的请求摘要：只含形状，不含用户内容。"""
    items = []
    for item in request_kwargs.get("input") or []:
        if not isinstance(item, dict):
            items.append(type(item).__name__)
            continue
        kind = item.get("type") or item.get("role") or "?"
        if kind == "function_call_output":
            out = item.get("output")
            kind += f"(call_id={item.get('call_id')!r},output_len={len(out) if isinstance(out, str) else type(out).__name__})"
        elif kind == "function_call":
            kind += f"(call_id={item.get('call_id')!r},id={item.get('id')!r})"
        items.append(kind)
    return (
        f"previous_response_id={request_kwargs.get('previous_response_id')!r} "
        f"input={items} store={request_kwargs.get('store')} "
        f"include={request_kwargs.get('include')} keys={sorted(request_kwargs)}"
    )


def _error_body_text(exc: BaseException, limit: int = 600) -> str:
    body = getattr(exc, "body", None)
    if body is None:
        response = getattr(exc, "response", None)
        body = getattr(response, "text", None)
    try:
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
    except Exception:
        text = str(body)
    return text[:limit]


def _is_empty_input_provider_error(exc: BaseException) -> bool:
    """Recognize the narrow gateway failure seen on native tool continuation.

    A few OpenAI-compatible Responses gateways accept a non-empty native
    ``function_call_output`` item at the client boundary but drop/flatten it
    internally, then reject the resulting request with ``input must be
    non-empty``.  This is *not* a stale ``previous_response_id`` signal.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or status != 400:
        return False
    parts: list[str] = [str(exc)]
    for attr in ("body", "response"):
        value = getattr(exc, attr, None)
        if value is not None:
            try:
                parts.append(json.dumps(value, ensure_ascii=False, default=str))
            except Exception:
                parts.append(str(value))
    text = " ".join(parts).lower()
    return "`input` must be non-empty" in text or "input must be non-empty" in text


# =============================================================================
# 非流式一次性调用：供 subagent_tool.py 复用
# =============================================================================
# 模拟响应对象（Simple* 五件套）由 bridge_common 提供，与 anthropic_bridge
# 共享同一份实现。
async def openai_responses_chat_completions_create(
        client: "AsyncOpenAI",
        *,
        model: str,
        messages: list,
        max_tokens: int,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        tools: Optional[list] = None,
        reasoning: Optional[dict] = None,
        **_ignored: Any,
) -> SimpleResponse:
    """非流式调用 Responses API，返回值形状模拟
    `await client.chat.completions.create(...)` 的返回对象
    （resp.choices[0].message.content / .tool_calls），供
    subagent_tool.py 之类只需要"一次性拿完整结果"的调用方直接复用，
    无需为 Responses API 单独写一套解析逻辑（与
    anthropic_bridge.anthropic_chat_completions_create 同一模式）。
    """
    instructions, input_items = _convert_messages_to_responses_input(messages, model=model)
    responses_tools = _convert_tools_to_responses(tools) if tools else None

    request_kwargs: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "max_output_tokens": max_tokens,
    }
    _add_responses_cache_options(
        request_kwargs, api_label="responses", model=model, chat_id=None, enabled=True
    )
    if instructions:
        request_kwargs["instructions"] = instructions
    if temperature is not None:
        request_kwargs["temperature"] = temperature
    if top_p is not None:
        request_kwargs["top_p"] = top_p
    if reasoning:
        request_kwargs["reasoning"] = reasoning
    if responses_tools:
        request_kwargs["tools"] = responses_tools

    resp = await client.responses.create(**request_kwargs)

    output_items = response_output_items(resp)
    content_text = output_text_from_items(output_items)
    tool_calls = [
        SimpleToolCall(
            call["id"],
            call["function"]["name"],
            call["function"]["arguments"],
        )
        for call in response_function_calls_to_chat(output_items)
    ]

    return SimpleResponse(
        choices=[SimpleChoice(SimpleMessage(content_text, tool_calls))],
        usage=getattr(resp, "usage", None),
    )


# =============================================================================
# usage 归一：Responses ResponseUsage -> OpenAI Chat Completions 形状 dict
# =============================================================================
# 目的：让 _log_cache_usage / app.update_conversation_and_ledger 的既有
# OpenAI 形状消费方无需分支处理（与 anthropic_bridge._anthropic_usage_to_openai
# / gemini_bridge._gemini_usage_to_openai 同一边界转换模式）。
#
# Responses API 的 Usage 字段是 input_tokens / output_tokens /
# output_tokens_details.reasoning_tokens（部分网关还会带
# input_tokens_details.cached_tokens），而 update_conversation_and_ledger
# 只读 prompt_tokens / completion_tokens —— 不归一化的话台账全部落 0。
def _responses_usage_to_openai(usage: Any) -> Optional[dict]:
    if usage is None:
        return None
    try:
        if hasattr(usage, "model_dump"):
            d = usage.model_dump()
        elif isinstance(usage, dict):
            d = dict(usage)
        else:
            d = {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            }
    except Exception:
        logger.debug("_responses_usage_to_openai 归一化失败，丢弃 usage", exc_info=True)
        return None


    input_details = d.get("input_tokens_details") or {}
    cached = None
    cache_write = None
    if isinstance(input_details, dict):
        if "cached_tokens" in input_details:
            cache_val = usage_num(input_details.get("cached_tokens"))
            cached = cache_val
        if "cache_write_tokens" in input_details:
            cache_write = usage_num(input_details.get("cache_write_tokens"))
    prompt = usage_num(d.get("input_tokens"))
    completion = usage_num(d.get("output_tokens"))
    out: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": usage_num(d.get("total_tokens")) or (prompt + completion),
    }
    if cached is not None or cache_write is not None:
        # 归一化给旧有 cache_usage 模块：cached_tokens 是 Responses
        # input_tokens_details.cached_tokens 的同义字段；保留 0，避免把
        # “明确上报了 0”误判成“网关完全没上报缓存字段”。
        out["prompt_tokens_details"] = {}
        if cached is not None:
            out["prompt_tokens_details"]["cached_tokens"] = cached
        if cache_write is not None:
            out["prompt_tokens_details"]["cache_write_tokens"] = cache_write
    return out


# =============================================================================
# 原生 agentic 循环
# =============================================================================
async def _agentic_loop_openai_responses(
        client: "AsyncOpenAI",
        current_model: str,
        messages: list,
        builder: "DraftManager",
        api_label: str = "openai_responses",
        tools: list | None = None,
        supports_tools: bool = True,
        journal: list | None = None,
        workspace_namespace: str | None = None,
        turn: Optional["_conv_state.TurnState"] = None,
) -> tuple[str | None, object | None, list]:
    """OpenAI 原生 Responses API（/v1/responses）专用循环。

    对外契约与 _agentic_loop_openai_compat / _agentic_loop_anthropic /
    _agentic_loop_gemini_native 完全一致：入参/出参（messages、返回的
    new_history_entries）统一为内部 Message（core/messages），只在请求
    Responses API 前做内部 -> 原生协议的边界转换（见模块头注释）。

    ``turn``：本回合的 responses_state.TurnState 快照。提供 turn 时沿
    chat 级 previous_response_id 链头续链（普通轮次 input 只带本回合新增
    用户内容）；没有 turn 时保留独立调用方的无状态全量 Responses 行为
    （例如 subagent_tool 的一次性调用）。

    Responses 请求已经发出后若本轮异常，会显式断开当前 response chain；
    下一轮从 canonical history 全量 bootstrap，避免服务端半轮与本地历史错位。
    """
    try:
        return await _agentic_loop_openai_responses_impl(
            client, current_model, messages, builder,
            api_label=api_label, tools=tools, supports_tools=supports_tools,
            journal=journal, workspace_namespace=workspace_namespace, turn=turn,
        )
    except BaseException:
        _invalidate_on_interrupt(builder, turn)
        raise


async def _agentic_loop_openai_responses_impl(
        client: "AsyncOpenAI",
        current_model: str,
        messages: list,
        builder: "DraftManager",
        api_label: str = "openai_responses",
        tools: list | None = None,
        supports_tools: bool = True,
        journal: list | None = None,
        workspace_namespace: str | None = None,
        turn: Optional["_conv_state.TurnState"] = None,
) -> tuple[str | None, object | None, list]:
    if tools is None:
        from tool_registry import get_model_tools
        tools = await get_model_tools()
    responses_tools = _convert_tools_to_responses(tools) if supports_tools else None

    state = init_bridge_loop_state(messages, journal, current_model)
    loop_messages = state.loop_messages
    new_history_entries = state.new_history_entries
    tool_call_count_ref = state.tool_call_count_ref
    final_content: str | None = state.final_content
    final_usage = state.final_usage
    model_info = state.model_info
    max_tokens = state.max_tokens
    sampling_params = state.sampling_params

    reasoning_param: Optional[dict] = None
    prompt_cache_enabled = bool(model_info and getattr(model_info, "supports_prompt_cache", False))
    effort = getattr(model_info, "reasoning_effort", None) if model_info else None
    if effort:
        # Responses API 顶层 reasoning={"effort": ...}（o-series / gpt-5
        # 系列的事实标准），与 Chat Completions 兼容层的顶层
        # reasoning_effort 字段语义相同，形状不同。
        reasoning_param = {"effort": str(effort).lower()}

    # ---- Responses response chain：读取本回合的 previous_response_id 链头 ----
    chat_id = getattr(builder, "chat_id", None)
    sync_ctx = await _begin_turn_sync(
        chat_id, turn, model_info, current_model,
    )

    # Responses-native tool continuation is deliberately kept separate from
    # canonical-message rendering. After a response emits function_call items
    # (identified from the authoritative structured response.output), the next
    # request MUST carry matching function_call_output items with the same
    # call_id and use that response as previous_response_id. Tool execution may
    # rewrite/replace history entries, so the continuation payload is captured
    # at the wire level instead of being rediscovered through any history diff.
    pending_tool_input_items: list[dict[str, Any]] | None = None

    for _round in range(MAX_TOOL_CALLS):
        # Compatibility recovery is scoped to THIS tool continuation request,
        # not the whole agent turn. A turn may contain multiple function-call ->
        # function_call_output cycles, and each rejected native continuation
        # gets at most one canonical Responses bootstrap.
        tool_continuation_retried = False
        from skills_runtime import refresh_skill_catalog as _refresh_skill_catalog
        _refresh_skill_catalog(loop_messages, builder, workspace_namespace)
        # instructions 不随 previous_response_id 继承：每轮都从当前
        # canonical history 取最新 system prompt（含技能目录等动态段）。
        instructions, _ = _convert_messages_to_responses_input(loop_messages, model=current_model)
        if (
            sync_ctx is not None
            and pending_tool_input_items
            and sync_ctx.mode == "chain"
            and _conv_state.is_tool_continuation_chain_unsupported(
                sync_ctx.vendor_key, current_model
            )
        ):
            # 该 (端点, 模型) 已确认拒绝 previous_response_id + 纯
            # function_call_output：直接用 canonical history（含 function_call
            # 与其 output）bootstrap，不再白发一个必然 400 的请求。
            pending_tool_input_items = None
            sync_ctx.mode = "bootstrap"
            sync_ctx.response_id = None
            if chat_id is not None:
                _conv_state.invalidate_response_chain(
                    chat_id, "tool_continuation_chain_unsupported"
                )
        if sync_ctx is not None:
            if pending_tool_input_items is not None:
                # Official Responses function-calling continuation: send the
                # exact function_call_output items paired with the completed
                # response via call_id — never a reconstructed history delta.
                input_items = [dict(item) for item in pending_tool_input_items]
            elif sync_ctx.mode == "chain":
                # 普通轮次（官方 server-managed state）：input 只带本回合
                # 新增的用户内容，历史上下文由 previous_response_id 承载。
                _, input_items = _convert_messages_to_responses_input(
                    _turn_input_messages(loop_messages), model=current_model
                )
            else:
                # bootstrap（明确的异常恢复路径）：全量重放 canonical
                # history（含 Responses 原生 output item 快照）。
                _, input_items = _convert_messages_to_responses_input(loop_messages, model=current_model)
        else:
            # 无 turn 快照的独立调用方：保持无状态全量行为。
            _, input_items = _convert_messages_to_responses_input(loop_messages, model=current_model)

        # 协议级 invariant：Responses continuation 必须携带非空 input。
        # 在真正调用 SDK 之前失败——绝不发送 input: []，也绝不伪造一条
        # 假消息去满足传输契约。
        if not input_items:
            raise ResponsesProtocolError(
                f"[{api_label}] Responses continuation requires non-empty input; "
                f"mode={sync_ctx.mode if sync_ctx is not None else 'stateless'} "
                f"chat={chat_id}"
            )

        request_kwargs: dict[str, Any] = {
            "model": current_model,
            "input": input_items,
            "stream": True,
            "max_output_tokens": max_tokens,
            # previous_response_id 需要服务端保留 response；显式开启 store，
            # 避免中转网关采用不同默认值导致下一轮无法续链。
            "store": True,
        }
        if sync_ctx is not None and sync_ctx.response_id:
            request_kwargs["previous_response_id"] = sync_ctx.response_id
        _add_responses_cache_options(
            request_kwargs,
            api_label=api_label,
            model=current_model,
            chat_id=builder.chat_id,
            enabled=prompt_cache_enabled,
        )
        if instructions:
            request_kwargs["instructions"] = instructions
        if sampling_params.get("temperature") is not None:
            request_kwargs["temperature"] = sampling_params["temperature"]
        if sampling_params.get("top_p") is not None:
            request_kwargs["top_p"] = sampling_params["top_p"]
        if reasoning_param:
            request_kwargs["reasoning"] = reasoning_param
            # Stateless/bootstrap replay must receive encrypted reasoning
            # content so the original output items can be replayed verbatim.
            request_kwargs["include"] = ["reasoning.encrypted_content"]
        if responses_tools:
            request_kwargs["tools"] = responses_tools
            request_kwargs["tool_choice"] = "auto"
            request_kwargs["parallel_tool_calls"] = True

        content_acc = ""
        reasoning_acc = ""
        # 与下方 response.output_item.done / reasoning 分支配合：标记本轮
        # 是否已经通过 response.reasoning_summary_text.delta 逐块推送过。
        # 每个 reasoning item 开始（output_item.added, type=reasoning）时
        # 重置，item 结束（output_item.done）时读取——同一 item 生命周期
        # 内一一对应，不会跨 item 误判。
        reasoning_seen_via_delta = False
        # 打断保全（改动点1，与 openai_compat / anthropic / gemini 循环同构）：
        # 流式期间 journal 始终持有一条与 content_acc / reasoning_acc 同步的
        # assistant 占位消息；function_call 累积只在流正常结束后由 finalize
        # 写入（改动点2：未完成的调用不入历史）。
        live_slot = LiveAssistantSlot(new_history_entries)
        # output_index -> {"call_id","name","args_json"}（function_call
        # 累积；键用 output_index 而非 item_id，因为 arguments.delta 事件
        # 用 item_id 关联，两者在同一 item 生命周期内一一对应，用哪个做
        # 累积表的 key 都可以，这里统一用 item_id 便于跟 delta 事件直接
        # 命中，output_index 仅用于日志排序）。
        tool_call_items: dict[str, dict] = {}
        # Authoritative native output item snapshots. They are used for replay
        # after a local bootstrap; Chat-style projections are only for the tool
        # executor/UI and are never treated as the canonical Responses shape.
        completed_output_items: list[Any] = []
        completed_item_ids: set[str] = set()
        current_stream_cell = [None]
        response_status: str = ""
        response_error_text: str = ""
        response_terminal = False
        # Never seed this from the previous response on entry. A response id is
        # eligible for continuation only after the current request emits a
        # response.completed event.
        resolved_response_id: Optional[str] = None

        switch_stream = make_switch_stream(builder, current_stream_cell)

        try:
            await start_chat_action(builder.chat_id, "typing")
            try:
                stream = await client.responses.create(**request_kwargs)
            except Exception as create_exc:
                # previous_response_id 过期/不存在是 Responses 自己的状态问题：
                # 不回退到 Conversations，也不切换协议；清掉旧链并用本地
                # canonical history 重新 bootstrap 一次。这个恢复仍然只使用
                # /v1/responses。
                if (
                    sync_ctx is not None
                    and sync_ctx.mode == "chain"
                    and sync_ctx.response_id
                    and not sync_ctx.rebootstrap_retried
                    and _is_previous_response_error(create_exc)
                ):
                    # 官方定义的异常恢复路径：服务端明确判定 previous
                    # response 不存在/不可用时，丢弃链头，仅用 Responses
                    # API 以 canonical history 全量 bootstrap 重试一次。
                    # 普通 4xx 不属于此类；不回退 Conversations，也不切换协议。
                    sync_ctx.rebootstrap_retried = True
                    if chat_id is not None:
                        _conv_state.invalidate_response_chain(
                            chat_id, "previous_response_id_invalid"
                        )
                    sync_ctx.mode = "bootstrap"
                    sync_ctx.response_id = None
                    logger.warning(
                        "[openai_responses] chat=%s previous_response_id=%s 无效（%r），"
                        "仅用 Responses API 全量 bootstrap 重试",
                        chat_id, request_kwargs.get("previous_response_id"), create_exc,
                    )
                    continue

                # Compatibility recovery for gateways that advertise native
                # Responses but drop function_call_output before validating the
                # request.  The local request was non-empty (guarded above),
                # so an "input must be non-empty" 400 here is a provider-side
                # serialization/compatibility failure, not a local empty-input
                # bug.  Rebuild once from canonical history, including the
                # assistant function_call and tool result, and deliberately
                # remove previous_response_id.
                if (
                    pending_tool_input_items is not None
                    and pending_tool_input_items
                    and sync_ctx is not None
                    and sync_ctx.mode == "chain"
                    and sync_ctx.response_id
                    and not tool_continuation_retried
                    and _is_empty_input_provider_error(create_exc)
                ):
                    tool_continuation_retried = True
                    # 留下决定性证据：网关原始错误体 + 实际发出的请求形状。
                    logger.warning(
                        "[openai_responses] chat=%s 工具续轮 400 诊断 status=%s body=%s request=%s",
                        chat_id, getattr(create_exc, "status_code", None),
                        _error_body_text(create_exc), _describe_request_shape(request_kwargs),
                    )
                    _conv_state.mark_tool_continuation_chain_unsupported(
                        sync_ctx.vendor_key, current_model
                    )
                    pending_tool_input_items = None
                    sync_ctx.mode = "bootstrap"
                    sync_ctx.response_id = None
                    if chat_id is not None:
                        _conv_state.invalidate_response_chain(
                            chat_id, "tool_continuation_input_compatibility"
                        )
                    logger.warning(
                        "[openai_responses] chat=%s gateway rejected non-empty "
                        "function_call_output as empty input; one-time canonical "
                        "Responses bootstrap retry",
                        chat_id,
                    )
                    continue
                raise
            if sync_ctx is not None:
                sync_ctx.dispatched = True
                # The pending tool continuation has now been handed to the
                # provider. Never reuse it on a later round.
                pending_tool_input_items = None

            async for event in iter_async_stream(stream):
                etype = event_type(event)

                if etype == "response.created":
                    # Useful for diagnostics; it is not enough to advance the chain.
                    created_response = event_field(event, "response")
                    created_id = event_field(created_response, "id")
                    if created_id:
                        logger.debug(
                            "[openai_responses] chat=%s response.created id=%s",
                            chat_id, created_id,
                        )

                elif etype == "response.output_text.delta":
                    text = event_field(event, "delta", "") or ""
                    if text:
                        content_acc += str(text)
                        await switch_stream("content")
                        builder.append_stream_delta(str(text))
                        live_slot.sync(content_acc, reasoning_acc)

                elif etype == "response.refusal.delta":
                    # Refusal is a message content variant, not an error event.
                    text = event_field(event, "delta", "") or ""
                    if text:
                        content_acc += str(text)
                        await switch_stream("content")
                        builder.append_stream_delta(str(text))
                        live_slot.sync(content_acc, reasoning_acc)

                elif etype == "response.reasoning_summary_text.delta":
                    text = event_field(event, "delta", "") or ""
                    if text:
                        reasoning_acc += str(text)
                        reasoning_seen_via_delta = True
                        await switch_stream("reasoning")
                        builder.append_stream_delta(str(text))
                        live_slot.sync(content_acc, reasoning_acc)

                elif etype == "response.output_item.added":
                    item = event_field(event, "item")
                    itype = event_field(item, "type") if item is not None else None
                    if itype == "reasoning":
                        # Each reasoning item has its own delta lifecycle.
                        reasoning_seen_via_delta = False
                    elif itype == "function_call":
                        item_id = str(
                            event_field(item, "id", "")
                            or f"fc_{uuid.uuid4().hex[:24]}"
                        )
                        call_id = str(event_field(item, "call_id", "") or item_id)
                        name = str(event_field(item, "name", "") or "")
                        tool_call_items[item_id] = {
                            "call_id": call_id,
                            "name": name,
                            "args_json": "",
                        }
                        fn_args: dict[str, Any] = {}
                        summary = _generate_initial_tool_summary(name, fn_args)
                        action_desc = _generate_action_description(name, fn_args)
                        builder.add_tool_item(
                            call_id, name, summary,
                            action_description=action_desc, fn_args=fn_args,
                        )
                        builder.request_flush(force=False)

                elif etype == "response.function_call_arguments.delta":
                    item_id = str(event_field(event, "item_id", "") or "")
                    delta_text = event_field(event, "delta", "") or ""
                    entry = tool_call_items.get(item_id)
                    if entry is not None and delta_text:
                        entry["args_json"] += str(delta_text)
                        if len(entry["args_json"]) % 40 < 4:
                            parsed_args = _safe_parse_args(entry["args_json"])
                            builder.update_tool_args(entry["call_id"], parsed_args)

                elif etype == "response.function_call_arguments.done":
                    item_id = str(event_field(event, "item_id", "") or "")
                    full_args = event_field(event, "arguments", "") or ""
                    entry = tool_call_items.get(item_id)
                    if entry is not None and full_args:
                        # The completed event is the authoritative function-call
                        # argument snapshot when deltas were incomplete.
                        entry["args_json"] = str(full_args)

                elif etype == "response.output_item.done":
                    item = event_field(event, "item")
                    if item is not None:
                        plain = plain_response_item(item)
                        item_id = str(plain.get("id") or "")
                        if item_id:
                            if item_id not in completed_item_ids:
                                completed_item_ids.add(item_id)
                                completed_output_items.append(plain)
                        else:
                            completed_output_items.append(plain)

                    itype = event_field(item, "type") if item is not None else None
                    if itype == "reasoning":
                        if not reasoning_seen_via_delta:
                            summary_list = event_field(item, "summary", None) or []
                            summary_text = "".join(
                                str(event_field(summary, "text", "") or "")
                                for summary in summary_list
                                if event_field(summary, "text", "")
                            )
                            if summary_text:
                                reasoning_acc += summary_text
                                await switch_stream("reasoning")
                                builder.append_stream_delta(summary_text)
                                live_slot.sync(content_acc, reasoning_acc)
                    elif itype == "function_call":
                        delta_item_id, entry, created = merge_function_call_item(
                            tool_call_items, item
                        )
                        if delta_item_id and created and entry is not None:
                            fn_args = _safe_parse_args(entry["args_json"])
                            summary = _generate_initial_tool_summary(entry["name"], fn_args)
                            action_desc = _generate_action_description(entry["name"], fn_args)
                            builder.add_tool_item(
                                entry["call_id"], entry["name"], summary,
                                action_description=action_desc, fn_args=fn_args,
                            )
                            builder.request_flush(force=False)

                elif etype == "response.output_text.done":
                    # Keep the delta path live; the full response snapshot below
                    # remains authoritative once response.completed arrives.
                    text = event_field(event, "text")
                    if not content_acc and text:
                        content_acc = str(text)

                elif etype == "response.reasoning_summary_text.done":
                    # The corresponding output_item.done / completed response remains
                    # the source of truth; this event only supplies a proxy fallback.
                    if not reasoning_acc:
                        text = event_field(event, "text")
                        if text:
                            reasoning_acc = str(text)

                elif etype == "response.completed":
                    response_obj = event_field(event, "response")
                    terminal_status = str(
                        event_field(response_obj, "status", "completed") or "completed"
                    )
                    response_status = terminal_status
                    response_terminal = True

                    if response_obj is not None and event_field(response_obj, "usage"):
                        final_usage = event_field(response_obj, "usage")

                    authoritative_items = response_output_items(response_obj)
                    if authoritative_items:
                        completed_output_items = authoritative_items

                    if terminal_status == "completed" and response_obj is not None:
                        resolved_response_id = event_field(response_obj, "id") or None
                        if sync_ctx is not None and resolved_response_id:
                            # A successful bootstrap/recovery response becomes a
                            # normal server-managed chain head immediately. This
                            # matters inside the SAME agent turn: later tool
                            # continuations must use previous_response_id again,
                            # rather than accidentally staying in replay/bootstrap
                            # mode for the rest of the turn.
                            sync_ctx.response_id = resolved_response_id
                            sync_ctx.mode = "chain"

                elif etype in ("response.failed", "response.incomplete"):
                    response_obj = event_field(event, "response")
                    response_terminal = True
                    if etype == "response.incomplete":
                        response_status = incomplete_reason(
                            response_obj, "incomplete"
                        )
                    else:
                        response_status = "failed"
                    response_error_text = response_error_message(event, response_obj)

                    # A failed/incomplete response is terminal but is not a
                    # successful continuation point. It must never feed partially
                    # generated function arguments back into the tool executor.
                    completed_output_items = []

                elif etype == "error":
                    response_terminal = True
                    response_status = "error"
                    response_error_text = response_error_message(event)
        finally:
            await stop_chat_action(builder.chat_id, "typing")

        if not response_terminal:
            # A readable partial stream is not a valid Responses response
            # continuation point. The official event lifecycle terminates
            # with response.completed / response.failed / response.incomplete
            # (or a top-level error event). Never advance the chain from an
            # otherwise truncated stream.
            if chat_id is not None and sync_ctx is not None:
                _conv_state.invalidate_response_chain(
                    chat_id, "responses_stream_missing_terminal_event"
                )
            raise AIResponseProtocolError(
                f"[{api_label}] Responses stream ended without a terminal event"
            )

        if response_status != "completed":
            # Failed/incomplete/error responses are terminal, but they are not
            # successful tool-call turns and cannot be used as the next
            # previous_response_id. Preserve any visible partial text for the
            # caller, but never execute partially generated function calls.
            tool_calls_list: list[dict] = []
            if not content_acc and not reasoning_acc:
                detail = response_error_text or response_status or "unknown error"
                raise RuntimeError(
                    f"[{api_label}] Responses API error: {detail}"
                )
        else:
            # Once response.completed arrives, response.output is authoritative.
            # Streaming deltas are a UI path only; do not construct executable
            # function calls from a partial delta accumulator.
            authoritative_content = output_text_from_items(completed_output_items)
            if authoritative_content:
                content_acc = authoritative_content

            tool_calls_list = response_function_calls_to_chat(completed_output_items)
            for call in tool_calls_list:
                args_str = call["function"]["arguments"] or "{}"
                try:
                    json.loads(args_str)
                except json.JSONDecodeError:
                    repaired, repair_info = repair_json_arguments(args_str)
                    if isinstance(repaired, dict):
                        note = repair_note_for_result(repair_info.get("fixes") or [])
                        if note:
                            repaired[_JSON_REPAIR_NOTE_KEY] = note
                        args_str = json.dumps(
                            repaired, ensure_ascii=False, separators=(",", ":"))
                        logger.info(
                            "[openai_responses] 工具 %s 参数 JSON 已自动修复（直接用修复后参数执行）",
                            call["function"]["name"],
                        )
                    else:
                        args_str = json.dumps(
                            build_invalid_arguments_envelope(
                                args_str, stream_finish_reason=None),
                            ensure_ascii=False, separators=(",", ":"),
                        )
                        logger.warning(
                            "[openai_responses] 工具 %s 参数 JSON 非法且无法自动修复，已写入带诊断的可恢复错误",
                            call["function"]["name"],
                        )
                call["function"]["arguments"] = args_str

        builder.end_stream()

        final_usage = _responses_usage_to_openai(final_usage) or final_usage
        _log_cache_usage(api_label, final_usage, model_name=current_model)

        # Responses API 的缓存字段在不同网关版本里可能出现在 usage
        # 或 response 本体；把诊断信息单独留下，便于排查“请求开了缓存但
        # 中转没有回传 cached_tokens”的情况。
        if final_usage is not None:
            try:
                if hasattr(final_usage, "model_dump"):
                    usage_dump = final_usage.model_dump()
                elif isinstance(final_usage, dict):
                    usage_dump = dict(final_usage)
                else:
                    usage_dump = {}
                details = usage_dump.get("input_tokens_details") or {}
                cached = details.get("cached_tokens") if isinstance(details, dict) else None
                if cached is not None:
                    logger.debug(
                        "[%s] responses prompt cache: key=%s cached_tokens=%s",
                        api_label,
                        _responses_prompt_cache_key(api_label, current_model, builder.chat_id),
                        cached,
                    )
            except Exception:
                logger.debug("Responses prompt cache diagnostics logging failed", exc_info=True)

        if reasoning_acc:
            builder.finalize_reasoning_block()

        will_request_again = bool(tool_calls_list)
        if will_request_again:
            builder.on_round_boundary()
            builder.request_flush()
        elif not await builder.finalize_turn():
            builder.request_flush()

        logger.info(
            "[AI RAW RESPONSE] provider=%s chat_id=%s length=%s\n%s",
            api_label, builder.chat_id, len(content_acc or ""), content_acc,
        )
        # 纯文本终局截断提示：必须在 live_slot.finalize 之前算出追加后的
        # 文本（与 anthropic_bridge / gemini_bridge 同一修复，理由见
        # bridge_common）。response_status 此前只在事件名为 incomplete/
        # failed 时才有值，现在已改为优先携带 incomplete_details.reason
        # （见上方事件处理分支），"max_output_tokens" 会被
        # _finish_reason_cut_info 按 length/max_tokens 同类归一识别。
        if not tool_calls_list:
            content_acc = append_truncation_notice_if_needed(
                builder, content_acc, response_status)

        # 打断保全（改动点1）：升级 journal 里的实时占位为完整消息
        # （tool_calls / reasoning / 最终文本原地补全，同一对象进 loop_messages）。
        finalized_assistant_msg = live_slot.finalize(loop_messages, content_acc, tool_calls_list, reasoning_acc)
        if response_status == "completed" and completed_output_items:
            attach_output_items_to_message(
                finalized_assistant_msg, completed_output_items, model=current_model
            )
        # 本回合由服务端响应产生的 assistant 消息：其 output item 已随
        # 响应自动进入服务端 response chain，后续轮次绝不重发；本地保留
        # 的原生 output 快照（attach_output_items_to_message）只在
        # bootstrap 重放时使用。

        if not tool_calls_list:
            final_content = content_acc
            finish_open_tool_group(builder)
            await builder.finalize_turn()
            break

        # The next Responses request is a protocol continuation, not another
        # canonical-history diff. Capture exactly the tool result messages that
        # were appended by this batch so their function_call_output items can be
        # sent with the response.completed id above.
        tool_messages_start = len(loop_messages)
        status = await run_tool_batch(builder, tool_calls_list, loop_messages,
                                      new_history_entries, tool_call_count_ref,
                                      api_label, tools)
        # Prefer the exact tool messages produced for this batch.  As a safety
        # net, also scan the canonical list by call_id: some executors can
        # replace an existing placeholder in-place instead of appending a new
        # entry, in which case a pure length-delta slice misses the result.
        expected_call_ids = {
            str(call.get("id") or "")
            for call in tool_calls_list
            if isinstance(call, dict) and call.get("id")
        }
        def _tr_call_id(msg: Message) -> str | None:
            tr = msg.tool_result_block()
            return tr.tool_call_id if tr is not None else None

        new_tool_messages = [
            msg for msg in loop_messages[tool_messages_start:]
            if isinstance(msg, Message)
            and msg.role == "tool"
            and (not expected_call_ids or (_tr_call_id(msg) in expected_call_ids))
        ]
        if expected_call_ids:
            found_ids = {
                tr_id
                for msg in loop_messages
                if isinstance(msg, Message)
                and msg.role == "tool"
                and (tr_id := _tr_call_id(msg)) is not None
                and tr_id in expected_call_ids
            }
            if found_ids != expected_call_ids:
                raise AIResponseProtocolError(
                    f"[{api_label}] Responses tool call missing function_call_output "
                    f"call_ids={sorted(expected_call_ids - found_ids)} "
                    f"response_id={sync_ctx.response_id if sync_ctx else None}"
                )
            if len(new_tool_messages) != len(expected_call_ids):
                new_tool_messages = [
                    msg for msg in loop_messages
                    if isinstance(msg, Message)
                    and msg.role == "tool"
                    and (_tr_call_id(msg) in expected_call_ids)
                ]
        elif not new_tool_messages and status == "continue":
            raise AIResponseProtocolError(
                f"[{api_label}] Responses tool call produced no function_call_output "
                f"messages; cannot continue response_id={sync_ctx.response_id if sync_ctx else None}"
            )

        pending_tool_input_items = []
        for msg in new_tool_messages:
            items = _convert_messages_to_responses_input([msg], model=current_model)[1]
            pending_tool_input_items.extend(items)
        if status == "continue" and not pending_tool_input_items:
            raise AIResponseProtocolError(
                f"[{api_label}] Responses tool continuation rendered empty input "
                f"response_id={sync_ctx.response_id if sync_ctx else None}"
            )
        logger.info(
            "[openai_responses] chat=%s tool continuation response_id=%s "
            "function_call_output=%s call_ids=%s",
            chat_id,
            sync_ctx.response_id if sync_ctx else None,
            len(pending_tool_input_items),
            [item.get("call_id") for item in pending_tool_input_items],
        )

        if status == "over_limit":

            async def _synth_stream(req: tuple) -> str:
                synth_instructions, synth_input = req
                synth_text = ""
                synth_kwargs: dict[str, Any] = {
                    "model": current_model,
                    "input": synth_input,
                    "stream": True,
                    "max_output_tokens": max_tokens,
                    "store": True,
                }
                _add_responses_cache_options(
                    synth_kwargs,
                    api_label=api_label,
                    model=current_model,
                    chat_id=builder.chat_id,
                    enabled=prompt_cache_enabled,
                )
                if synth_instructions:
                    synth_kwargs["instructions"] = synth_instructions
                synth_stream = await client.responses.create(**synth_kwargs)
                async for ev in iter_async_stream(synth_stream):
                    # 事件字段统一经 event_type()/event_field() 读取：
                    # SDK 的流事件可能是模型对象，也可能是原始 dict 形状
                    # （合成总结走独立的 responses.create，网关侧形状不受
                    # 主流同步机制约束）；getattr 会把 dict 事件整体丢弃，
                    # 造成"总结流静默变空"。与主流消费循环（上方 event_type
                    # 分发）保持同一读取协议。
                    if event_type(ev) == "response.output_text.delta":
                        text = event_field(ev, "delta", "") or ""
                        if text:
                            synth_text += text
                            builder.append_stream_delta(text)
                return synth_text

            final_content = await over_limit_final_summary(
                builder, new_history_entries,
                api_label=api_label, loop_name="_agentic_loop_openai_responses",
                build_synth_request=lambda extra: _convert_messages_to_responses_input(
                    loop_messages + [extra], model=current_model),
                stream_synth=_synth_stream,
            )
            # 摘要请求是独立 Responses 请求，不属于主 response chain；
            # 工具结果也已写入本地历史但未进入链。显式断链，下一轮从
            # canonical history bootstrap。
            resolved_response_id = None
            pending_tool_input_items = None
            if chat_id is not None:
                _conv_state.invalidate_response_chain(
                    chat_id, "over_limit_summary_off_chain"
                )
            break
        # status == "continue"：循环自然继续

    if pending_tool_input_items is not None:
        # 工具续轮已准备但回合轮数耗尽、未能发出：这些 function_call_output
        # 尚未进入服务端链，产生 function_call 的 response 不能成为链头，
        # 否则下一轮会把"模型从没见过工具结果"的链当成已同步。
        resolved_response_id = None
        if chat_id is not None:
            _conv_state.invalidate_response_chain(chat_id, "tool_rounds_exhausted")

    final_content = await ensure_final_content(builder, new_history_entries, final_content)

    # ---- Responses response chain：回合结束提交最新 response.id --------
    # 只有 response 真正成功返回并得到有效 response.id 才推进链头（失败
    # 保持旧链头）；generation fencing 保证 /clear 后的迟到回合不能复活
    # 旧 response chain。
    if sync_ctx is not None and chat_id is not None and turn is not None and resolved_response_id:
        try:
            committed = _conv_state.commit_response_sync(
                chat_id, turn,
                vendor_key=sync_ctx.vendor_key,
                response_id=resolved_response_id,
                model=current_model,
            )
            logger.info(
                "[openai_responses] chat=%s Responses 链提交=%s vendor=%s mode=%s "
                "response_id=%s",
                chat_id, committed, sync_ctx.vendor_key, sync_ctx.mode,
                resolved_response_id,
            )
        except Exception:
            # 状态提交失败不会改变已经成功返回的模型结果；下一轮会从
            # canonical history bootstrap，而不是进入任何 fallback 协议。
            # 但链头丢失意味着下一轮全量重发上下文（成本与语义退化），
            # 属状态机异常而非常规路径，warning 保证生产可见。
            logger.warning("[openai_responses] commit_response_sync 失败（下轮将 bootstrap）", exc_info=True)

    return final_content, final_usage, new_history_entries


__all__ = [
    "openai_responses_chat_completions_create",
    "_agentic_loop_openai_responses",
    "_convert_messages_to_responses_input",
    "_convert_tools_to_responses",
    "_responses_usage_to_openai",
    "_begin_turn_sync",
]

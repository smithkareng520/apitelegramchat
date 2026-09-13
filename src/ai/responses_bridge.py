"""OpenAI 原生 Responses API（/v1/responses）桥接层。

设计原则（务必先读，修改本文件前请确保理解，与 anthropic_bridge.py 同构）：
====================================================
1. 全局对话历史（app.py:update_conversation_and_ledger 写入的
   ctx["conversation_history"]）是所有厂商共享的单一存储，且用户可以
   随时在任意两次发言之间切换模型/厂商。因此持久化进历史的消息，
   必须始终是项目原有的 OpenAI **Chat Completions** 形状：
     {"role": "user"/"assistant"/"tool"/"system", "content": ..., ...}
   绝不能把 Responses API 原生的 item 形状（function_call /
   function_call_output / reasoning item）写回 new_history_entries /
   loop_messages 传给调用方。

2. 因此本文件的策略是"边界转换"（与 anthropic_bridge.py /
   gemini_bridge.py 完全一致）：
   - _agentic_loop_openai_responses 接收到的 messages 参数、以及它追加
     进 new_history_entries 的内容，全部是内部 Message（core/messages），
     可以直接复用 tool_call_loop._run_tool_calls_and_append 与
     bridge_common 的公共骨架。
   - 仅在"即将调用 Responses API"之前，把当前累积的内部消息转换成
     Responses 的 input item 列表（_convert_messages_to_responses_input）；
     工具 schema 转换见 _convert_tools_to_responses。
   - Responses API 返回的内容在写回 loop_messages / new_history_entries
     前，统一转换回内部 Message（文本 + tool_calls 列表），与其它两条
     原生桥接完全同构，下游 _run_tool_calls_and_append / turn_recovery /
     update_conversation_and_ledger 都无需改动。

背景（为什么需要专用循环，而不是复用 _agentic_loop_openai_compat）：
====================================================
Responses API 与 Chat Completions 虽同属 OpenAI，但线上协议形状完全
不同：
  - 请求用 `input`（item 列表）而非 `messages`；工具调用结果用
    `function_call_output` item 配对 `call_id`，不是 role=tool 消息；
  - 工具 schema 是扁平的 {"type":"function","name","parameters",...}，
    不是 Chat Completions 的 {"type":"function","function":{...}} 嵌套；
  - 推理参数是顶层 `reasoning: {"effort": ...}`，不是 `reasoning_effort`；
  - 流式事件是一组带 `type` 判别字段的强类型事件
    （response.output_text.delta / response.function_call_arguments.delta
    / response.output_item.added|done / response.completed / ...），
    不是 Chat Completions 的 choices[0].delta 增量合并模型。
把这些差异塞进 _agentic_loop_openai_compat 会让该函数的分支判断进一步
膨胀；按项目既有的原生协议桥接惯例单独实现一份，改动面清晰、互不干扰。
"""
import hashlib
import json
import uuid
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
    ensure_final_content,
    finish_open_tool_group,
    init_bridge_loop_state,
    make_switch_stream,
    over_limit_final_summary,
    run_tool_batch,
)
from ai.cache_usage import _log_cache_usage
from config import RESPONSES_EXPLICIT_CACHE_ENABLED
from state import get_llm_session_key

if TYPE_CHECKING:
    from ai.draft_manager import DraftManager
    from openai import AsyncOpenAI

from core.messages import (
    DocumentBlock, ImageBlock, Message, TextBlock, ToolCallBlock, ToolResultBlock,
)

logger = get_logger(__name__)


# =============================================================================
# 工具 schema 转换：OpenAI Chat Completions 函数调用形状 -> Responses 扁平形状
# =============================================================================
# Chat Completions: {"type": "function", "function": {"name","description","parameters"}}
# Responses:         {"type": "function", "name","description","parameters", "strict"?}
def _convert_tools_to_responses(tools: Optional[list]) -> Optional[list]:
    if not tools:
        return None
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        # 已经是 Responses 扁平形状（无嵌套 function 字段）时直通，兼容
        # 调用方直接传入 Responses 原生工具定义的场景。
        if tool.get("type") == "function" and "function" not in tool and tool.get("name"):
            converted.append(tool)
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = fn.get("name")
        if not name:
            continue
        flat: dict[str, Any] = {
            "type": "function",
            "name": name,
            "description": fn.get("description", "") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        }
        # strict 位（strict_tools_for_request 可能已在 Chat Completions
        # 形状上注入）原样透传到扁平形状的同名字段。
        if fn.get("strict") is not None:
            flat["strict"] = fn.get("strict")
        converted.append(flat)
    return converted or None


# =============================================================================
# 消息格式转换：内部 Message -> Responses input item 列表
# =============================================================================
def _block_to_responses_content_part(block: Any) -> Optional[dict]:
    """内部内容块 -> Responses input content part（user/system 消息内使用）。

    未识别的块类型静默跳过，不中断请求（与 anthropic_bridge 同策略）。
    """
    if isinstance(block, TextBlock):
        return {"type": "input_text", "text": block.text} if block.text else None
    if isinstance(block, ImageBlock):
        url = block.url or ""
        if not url:
            return None
        return {
            "type": "input_image",
            "image_url": url,
            "detail": block.detail or "auto",
        }
    if isinstance(block, DocumentBlock):
        if block.data_url:
            return {
                "type": "input_file",
                "filename": block.filename or "document.pdf",
                "file_data": block.data_url,
            }
        if block.url:
            # Responses API 的 input_file 没有"服务端自行抓取 URL"的
            # source 形态（与 Anthropic document url source 不同）；
            # 退化为把链接当文本提示，交由模型自行判断是否需要工具抓取。
            return {"type": "input_text", "text": f"[document] {block.filename or block.url}: {block.url}"}
        return None
    # AudioBlock / VideoBlock：Responses API 当前无对应事实标准 content
    # part（音频走独立的 Realtime/Audio API），静默跳过而不是抛错。
    return None


def _blocks_to_responses_content(blocks: list) -> list:
    out: list[dict] = []
    for block in blocks:
        part = _block_to_responses_content_part(block)
        if part is not None:
            out.append(part)
    return out


def _convert_messages_to_responses_input(messages: list) -> tuple[str, list]:
    """把内部消息（Message）列表转换成 Responses 的 (instructions, input)。

    规则：
      - role=system -> 拼接进顶层 instructions 字符串（Responses 用
        instructions 承载系统提示，而不是 input 里的 system 消息角色；
        两者语义等价，选 instructions 是因为它在多轮 previous_response_id
        场景下有更清晰的覆盖语义，且与本项目"每轮全量重发"的调用方式
        兼容）。
      - role=user   -> Responses message item（role=user，content 为
        input_text/input_image/input_file part 列表）。
      - role=assistant -> 拆成两类 item：
          * 文本内容 -> message item（role=assistant，content 为
            output_text part）——仅用于把历史文本回填进下一轮输入，
            Responses API 接受把助手历史消息作为 input 回传。
          * 每个 ToolCallBlock -> function_call item
            {"type":"function_call","call_id","name","arguments"}。
        reasoning（ReasoningBlock）不回填：Responses API 的 reasoning
        item 需要服务端签发的 id 才能被同一 response 链路复用，跨轮次
        重新构造的 reasoning 文本无法以合法 item 形式回传，静默跳过
        （与 Anthropic thinking 块在非官方最新模型上的降级策略一致，
        不影响功能，只是模型看不到上一轮的思考过程文本，只看得到结论）。
      - role=tool   -> function_call_output item
        {"type":"function_call_output","call_id","output"}，call_id
        取自 ToolResultBlock.tool_call_id（与产生它的 function_call
        item 的 call_id 必须一致，由 assistant_with_tool_calls 保证
        tool_call_id 全链路透传）。
    """
    instructions_parts: list[str] = []
    input_items: list[dict] = []

    def _as_message(msg: Any) -> Message:
        return msg if isinstance(msg, Message) else Message.from_openai_dict(msg)

    for raw in messages:
        msg = _as_message(raw)
        role = msg.role

        if role == "system":
            text = msg.text()
            if text:
                instructions_parts.append(text)
            continue

        if role == "tool":
            tr = msg.tool_result_block()
            if tr is None:
                continue
            content = tr.content
            output_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            input_items.append({
                "type": "function_call_output",
                "call_id": tr.tool_call_id,
                "output": output_text,
            })
            continue

        if role == "user":
            parts = _blocks_to_responses_content(msg.blocks)
            if parts:
                input_items.append({"type": "message", "role": "user", "content": parts})
            continue

        if role == "assistant":
            text_content = msg.text()
            if text_content:
                input_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text_content}],
                })
            for tc in msg.tool_calls():
                try:
                    args_json = json.dumps(tc.arguments or {}, ensure_ascii=False)
                except (TypeError, ValueError):
                    args_json = "{}"
                input_items.append({
                    "type": "function_call",
                    "call_id": tc.id or f"call_{uuid.uuid4().hex[:24]}",
                    "name": tc.name,
                    "arguments": args_json,
                })
            continue

    instructions = "\n\n".join(p for p in instructions_parts if p)
    return instructions, input_items


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


_RESPONSES_EXPLICIT_CACHE_MARKS = 3
_RESPONSES_TTL = "30m"


def _is_responses_text_content(part: Any) -> bool:
    """显式缓存断点只挂在 Responses 支持 breakpoint 的文本 content block 上。"""
    return isinstance(part, dict) and part.get("type") == "input_text"


def _apply_responses_cache_breakpoints(input_items: list[dict]) -> int:
    """硬编码 3 个显式断点，并保留 Responses 的第 4 个 implicit 断点。

    复刻项目原 Anthropic 显式缓存策略的结构：
      1) 前部 1 个固定断点：稳定锚点，优先覆盖 system/instructions 之后的
         第一段长期不变内容；
      2) 尾部 2 个滚动断点：贴近最近的会话内容/工具回填，支持 agentic loop
         中连续轮次缓存最近前缀。

    注意：Responses API 当前的 prompt_cache_options.ttl 对整次请求统一为
    30m，不能逐断点设置不同 TTL。因此“前长后短”只能通过断点位置复刻
    原策略的缓存层次，不能在同一 request 内真正设置 1h + 5m + 5m。
    返回实际添加的显式断点数。
    """
    candidates: list[tuple[int, int]] = []
    for item_index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            if _is_responses_text_content(part):
                candidates.append((item_index, part_index))

    if not candidates:
        return 0

    # 第一处：固定在最前面的可用文本 block。
    selected: list[tuple[int, int]] = [candidates[0]]

    # 最后两处：从尾部向前取，避免与第一处重复。
    for candidate in reversed(candidates):
        if candidate in selected:
            continue
        selected.append(candidate)
        if len(selected) >= _RESPONSES_EXPLICIT_CACHE_MARKS:
            break

    for item_index, part_index in selected:
        input_items[item_index]["content"][part_index]["prompt_cache_breakpoint"] = {"mode": "explicit"}

    return len(selected)


def _add_responses_cache_options(
    request_kwargs: dict[str, Any],
    *,
    api_label: str,
    model: str,
    chat_id: Any,
    enabled: bool = True,
) -> None:
    """注入稳定 key 和自动缓存；按开关可附加最多 3 个显式断点。

    默认模式只发送 ``mode=implicit``，兼容只支持自动缓存的中转。
    显式模式仍保持 ``implicit + 3 explicit`` 的原策略。
    """
    if not enabled:
        return
    request_kwargs["prompt_cache_key"] = _responses_prompt_cache_key(
        api_label, model, chat_id
    )
    # 默认保留 1 个 implicit 自动断点；若开关打开，调用方会另外写入最多 3 个
    # explicit breakpoint，使总缓存层级为：自动 1 + 手动最多 3。
    # ttl 当前只有 30m 这一档，不能逐 breakpoint 区分长短。
    request_kwargs["prompt_cache_options"] = {
        "mode": "implicit",
        "ttl": _RESPONSES_TTL,
    }


# =============================================================================
# 非流式一次性调用：供 subagent_tool.py 复用（与
# anthropic_bridge.anthropic_chat_completions_create 同一角色）。
# =============================================================================
class _SimpleFunctionCall:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _SimpleToolCall:
    def __init__(self, id_: str, name: str, arguments: str) -> None:
        self.id = id_
        self.function = _SimpleFunctionCall(name, arguments)


class _SimpleMessage:
    """模拟 OpenAI SDK 的 resp.choices[0].message 接口（仅 subagent_tool.py
    实际读取的 .content / .tool_calls 两个属性），让调用方无需分支处理
    Responses 响应即可复用现有解析代码。
    """
    def __init__(self, content: str, tool_calls: list) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _SimpleChoice:
    def __init__(self, message: "_SimpleMessage") -> None:
        self.message = message


class _SimpleResponse:
    def __init__(self, choices: list, usage: Any = None) -> None:
        self.choices = choices
        self.usage = usage


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
) -> _SimpleResponse:
    """非流式调用 Responses API，返回值形状模拟
    `await client.chat.completions.create(...)` 的返回对象
    （resp.choices[0].message.content / .tool_calls），供
    subagent_tool.py 之类只需要"一次性拿完整结果"的调用方直接复用，
    无需为 Responses API 单独写一套解析逻辑（与
    anthropic_bridge.anthropic_chat_completions_create 同一模式）。
    """
    instructions, input_items = _convert_messages_to_responses_input(messages)
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

    content_text = ""
    tool_calls: list[_SimpleToolCall] = []
    for item in (resp.output or []):
        itype = getattr(item, "type", None)
        if itype == "message":
            for part in (getattr(item, "content", None) or []):
                if getattr(part, "type", None) in ("output_text", "text"):
                    content_text += getattr(part, "text", "") or ""
        elif itype == "function_call":
            tool_calls.append(_SimpleToolCall(
                getattr(item, "call_id", "") or f"call_{uuid.uuid4().hex[:24]}",
                getattr(item, "name", "") or "",
                getattr(item, "arguments", "") or "{}",
            ))

    return _SimpleResponse(
        choices=[_SimpleChoice(_SimpleMessage(content_text, tool_calls))],
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

    def _num(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return int(value)

    input_details = d.get("input_tokens_details") or {}
    cached = None
    cache_write = None
    if isinstance(input_details, dict):
        if "cached_tokens" in input_details:
            cache_val = _num(input_details.get("cached_tokens"))
            cached = cache_val
        if "cache_write_tokens" in input_details:
            cache_write = _num(input_details.get("cache_write_tokens"))
    prompt = _num(d.get("input_tokens"))
    completion = _num(d.get("output_tokens"))
    out: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": _num(d.get("total_tokens")) or (prompt + completion),
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
) -> tuple[str | None, object | None, list]:
    """OpenAI 原生 Responses API（/v1/responses）专用循环。

    对外契约与 _agentic_loop_openai_compat / _agentic_loop_anthropic /
    _agentic_loop_gemini_native 完全一致：入参/出参（messages、返回的
    new_history_entries）统一为内部 Message（core/messages），只在请求
    Responses API 前做内部 -> 原生协议的边界转换（见模块头注释）。
    """
    if tools is None:
        from search_engine import SEARCH_TOOLS
        tools = SEARCH_TOOLS
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

    for _round in range(MAX_TOOL_CALLS):
        instructions, input_items = _convert_messages_to_responses_input(loop_messages)
        if RESPONSES_EXPLICIT_CACHE_ENABLED:
            _apply_responses_cache_breakpoints(input_items)

        request_kwargs: dict[str, Any] = {
            "model": current_model,
            "input": input_items,
            "stream": True,
            "max_output_tokens": max_tokens,
        }
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
        current_stream_cell = [None]
        response_status: str = ""
        response_error_text: str = ""

        switch_stream = make_switch_stream(builder, current_stream_cell)

        try:
            await start_chat_action(builder.chat_id, "typing")
            stream = await client.responses.create(**request_kwargs)
            async for event in stream:
                etype = getattr(event, "type", None)

                if etype == "response.output_text.delta":
                    text = getattr(event, "delta", "") or ""
                    if text:
                        content_acc += text
                        await switch_stream("content")
                        builder.append_stream_delta(text)
                        live_slot.sync(content_acc, reasoning_acc)

                elif etype == "response.reasoning_summary_text.delta":
                    # 与 anthropic_bridge 的 thinking_delta 对称：逐块推草稿，
                    # 不等这段 reasoning item 的 .done 事件再整段推送——否则
                    # 思考阶段（往往比正文更耗时）会表现为"卡住不动直到
                    # 整段思考结束才刷新"的明显延迟。reasoning_seen 标记
                    # 该 item 已走过 delta 路径，供 .done 分支避免重复累加。
                    text = getattr(event, "delta", "") or ""
                    if text:
                        reasoning_acc += text
                        reasoning_seen_via_delta = True
                        await switch_stream("reasoning")
                        builder.append_stream_delta(text)
                        live_slot.sync(content_acc, reasoning_acc)

                elif etype == "response.output_item.added":
                    item = getattr(event, "item", None)
                    itype = getattr(item, "type", None) if item is not None else None
                    if itype == "reasoning":
                        # 新的 reasoning item 开始：重置本 item 的 delta 标记
                        # （一轮响应内可能有多个 reasoning item，例如工具调用
                        # 前后各思考一次），避免上一个 item 的标记误判本次。
                        reasoning_seen_via_delta = False
                    elif itype == "function_call":
                        item_id = getattr(item, "id", "") or f"fc_{uuid.uuid4().hex[:24]}"
                        call_id = getattr(item, "call_id", "") or item_id
                        name = getattr(item, "name", "") or ""
                        tool_call_items[item_id] = {
                            "call_id": call_id, "name": name, "args_json": "",
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
                    item_id = getattr(event, "item_id", "") or ""
                    delta_text = getattr(event, "delta", "") or ""
                    entry = tool_call_items.get(item_id)
                    if entry is not None and delta_text:
                        entry["args_json"] += delta_text
                        if len(entry["args_json"]) % 40 < 4:
                            parsed_args = _safe_parse_args(entry["args_json"])
                            builder.update_tool_args(entry["call_id"], parsed_args)

                elif etype == "response.function_call_arguments.done":
                    item_id = getattr(event, "item_id", "") or ""
                    full_args = getattr(event, "arguments", "") or ""
                    entry = tool_call_items.get(item_id)
                    if entry is not None and full_args:
                        # 权威兜底：某些网关只在 .done 事件里给出完整参数
                        # （delta 事件缺失或不完整时），用它覆盖累积值。
                        entry["args_json"] = full_args

                elif etype == "response.output_item.done":
                    item = getattr(event, "item", None)
                    itype = getattr(item, "type", None) if item is not None else None
                    if itype == "reasoning":
                        if reasoning_seen_via_delta:
                            # 已经通过 response.reasoning_summary_text.delta
                            # 逐块推送过本段思考内容，这里不再重复累加/推送
                            # ——否则会把同一段文字在 reasoning_acc 里加两遍。
                            pass
                        else:
                            # 兜底：网关不发 delta、只在 .done 里给出完整
                            # summary 的情况（例如某些非官方中转），退回
                            # 整段推送，保证内容不丢，即便体验上仍是一次性
                            # 到达（无法比网关实际发送的粒度更细）。
                            summary_list = getattr(item, "summary", None) or []
                            summary_text = "\n".join(
                                getattr(s, "text", "") or "" for s in summary_list if getattr(s, "text", "")
                            )
                            if summary_text:
                                reasoning_acc += summary_text
                                await switch_stream("reasoning")
                                builder.append_stream_delta(summary_text)
                                live_slot.sync(content_acc, reasoning_acc)
                    elif itype == "function_call":
                        # 兜底：若前面 .added / .delta 事件因网关差异未触发
                        # （个别兼容层只在 .done 里一次性给出完整 item），
                        # 在这里补建累积表条目，避免该工具调用被漏收。
                        item_id = getattr(item, "id", "") or ""
                        if item_id and item_id not in tool_call_items:
                            call_id = getattr(item, "call_id", "") or item_id
                            name = getattr(item, "name", "") or ""
                            args_json = getattr(item, "arguments", "") or ""
                            tool_call_items[item_id] = {
                                "call_id": call_id, "name": name, "args_json": args_json,
                            }
                            fn_args = _safe_parse_args(args_json)
                            summary = _generate_initial_tool_summary(name, fn_args)
                            action_desc = _generate_action_description(name, fn_args)
                            builder.add_tool_item(
                                call_id, name, summary,
                                action_description=action_desc, fn_args=fn_args,
                            )
                            builder.request_flush(force=False)

                elif etype == "response.completed":
                    resp_obj = getattr(event, "response", None)
                    if resp_obj is not None and getattr(resp_obj, "usage", None):
                        final_usage = resp_obj.usage
                    response_status = "completed"

                elif etype in ("response.failed", "response.incomplete"):
                    resp_obj = getattr(event, "response", None)
                    response_status = etype.rsplit(".", 1)[-1]
                    err = getattr(resp_obj, "error", None) if resp_obj is not None else None
                    if err is not None:
                        response_error_text = getattr(err, "message", "") or str(err)

                elif etype == "error":
                    response_error_text = getattr(event, "message", "") or "unknown error"

        except Exception:
            raise
        finally:
            await stop_chat_action(builder.chat_id, "typing")

        if response_error_text and not content_acc and not reasoning_acc and not tool_call_items:
            # 零输出即失败：直接抛出，交由上层统一的错误提示 / 重试策略
            # 处理（与 openai_compat 循环里网关 4xx/5xx 的处理方式一致，
            # 本桥接不做应用层自动重试——Responses API 网关的瞬时故障重试
            # 语义尚不如 Anthropic 官方文档清晰，保守起见不引入误重试）。
            raise RuntimeError(f"[{api_label}] Responses API error: {response_error_text}")

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

        # 把这一轮的 function_call 累积转换为 OpenAI Chat Completions 形状
        # 的 tool_calls，供 _run_tool_calls_and_append 复用（与另外两条
        # 原生桥接完全同构）。
        tool_calls_list: list[dict] = []
        for item_id in sorted(tool_call_items.keys()):
            entry = tool_call_items[item_id]
            args_str = entry["args_json"] or "{}"
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
                        entry["name"],
                    )
                else:
                    args_str = json.dumps(
                        build_invalid_arguments_envelope(
                            args_str, stream_finish_reason=(response_status or None)),
                        ensure_ascii=False, separators=(",", ":"),
                    )
                    logger.warning(
                        "[openai_responses] 工具 %s 参数 JSON 非法且无法自动修复，已写入带诊断的可恢复错误"
                        "（响应状态 status=%r）",
                        entry["name"], response_status,
                    )
            tool_calls_list.append({
                "id": entry["call_id"], "type": "function",
                "function": {"name": entry["name"], "arguments": args_str},
            })

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
        # 打断保全（改动点1）：升级 journal 里的实时占位为完整消息
        # （tool_calls / reasoning / 最终文本原地补全，同一对象进 loop_messages）。
        live_slot.finalize(loop_messages, content_acc, tool_calls_list, reasoning_acc)

        if not tool_calls_list:
            final_content = content_acc
            finish_open_tool_group(builder)
            await builder.finalize_turn()
            break

        status = await run_tool_batch(builder, tool_calls_list, loop_messages,
                                      new_history_entries, tool_call_count_ref,
                                      api_label, tools)

        if status == "over_limit":

            async def _synth_stream(req: tuple) -> str:
                synth_instructions, synth_input = req
                synth_text = ""
                synth_kwargs: dict[str, Any] = {
                    "model": current_model,
                    "input": synth_input,
                    "stream": True,
                    "max_output_tokens": max_tokens,
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
                async for ev in synth_stream:
                    if getattr(ev, "type", None) == "response.output_text.delta":
                        text = getattr(ev, "delta", "") or ""
                        if text:
                            synth_text += text
                            builder.append_stream_delta(text)
                return synth_text

            final_content = await over_limit_final_summary(
                builder, new_history_entries,
                api_label=api_label, loop_name="_agentic_loop_openai_responses",
                build_synth_request=lambda extra: _convert_messages_to_responses_input(
                    loop_messages + [extra]),
                stream_synth=_synth_stream,
            )
            break
        # status == "continue"：循环自然继续

    final_content = await ensure_final_content(builder, new_history_entries, final_content)

    return final_content, final_usage, new_history_entries


__all__ = [
    "openai_responses_chat_completions_create",
    "_agentic_loop_openai_responses",
    "_convert_messages_to_responses_input",
    "_convert_tools_to_responses",
    "_responses_usage_to_openai",
]

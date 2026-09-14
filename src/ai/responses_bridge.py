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

Responses stateful 增量链（Phase 1，2026-09）：
====================================================
本循环支持 previous_response_id 增量会话（模型配置
responses_stateful=True 时启用，见 ai/responses_state.py）：首轮
bootstrap 全量发送本地历史；后续每轮（同一轮内的工具续链 + 后续
user 轮次）只发送水位之后的增量 input 并携带 previous_response_id 续链。
不变式：
  - 本地 conversation_history 是唯一业务真相，形状与写入节奏完全不变；
  - server state（response_id 链）只是可丢弃的缓存：指针失效时
    create 阶段的 4xx/404 被捕获后自动丢弃指针、从本地历史 bootstrap
    重建重试一次；水位/指纹不对齐时静默回落 bootstrap；
  - instructions（system）每轮全量重发——它是逐请求参数，不进入
    server 链；reasoning item 永不重造重发（由 server state 承载）。
"""
import asyncio
import json
import time
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
    append_truncation_notice_if_needed,
    ensure_final_content,
    finish_open_tool_group,
    init_bridge_loop_state,
    make_switch_stream,
    over_limit_final_summary,
    run_tool_batch,
)
from ai.cache_usage import _log_cache_usage
from ai.responses_state import (
    count_input_visible_messages,
    drop_responses_session,
    fingerprint_synced_prefix,
    get_responses_session,
    resolve_synced_prefix,
    save_responses_session,
)
from config import get_effective_endpoint
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


_RESPONSES_TTL = "30m"

# -----------------------------------------------------------------------------
# Background Mode（Phase 3 长任务）轮询参数
# -----------------------------------------------------------------------------
# background=True 的响应不再有 SSE 连接：create 立即返回（status=queued/
# in_progress），由本模块轮询 retrieve() 到终态。节奏对齐 OpenAI 官方
# background 任务指南：起步 2s 轮询足够（服务端还要排队/推理），总超时
# 15 分钟覆盖最长常规回合；连续轮询失败 5 次视为网关故障（网络抖动在
# 中间轮次自动重试，不中断长任务）。
_RESPONSES_BG_POLL_INTERVAL_SECONDS = 2.0
_RESPONSES_BG_POLL_TIMEOUT_SECONDS = 900.0
_RESPONSES_BG_POLL_MAX_FAILURES = 5


class _SynthEvent:
    """background 终态 Response 的合成流事件。

    getattr 形状与真实 SSE 事件对齐（type / delta / item / item_id /
    arguments / response），让流式路径的事件分发代码原样复用：文本/思考/
    工具卡片/参数修复/截断提示/水位推进全部零改动。缺失属性统一回
    默认值（getattr 的 default 由调用方提供）。
    """

    _raw: dict

    def __init__(self, raw: dict) -> None:
        self._raw = raw

    @property
    def type(self) -> str:
        return self._raw.get("type", "")

    @property
    def delta(self) -> str:
        return self._raw.get("delta", "")

    @property
    def item(self) -> Any:
        return self._raw.get("item")

    @property
    def item_id(self) -> str:
        return self._raw.get("item_id", "")

    @property
    def arguments(self) -> str:
        return self._raw.get("arguments", "")

    @property
    def response(self) -> Any:
        return self._raw.get("response")


class _BackgroundResponseStream:
    """把 background 轮询得到的终态 Response 转成 async 事件流。

    事件形状与真实 SSE 同构（见 _SynthEvent）：message item ->
    output_text.delta（一次性全量文本，background 无打字机语义）；
    reasoning item -> output_item.done（走既有 .done 兑底分支提取
    summary）；function_call -> added + arguments.done + done（与流式
    累积/卡片渲染路径一致）；终态 -> completed / incomplete / failed。
    """

    def __init__(self, response: Any) -> None:
        self._response = response

    def _events(self) -> list[dict[str, Any]]:
        resp = self._response
        status = getattr(resp, "status", None) or "completed"
        events: list = []
        for item in (getattr(resp, "output", None) or []):
            itype = getattr(item, "type", None)
            if itype == "message":
                text = "".join(
                    getattr(part, "text", "") or ""
                    for part in (getattr(item, "content", None) or [])
                    if getattr(part, "type", None) in ("output_text", "text")
                )
                if text:
                    events.append({"type": "response.output_text.delta", "delta": text})
            elif itype == "reasoning":
                events.append({"type": "response.output_item.done", "item": item})
            elif itype == "function_call":
                events.append({"type": "response.output_item.added", "item": item})
                events.append({
                    "type": "response.function_call_arguments.done",
                    "item_id": getattr(item, "id", "") or "",
                    "arguments": getattr(item, "arguments", "") or "",
                })
                events.append({"type": "response.output_item.done", "item": item})
        terminal_type = {
            "completed": "response.completed",
            "incomplete": "response.incomplete",
            "failed": "response.failed",
            "cancelled": "response.failed",
        }.get(status, "response.completed")
        events.append({"type": terminal_type, "response": resp})
        return events

    def __aiter__(self) -> "_BackgroundResponseStream":
        self._iter = iter(self._events())
        return self

    async def __anext__(self) -> _SynthEvent:
        try:
            return _SynthEvent(next(self._iter))
        except StopIteration:
            raise StopAsyncIteration


async def _cancel_response_background(client: "AsyncOpenAI", response_id: Optional[str]) -> None:
    """尽力取消服务端 background 任务（超时/被打断时调用，失败不影响主流程）。"""
    if not response_id:
        return
    try:
        await client.responses.cancel(response_id)
    except Exception:
        logger.debug("Responses background 取消请求失败（忽略）", exc_info=True)


async def _run_response_background_round(
        client: "AsyncOpenAI",
        *,
        request_kwargs: dict[str, Any],
        builder: "DraftManager",
        api_label: str,
) -> Any:
    """Background Mode（Phase 3）：background=True 提交 + 轮询 retrieve 至终态。

    request_kwargs 应已含 background=True 且不含 stream（见
    _build_request_kwargs）。返回终态 Response 对象（status ∈
    completed / incomplete / failed / cancelled），由调用方经
    _BackgroundResponseStream 转成合成事件后复用流式管线。

    长任务语义：
      - 轮询间隔/总超时/连续失败上限见模块顶部常量；
      - 轮询期间经 builder.set_thinking_status 推送心跳（草稿区的
        thinking 状态文本）；Telegram typing 指示不刷新（后台分钟级
        任务里持续刷 typing 无意义，草稿状态即用户可见信号）；
      - 超时：cancel 服务端任务后抛 RuntimeError（走上层统一错误提示）；
      - 被打断（asyncio.CancelledError）：尽力 cancel 后原样传播，
        保留打断保全（LiveAssistantSlot 占位过滤）语义。
    """
    response = await client.responses.create(**request_kwargs)
    round_response_id = getattr(response, "id", None)
    started = time.monotonic()
    deadline = started + _RESPONSES_BG_POLL_TIMEOUT_SECONDS
    consecutive_failures = 0
    status = getattr(response, "status", None) or "queued"
    if status in ("queued", "in_progress") and not round_response_id:
        # 网关接了 background 却没回 id：无法轮询，立即失败而非空转超时。
        raise RuntimeError(
            f"[{api_label}] Responses background 响应缺少 id（status={status}），无法轮询")
    # mypy 收窄：进入轮询后 round_response_id 必为 str（终态直达时不用）。
    poll_response_id: str = round_response_id or ""
    try:
        while status in ("queued", "in_progress"):
            await asyncio.sleep(_RESPONSES_BG_POLL_INTERVAL_SECONDS)
            if time.monotonic() > deadline:
                await _cancel_response_background(client, poll_response_id)
                raise RuntimeError(
                    f"[{api_label}] Responses background 任务超时"
                    f"（>{int(_RESPONSES_BG_POLL_TIMEOUT_SECONDS)}s），"
                    f"response_id={poll_response_id} 已请求取消")
            try:
                response = await client.responses.retrieve(poll_response_id)
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                # 瞬时网络抖动：长任务轮询必须容忍中间失败，连续超限才放弃。
                consecutive_failures += 1
                if consecutive_failures >= _RESPONSES_BG_POLL_MAX_FAILURES:
                    raise
                logger.debug(
                    "[%s] background 轮询瞬时失败（%d/%d）",
                    api_label, consecutive_failures, _RESPONSES_BG_POLL_MAX_FAILURES,
                    exc_info=True,
                )
                continue
            status = getattr(response, "status", None) or status
            # 心跳：草稿区 thinking 状态文本（不打扰正文流）。
            try:
                builder.set_thinking_status(
                    f"Background task {status} ({int(time.monotonic() - started)}s)")
            except Exception:
                logger.debug("background 心跳更新失败（忽略）", exc_info=True)
        return response
    except asyncio.CancelledError:
        await _cancel_response_background(client, poll_response_id)
        raise


def _add_responses_cache_options(
    request_kwargs: dict[str, Any],
    *,
    api_label: str,
    model: str,
    chat_id: Any,
    enabled: bool = True,
) -> None:
    """注入稳定 key 和自动缓存（implicit 模式）。

    只发送 ``mode=implicit``： Responses 的 implicit 缓存是"按最长匹配
    前缀自动命中"的默认行为，兼容只支持自动缓存的中转；不需要额外的
    显式 breakpoint 标记（历史上的 RESPONSES_EXPLICIT_CACHE_ENABLED 显式
    断点机制已移除——除本桥接外没有任何模型/环境启用过它，保留一个
    全局开关只增加分支复杂度；前缀稳定性由 prompt_cache_key +
    stateful 增量输入共同保证）。
    """
    if not enabled:
        return
    request_kwargs["prompt_cache_key"] = _responses_prompt_cache_key(
        api_label, model, chat_id
    )
    # 默认保留 implicit 自动断点；若开关打开，调用方会另外写入最多 2 个
    # explicit breakpoint（system 段首尾）。
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

    模型配置 responses_stateful=True 时启用增量会话（Phase 1）：首轮
    bootstrap 全量、后续增量 + 工具续链 + server state 失效自动重建
    （见模块头注释与 ai/responses_state.py）。
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

    # =========================================================================
    # Responses stateful（Phase 1）：previous_response_id 增量链
    # =========================================================================
    # 本地历史是唯一业务真相；response_id 只是上游上下文指针（见
    # ai/responses_state.py 模块头注释）：
    #   1) 轮次开始：读取 (chat, provider, endpoint, model) 对应的指针，
    #      并用「水位 + 指纹」校验 server 链是否仍与本地历史前缀对齐；
    #   2) 对齐 → 本轮只发送水位之后的增量 input（新 user 消息 / 工具
    #      function_call_output），带 previous_response_id 续链；
    #   3) 不对齐 / 指针失效 → bootstrap 全量发送（本地历史重立新链）。
    # 模型切换天然隔离：键含 provider/endpoint/model，切到没有指针的
    # 模型自动 bootstrap，切回旧模型指纹对齐即可继续增量，无需复制历史。
    responses_stateful = bool(model_info and getattr(model_info, "responses_stateful", False))
    responses_store = getattr(model_info, "responses_store", None) if model_info else None
    # Phase 3 长任务：Background Mode + Prompt Templates（均默认关闭，
    # 逐模型配置开启，语义见 config.ModelConfig 字段注释）。
    responses_background = bool(
        model_info and getattr(model_info, "responses_background", False))
    responses_prompt = getattr(model_info, "responses_prompt", None) if model_info else None
    # state 键的 provider/endpoint：provider 沿用 api_label（协议适配器传入
    # 的 provider key）；endpoint 取合并后的有效端点（同一 provider 下不同
    # 模型可能有模型级端点覆盖，endpoint 参与键避免跨端点串链）。
    state_endpoint = ""
    if model_info is not None:
        try:
            state_endpoint = get_effective_endpoint(model_info).endpoint or ""
        except Exception:
            state_endpoint = ""

    responses_session = None
    if responses_stateful:
        try:
            responses_session = await get_responses_session(
                builder.chat_id, api_label, state_endpoint, current_model)
        except Exception:
            logger.debug("读取 Responses state 失败，按无 state 继续", exc_info=True)
            responses_session = None
    synced_count = 0
    synced_fingerprint = ""
    if responses_session is not None and responses_session.response_id:
        delta_start = resolve_synced_prefix(
            loop_messages,
            responses_session.synced_message_count,
            responses_session.synced_fingerprint,
        )
        if delta_start is not None:
            synced_count = responses_session.synced_message_count
            synced_fingerprint = responses_session.synced_fingerprint
            logger.debug(
                "[responses] stateful 续链：previous_response_id=%s synced=%d delta_start=%d",
                responses_session.response_id, synced_count, delta_start,
            )
        else:
            logger.info(
                "[responses] server state 与本地历史前缀不对齐（水位=%d），本轮 bootstrap 重建",
                responses_session.synced_message_count,
            )
            responses_session = None
    current_response_id: Optional[str] = (
        responses_session.response_id if responses_session is not None else None
    )

    def _build_request_kwargs(
            instructions: str,
            input_items: list,
            previous_response_id: Optional[str],
    ) -> dict[str, Any]:
        """组装单轮请求 kwargs（bootstrap / 增量 / fallback / background 共用）。"""
        kwargs: dict[str, Any] = {
            "model": current_model,
            "input": input_items,
            "max_output_tokens": max_tokens,
        }
        if responses_background:
            # Background Mode（Phase 3 长任务）：非流式提交，由
            # _run_response_background_round 轮询 retrieve() 到终态。
            kwargs["background"] = True
        else:
            kwargs["stream"] = True
        _add_responses_cache_options(
            kwargs,
            api_label=api_label,
            model=current_model,
            chat_id=builder.chat_id,
            enabled=prompt_cache_enabled,
        )
        if responses_prompt is not None:
            # Prompt Templates（Phase 3）：模板自带 system 消息，与
            # instructions 互斥（官方语义），二者只能选其一。
            kwargs["prompt"] = responses_prompt
        elif instructions:
            kwargs["instructions"] = instructions
        if sampling_params.get("temperature") is not None:
            kwargs["temperature"] = sampling_params["temperature"]
        if sampling_params.get("top_p") is not None:
            kwargs["top_p"] = sampling_params["top_p"]
        if reasoning_param:
            kwargs["reasoning"] = reasoning_param
        if responses_tools:
            kwargs["tools"] = responses_tools
            kwargs["tool_choice"] = "auto"
            kwargs["parallel_tool_calls"] = True
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        if responses_store is not None:
            kwargs["store"] = bool(responses_store)
        return kwargs

    for _round in range(MAX_TOOL_CALLS):
        # instructions 每轮都从完整本地历史重算：system prompt 会随技能/
        # 静默模式/TIMER 变化，而 Responses 的 instructions 是逐请求参数，
        # previous_response_id 链不会继承上一请求的 instructions。
        instructions, full_input_items = _convert_messages_to_responses_input(loop_messages)
        # 增量模式：水位之后的非 system 消息转成 delta input；空增量
        # （尾部只有 system 提示等不可见消息）退回全量 bootstrap——
        # Responses 不接受空 input，全量重发永远是最安全的一致状态。
        input_items: list = full_input_items
        previous_response_id: Optional[str] = None
        if current_response_id and synced_count > 0:
            delta_start = resolve_synced_prefix(
                loop_messages, synced_count, synced_fingerprint)
            if delta_start is not None and delta_start < len(loop_messages):
                _, delta_items = _convert_messages_to_responses_input(
                    loop_messages[delta_start:])
                if delta_items:
                    input_items = delta_items
                    previous_response_id = current_response_id
            # delta_start 为 None / 空增量：维持全量 bootstrap（指针暂不
            # 丢，本轮成功后按新水位推进——失败重试天然安全）。

        request_kwargs: dict[str, Any] = _build_request_kwargs(
            instructions, input_items, previous_response_id)

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
        # 本轮响应的 server 端 id（response.completed / response.incomplete
        # 事件携带）：stateful 模式下作为下一轮的 previous_response_id。
        round_response_id: Optional[str] = None

        switch_stream = make_switch_stream(builder, current_stream_cell)

        try:
            await start_chat_action(builder.chat_id, "typing")
            if responses_background:
                # Background Mode（Phase 3）：轮询到终态后转成与 SSE 同构的
                # 合成事件流，下游累积/修复/UI/水位推进管线零改动复用。
                stream = _BackgroundResponseStream(
                    await _run_response_background_round(
                        client, request_kwargs=request_kwargs,
                        builder=builder, api_label=api_label))
            elif previous_response_id:
                # server state 失效（previous_response_id 不可达 / 网关不
                # 支持 / 上游保留期过期）会在 create 阶段直接抛 4xx/404。
                # 捕获后丢弃指针、从本地历史 bootstrap 重试一次——这是
                # 「server state 是缓存不是业务数据」的执行点：本地完整
                # 历史永远足够重建全新链路，不丢任何业务数据。
                try:
                    stream = await client.responses.create(**request_kwargs)
                except Exception as state_exc:
                    logger.warning(
                        "[%s] previous_response_id=%s 请求失败（%s: %s），"
                        "丢弃 server state 并从本地历史 bootstrap 重建",
                        api_label, previous_response_id,
                        type(state_exc).__name__, str(state_exc)[:300],
                    )
                    try:
                        await drop_responses_session(
                            builder.chat_id, api_label, state_endpoint, current_model)
                    except Exception:
                        logger.debug("丢弃 Responses state 失败（忽略）", exc_info=True)
                    responses_session = None
                    current_response_id = None
                    synced_count = 0
                    synced_fingerprint = ""
                    previous_response_id = None
                    request_kwargs = _build_request_kwargs(
                        instructions, full_input_items, None)
                    stream = await client.responses.create(**request_kwargs)
            else:
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
                    if resp_obj is not None:
                        # stateful 增量链的续链指针：completed 响应一定已
                        # 在 server 端落库，可安全作为下一轮的 previous_id。
                        round_response_id = getattr(resp_obj, "id", None) or round_response_id
                        if getattr(resp_obj, "usage", None):
                            final_usage = resp_obj.usage
                    response_status = "completed"

                elif etype in ("response.failed", "response.incomplete"):
                    resp_obj = getattr(event, "response", None)
                    if resp_obj is not None:
                        # incomplete 响应同样已落库（可被 previous_response_id
                        # 引用）；failed 响应的落库状态不可靠，下方推进水位
                        # 时会按 status="failed" 跳过，这里仅作观测记录。
                        round_response_id = getattr(resp_obj, "id", None) or round_response_id
                    # response.incomplete 本身只说"没说完"，真正的截断原因在
                    # response.incomplete_details.reason（"max_output_tokens" /
                    # "content_filter"，见 OpenAI Responses API 文档）。此前
                    # 这里只记了事件名 "incomplete"，既不等于 _finish_reason_
                    # cut_info 认识的任何取值，也丢失了"是输出上限还是内容
                    # 过滤"的区分——下游诊断信封和本轮新增的截断提示都会
                    # 因此永远判定为"未截断"。改为优先读取 details.reason，
                    # 取不到时才退回事件名，保持旧行为不回退。
                    incomplete_details = (
                        getattr(resp_obj, "incomplete_details", None)
                        if resp_obj is not None else None
                    )
                    reason = getattr(incomplete_details, "reason", None) if incomplete_details else None
                    response_status = str(reason) if reason else etype.rsplit(".", 1)[-1]
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
        live_slot.finalize(loop_messages, content_acc, tool_calls_list, reasoning_acc)

        # stateful（Phase 1）：本条 assistant 消息此刻已同时进入本地
        # loop_messages；响应若是 completed/incomplete，其全部 item 也已在
        # server 端落库。把指针与水位推进到当前本地长度——下一轮（工具
        # 续链或后续 user 轮次）只需发送水位之后的增量。
        # response.failed 不推进：失败响应的 server 落库状态不可靠，下一轮
        # 会把本轮局部产物（如有）作为普通 input 随增量重新发送。
        # 水位含本轮 assistant 消息本身（server 链已包含它，无需重发）。
        if responses_stateful and round_response_id and response_status != "failed":
            synced_count = count_input_visible_messages(loop_messages)
            synced_fingerprint = fingerprint_synced_prefix(loop_messages, synced_count)
            current_response_id = round_response_id
            try:
                responses_session = await save_responses_session(
                    builder.chat_id, api_label, state_endpoint, current_model,
                    round_response_id,
                    synced_message_count=synced_count,
                    synced_fingerprint=synced_fingerprint,
                    status=response_status or "completed",
                )
            except Exception:
                # 指针保存失败只影响"跨轮次续链"，本轮内的增量续链
                # （current_response_id / 水位局部变量）不受影响。
                logger.debug("保存 Responses state 失败（不影响本轮结果）", exc_info=True)
                responses_session = None

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
                # 超限强制总结是本 turn 的终局一次性请求：维持无状态
                # 全量 bootstrap（不带 previous_response_id，与 build_
                # synth_request 的全量转换配对）。它不推进水位——总结
                # 产生的 assistant 消息只进历史不入 server 链，下一轮
                # 作为普通增量 input 发送，链路仍然自洽。
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

"""Anthropic 原生 Messages API 桥接层。

设计原则（务必先读，修改本文件前请确保理解）：
====================================================
1. 全局对话历史（app.py:update_conversation_and_ledger 写入的
   ctx["conversation_history"]）是所有厂商共享的单一存储，且用户可以
   随时在任意两次发言之间切换模型/厂商。因此持久化进历史的消息，
   必须始终是项目原有的 OpenAI 兼容形状：
     {"role": "user"/"assistant"/"tool"/"system", "content": ..., ...}
   绝不能把 Anthropic 原生的 content-block 形状（tool_use / tool_result
   块）写回 new_history_entries / loop_messages 传给调用方。

2. 因此本文件的策略是"边界转换"：
   - _agentic_loop_anthropic 接收到的 messages 参数、以及它追加进
     new_history_entries 的内容，全部是 OpenAI 形状（与
     _agentic_loop_openai_compat / _agentic_loop_gemini_native
     完全一致），可以直接复用 tool_call_loop._run_tool_calls_and_append。
   - 仅在"即将调用 Anthropic API"之前，把当前累积的 OpenAI 形状
     loop_messages 转换成 Anthropic 的 {system, messages} 形状
     （_convert_messages_to_anthropic）；工具 schema 转换见
     _convert_tools_to_anthropic。
   - Anthropic 返回的内容在写回 loop_messages / new_history_entries 前，
     统一转换回 OpenAI 形状（content 字符串 + tool_calls 列表），
     与其它两条循环完全同构，下游 _run_tool_calls_and_append /
     turn_recovery / update_conversation_and_ledger 都无需改动。

这样即使用户上一轮用的是 Claude，下一轮切换回 OpenAI 兼容厂商，历史
读出来仍是标准 OpenAI 形状，不会导致任何其它厂商的请求出错——完全
不影响现有项目行为。
"""
import asyncio
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
    append_assistant_message,
    ensure_final_content,
    finish_open_tool_group,
    init_bridge_loop_state,
    make_switch_stream,
    over_limit_final_summary,
    run_tool_batch,
)
from ai.cache_usage import _log_cache_usage

if TYPE_CHECKING:
    from ai.draft_manager import DraftManager
    from anthropic import AsyncAnthropic

from core.messages import (
    DocumentBlock, ImageBlock, Message, TextBlock, ToolCallBlock, ToolResultBlock,
)

logger = get_logger(__name__)


# =============================================================================
# 工具 schema 转换：OpenAI function-calling 形状 -> Anthropic tool 形状
# =============================================================================
# OpenAI: {"type": "function", "function": {"name", "description", "parameters"}}
# Anthropic: {"name", "description", "input_schema"}
def _sanitize_anthropic_tool_schema(schema: Any) -> dict[str, Any]:
    """Normalize JSON schema for Anthropic custom tools.

    Anthropic requires input_schema to be a valid JSON Schema object with a
    top-level type. OpenAI schemas may contain top-level anyOf/oneOf/allOf or
    branches without type, which must be normalized here only.
    """
    import copy

    def normalize(node: Any) -> Any:
        if not isinstance(node, dict):
            return node

        node = copy.deepcopy(node)

        for key in ("oneOf", "anyOf", "allOf"):
            if key in node:
                variants = node.get(key)
                if isinstance(variants, list):
                    for item in variants:
                        if isinstance(item, dict) and item.get("type") == "object":
                            base = {k:v for k,v in node.items() if k not in ("oneOf","anyOf","allOf")}
                            base.update(item)
                            node = base
                            break
                    else:
                        if variants and isinstance(variants[0], dict):
                            node = variants[0]
                        else:
                            node = {"type":"object", "properties":{}}
                else:
                    node = {"type":"object", "properties":{}}
                break

        if "type" not in node:
            if "properties" in node:
                node["type"] = "object"
            else:
                node["type"] = "object"
                node.setdefault("properties", {})

        return node

    result = normalize(schema)
    if not isinstance(result, dict):
        return {"type":"object", "properties":{}}
    return result


def _convert_tools_to_anthropic(tools: Optional[list]) -> Optional[list]:
    if not tools:
        return None
    converted = []
    for tool in tools:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        converted.append({
            "name": name,
            "description": fn.get("description", "") or "",
            "input_schema": _sanitize_anthropic_tool_schema(
                fn.get("parameters") or {"type": "object", "properties": {}}
            ),
        })
    return converted or None


# =============================================================================
# 消息格式转换：OpenAI 形状（role: system/user/assistant/tool）
#             -> Anthropic 形状（顶层 system 字符串 + messages: user/assistant，
#                                 tool 结果作为 user 消息里的 tool_result 块）
# =============================================================================
def _blocks_to_anthropic_content(blocks: list) -> list:
    """把内部内容块列表转换成 Anthropic content 块列表。

    支持 text、ImageBlock（data:base64 内联 / 公开 URL）、DocumentBlock
    （URL source 直通与 base64 source），未识别的块类型静默跳过，不中断
    请求。

    文档块说明（Anthropic 官方限制）：
      - document 块的 url / base64 source 仅接受 PDF（media_type=
        application/pdf）；docx/xlsx 等二进制格式不被 document 块支持，
        官方要求先转成文本或 PDF，因此这里遇到非 PDF 一律跳过
        （attachment 层已保证 anthropic_messages 模型非 PDF 走文本占位）。
      - DocumentBlock(url=...) 直通为 URL source：Anthropic 服务端在请求
        时自行抓取该 URL。
      - DocumentBlock(data_url=...)（data: URI）→ base64 source document 块。
    """
    out_blocks: list[dict] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            if block.text:
                out_blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            url = block.url or ""
            if url.startswith("data:"):
                try:
                    header, b64data = url.split(",", 1)
                    media_type = header.split(";")[0].split(":", 1)[1] or "image/png"
                except (ValueError, IndexError):
                    media_type, b64data = "image/png", ""
                if b64data:
                    out_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64data},
                    })
            elif url:
                # Anthropic 支持公开可访问 URL 直接引用，无需先下载转 base64。
                out_blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        elif isinstance(block, DocumentBlock):
            if block.url:
                block_out: dict = {
                    "type": "document",
                    "source": {"type": "url", "url": block.url},
                }
                if block.filename:
                    block_out["title"] = block.filename
                out_blocks.append(block_out)
            elif block.data_url:
                try:
                    header, b64data = block.data_url.split(",", 1)
                    media_type = header.split(";")[0].split(":", 1)[1] or "application/pdf"
                except (ValueError, IndexError):
                    media_type, b64data = "application/pdf", ""
                if media_type == "application/pdf" and b64data:
                    doc = {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64data,
                        },
                    }
                    if block.filename:
                        doc["title"] = block.filename
                    out_blocks.append(doc)
                else:
                    logger.info(
                        "anthropic 转换：内联文档非 PDF（media_type=%s），"
                        "无法转为 document 块，已跳过",
                        media_type,
                    )
        # 其它块类型（audio/video 等）Anthropic Messages API 暂不支持，
        # 静默跳过而不是抛错中断整轮请求。
    return out_blocks


def _convert_messages_to_anthropic(messages: list) -> tuple[str, list]:
    """把内部消息（Message）列表转换成 Anthropic 的 (system_prompt, messages)。

    规则：
      - role=system -> 拼接进顶层 system 字符串（Anthropic 无 system 角色消息）
      - role=user   -> Anthropic user 消息（blocks 转换为块列表）
      - role=assistant -> Anthropic assistant 消息；ToolCallBlock 渲染为
        tool_use 块（input 为结构化 dict）
      - role=tool   -> ToolResultBlock 追加/合并进下一个 Anthropic user
        消息的 tool_result 块（Anthropic 要求 tool_result 必须放在 user
        消息里，且通常紧跟在触发它的 assistant tool_use 消息之后）
    """
    system_parts: list[str] = []
    anthropic_messages: list[dict] = []
    pending_tool_results: list[dict] = []

    def _flush_pending_tool_results() -> None:
        if pending_tool_results:
            anthropic_messages.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    def _as_message(msg: Any) -> Message:
        return msg if isinstance(msg, Message) else Message.from_openai_dict(msg)

    for raw in messages:
        msg = _as_message(raw)
        role = msg.role
        if role == "system":
            text = msg.text()
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            # tool 结果必须归入下一条 user 消息；先攒着，遇到下一个非 tool
            # 消息（或结尾）时统一 flush。
            tr = msg.tool_result_block()
            if tr is None:
                continue
            text = tr.content if isinstance(tr.content, str) else json.dumps(tr.content, ensure_ascii=False)
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": tr.tool_call_id,
                "content": [{"type": "text", "text": text}],
            })
            continue

        # 非 tool 消息出现之前，先把攒的 tool_result 落成一条 user 消息。
        _flush_pending_tool_results()

        if role == "user":
            blocks = _blocks_to_anthropic_content(msg.blocks)
            if blocks:
                anthropic_messages.append({"role": "user", "content": blocks})
            continue

        if role == "assistant":
            blocks: list[dict] = []
            text_content = msg.text()
            if text_content:
                blocks.append({"type": "text", "text": text_content})
            for tc in msg.tool_calls():
                blocks.append({
                    "type": "tool_use",
                    "id": tc.id or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": tc.name,
                    "input": tc.arguments if isinstance(tc.arguments, dict) else {},
                })
            if blocks:
                anthropic_messages.append({"role": "assistant", "content": blocks})
            continue

    _flush_pending_tool_results()

    system_prompt = "\n\n".join(p for p in system_parts if p)
    return system_prompt, anthropic_messages


# =============================================================================
# 非流式一次性调用：供 subagent_tool.py 复用（子 agent 是后台任务，不需要
# 流式增量，与其原有 "普通 chat.completions.create" 语义对齐）。
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
    Anthropic 响应即可复用现有解析代码。
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


async def anthropic_chat_completions_create(
        client: "AsyncAnthropic",
        *,
        model: str,
        messages: list,
        max_tokens: int,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        tools: Optional[list] = None,
        thinking: Optional[dict] = None,
        supports_prompt_cache: bool = False,
        **_ignored: Any,
) -> _SimpleResponse:
    """非流式调用 Anthropic Messages API，返回值形状模拟
    `await client.chat.completions.create(...)` 的返回对象
    （resp.choices[0].message.content / .tool_calls），供
    subagent_tool.py 之类只需要"一次性拿完整结果"的调用方直接复用，
    无需为 Anthropic 单独写一套解析逻辑。

    入参 messages 为内部 Message 列表（含 role=system/tool），本函数内部
    完成到 Anthropic {system, messages} 形状的转换；返回值同样转换回
    OpenAI SDK 形状，调用方感知不到协议差异。
    """
    system_prompt, anthropic_messages = _convert_messages_to_anthropic(messages)
    if supports_prompt_cache and anthropic_messages:
        last_msg = anthropic_messages[-1]
        content = last_msg.get("content")

        if isinstance(content, list) and content:
            last_block = content[-1]
            if isinstance(last_block, dict):
                content[-1] = {
                    **last_block,
                    "cache_control": {"type": "ephemeral"},
                }
        elif isinstance(content, str) and content:
            last_msg["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
    anthropic_tools = _convert_tools_to_anthropic(tools) if tools else None

    request_kwargs: dict = {
        "model": model,
        "system": system_prompt or "You are a helpful assistant.",
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        request_kwargs["temperature"] = temperature
    if top_p is not None:
        request_kwargs["top_p"] = top_p
    if thinking:
        request_kwargs["thinking"] = thinking
    if anthropic_tools:
        request_kwargs["tools"] = anthropic_tools

    resp = await client.messages.create(**request_kwargs)

    content_text = ""
    tool_calls: list[_SimpleToolCall] = []
    for block in (resp.content or []):
        btype = getattr(block, "type", None)
        if btype == "text":
            content_text += getattr(block, "text", "") or ""
        elif btype == "tool_use":
            tool_calls.append(_SimpleToolCall(
                getattr(block, "id", "") or f"toolu_{uuid.uuid4().hex[:24]}",
                getattr(block, "name", "") or "",
                json.dumps(getattr(block, "input", {}) or {}, ensure_ascii=False),
            ))

    return _SimpleResponse(
        choices=[_SimpleChoice(_SimpleMessage(content_text, tool_calls))],
        usage=getattr(resp, "usage", None),
    )


# =============================================================================
# usage 归一：Anthropic Usage -> OpenAI 形状 dict
# =============================================================================
# 目的：让 _log_cache_usage / app.update_conversation_and_ledger 的既有
# OpenAI 形状消费方无需分支处理（与 gemini_bridge._gemini_usage_to_openai
# 同一边界转换模式）。
#
# 背景（bug 根因）：Anthropic SDK 的 Usage 字段是
#   input_tokens / output_tokens / cache_read_input_tokens /
#   cache_creation_input_tokens
# 而 update_conversation_and_ledger 只读 prompt_tokens /
# completion_tokens —— 不归一化的话台账全部落 0，表现为
# "XXTF 没有返回 usage 字段"。
#
# 口径：Anthropic 的 cache_read / cache_creation token 同样是本次请求
# 实际处理的输入 token（OpenAI 的 prompt_tokens 也包含 cached 子集），
# 因此并入 prompt_tokens，否则上下文水位会低估、台账差分失真。
def _anthropic_usage_to_openai(usage: Any) -> Optional[dict]:
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
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
            }
    except Exception:
        logger.debug("_anthropic_usage_to_openai 归一化失败，丢弃 usage", exc_info=True)
        return None

    def _num(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return int(value)

    prompt = (_num(d.get("input_tokens"))
              + _num(d.get("cache_read_input_tokens"))
              + _num(d.get("cache_creation_input_tokens")))
    completion = _num(d.get("output_tokens"))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        # 保留缓存明细：cache_usage._extract_cache_usage 可直接读取，
        # anthropic 原生循环的缓存命中率从此可观测。
        "cache_read_input_tokens": _num(d.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": _num(d.get("cache_creation_input_tokens")),
    }


# =============================================================================
# 流式请求瞬时故障重试（503 overloaded / 429 / 5xx / 连接抖动）
# =============================================================================
# 背景（2026-09 生产事故）：上游网关过载时返回 503 overloaded_error，
# 错误元数据明确标注 retryable=True / safe_to_auto_retry=True /
# max_retry_attempts=2 / retry_after_seconds=1，但 anthropic SDK 对流式
# 请求不会自动代劳重试——异常直接抛进 _agentic_loop_anthropic，整轮
# 对话即告失败。在「零输出」阶段（尚未向用户流出任何内容）重试完全
# 安全，这里补上应用层重试；一旦有任何增量已推给 builder 则绝不重试，
# 避免用户看到重复内容。
_ANTHROPIC_STREAM_MAX_RETRIES = 2
# 可重试 HTTP 状态码：请求超时/冲突/早数据/限流/上游服务端错误/过载。
# 529 是 Anthropic 官方的 overloaded 状态码；网关层常见 503 同义。
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
# 错误体 error.type 兜底识别（兼容网关改写状态码为 200 的场景）
_RETRYABLE_ERROR_TYPES = frozenset({"overloaded_error", "api_error", "timeout_error"})


def _is_retryable_stream_error(e: Exception) -> bool:
    """判定流式请求异常是否值得重试（仅用于零输出阶段的快速失败）。"""
    # 取消异常绝不重试（外层已单独 raise，这里防御兜底）
    if isinstance(e, asyncio.CancelledError):
        return False

    # 1) 状态码判定（anthropic SDK 的 APIStatusError 一定带 status_code）
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(e, "status", None)
    if status is not None:
        try:
            return int(status) in _RETRYABLE_STATUS_CODES
        except (TypeError, ValueError):
            pass

    # 2) 连接层异常（APITimeoutError / APIConnectionError，无状态码）：
    #    按类名判定而非 isinstance，避免在运行时硬依赖 anthropic 导入。
    if type(e).__name__ in ("APIConnectionError", "APITimeoutError"):
        return True

    # 3) 错误体 error.type 兜底判定
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("type") in _RETRYABLE_ERROR_TYPES:
            return True
    return False


def _extract_retry_after_seconds(e: Exception) -> Optional[float]:
    """从错误体/响应头提取上游建议的重试等待秒数；取不到返回 None。

    网关错误元数据形如 metadata.retry_after_seconds=1 /
    retry_after_ms=1000；个别网关也会带标准 Retry-After 响应头。
    """
    def _scan(obj: Any) -> Optional[float]:
        if not isinstance(obj, dict):
            return None
        for key in ("retry_after_seconds", "retryAfterSeconds", "retry-after-seconds"):
            v = obj.get(key)
            if isinstance(v, (int, float)) and v >= 0:
                return float(v)
        for key in ("retry_after_ms", "retryAfterMs"):
            v = obj.get(key)
            if isinstance(v, (int, float)) and v >= 0:
                return float(v) / 1000.0
        return None

    # 1) e.body：网关错误体
    #    （{'error': {...}, 'metadata': {'retry_after_seconds': 1, ...}}）
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        metadata = body.get("metadata")
        found = _scan(metadata) if isinstance(metadata, dict) else None
        if found is None:
            found = _scan(body)
        if found is not None:
            return found

    # 2) 标准 Retry-After 响应头
    response = getattr(e, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw = headers.get("retry-after")
            if raw:
                return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    return None


# =============================================================================
# 原生 agentic 循环
# =============================================================================
async def _agentic_loop_anthropic(
        client: "AsyncAnthropic",
        current_model: str,
        messages: list,
        builder: "DraftManager",
        tools: list | None = None,
        supports_tools: bool = True,
        journal: list | None = None,
) -> tuple[str | None, object | None, list]:
    """Anthropic 原生 Messages API 专用循环。

    对外契约与 _agentic_loop_openai_compat / _agentic_loop_gemini_native
    完全一致：入参/出参（messages、返回的 new_history_entries）统一为内部
    Message（core/messages），只在请求 Anthropic API 前做内部 -> 原生协议
    的边界转换（见模块头注释）。
    """
    api_label = "anthropic"
    if tools is None:
        from search_engine import SEARCH_TOOLS
        tools = SEARCH_TOOLS
    anthropic_tools = _convert_tools_to_anthropic(tools) if supports_tools else None

    state = init_bridge_loop_state(messages, journal, current_model)
    loop_messages = state.loop_messages
    new_history_entries = state.new_history_entries
    tool_call_count_ref = state.tool_call_count_ref
    final_content: str | None = state.final_content
    final_usage = state.final_usage
    model_info = state.model_info
    max_tokens = state.max_tokens
    sampling_params = state.sampling_params
    # Anthropic 采样参数直接是顶层 temperature/top_p，与 get_sampling_params
    # 的输出字段名一致，可直接透传。

    thinking_param = None
    if model_info and getattr(model_info, "reasoning_enabled", None) is True:
        budget = getattr(model_info, "reasoning_max_tokens", None) or 10000
        thinking_param = {"type": "enabled", "budget_tokens": int(budget)}

    prompt_cache_enabled = bool(model_info and model_info.supports_prompt_cache)

    for _round in range(MAX_TOOL_CALLS):
        system_prompt, anthropic_messages = _convert_messages_to_anthropic(loop_messages)
        # 顶层 system 请求参数：默认 None（非缓存路径用纯字符串）。
        request_system = None

        if prompt_cache_enabled and anthropic_messages:
            # Anthropic 多断点缓存策略（官方上限：单请求最多 4 个显式断点）
            # 断点应打在希望下一轮请求能复用的前缀末尾，按重要性排序：
            # 0. 顶层 system 段末尾（最稳定，几乎每轮都命中；打 1h TTL——
            #    系统提示会话内不变，Telegram 对话间隔经常超过默认 5 分钟，
            #    短 TTL 会反复过期重写；1h 写入溢价 2x 只付一次，读取仍 0.1x）
            # 1. 第一条 user 消息末尾（覆盖开场上下文注入）
            # 2. 倒数第二条 user/assistant 消息末尾（覆盖上一轮完整内容）
            # 3. 最后一条消息末尾（覆盖本轮新输入，loop 内多轮复用）
            # 合计恰好 4 个，不超上限（本循环没有其他动态打标出口）。
            cache_points_applied = 0
            MAX_CACHE_POINTS = 3  # 断点 1..3（不含顶层 system 段）

            # 断点 0: 顶层 system 段（1h TTL）。Anthropic 的 system 参数
            # 接受字符串或块列表；转成块列表才能挂 cache_control。
            if system_prompt:
                request_system = [{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }]
            else:
                request_system = None

            # 断点 1: 第一条 user 消息
            if system_prompt and anthropic_messages:
                first_msg = anthropic_messages[0]
                if first_msg.get("role") == "user" and first_msg.get("content"):
                    content = first_msg["content"]
                    if isinstance(content, list) and content:
                        content[-1] = {
                            **content[-1],
                            "cache_control": {"type": "ephemeral"},
                        }
                        cache_points_applied += 1
            
            # 断点 2 & 3: 从后往前找两条 user/assistant 消息
            remaining = MAX_CACHE_POINTS - cache_points_applied
            for i in range(len(anthropic_messages) - 1, 0, -1):
                if remaining <= 0:
                    break
                msg = anthropic_messages[i]
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    content = msg["content"]
                    if isinstance(content, list) and content:
                        already_marked = "cache_control" in content[-1]
                        if not already_marked:
                            content[-1] = {
                                **content[-1],
                                "cache_control": {"type": "ephemeral"},
                            }
                            remaining -= 1
                            cache_points_applied += 1

        request_kwargs: dict = {
            "model": current_model,
            # 缓存开启时 system 为带断点的块列表（1h TTL）；否则退回
            # 纯字符串形态，与旧行为一致。
            "system": request_system or (system_prompt or "You are a helpful assistant."),
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }
        if sampling_params.get("temperature") is not None:
            request_kwargs["temperature"] = sampling_params["temperature"]
        if sampling_params.get("top_p") is not None:
            request_kwargs["top_p"] = sampling_params["top_p"]
        if thinking_param:
            request_kwargs["thinking"] = thinking_param
        if anthropic_tools:
            request_kwargs["tools"] = anthropic_tools

        content_acc = ""
        reasoning_acc = ""
        tool_use_blocks: dict[int, dict] = {}
        current_stream_cell = [None]
        # v2.5：Anthropic 流结束原因（max_tokens / end_turn / tool_use…）。
        stop_reason = ""

        switch_stream = make_switch_stream(builder, current_stream_cell)

        try:
            await start_chat_action(builder.chat_id, "typing")
            # v2.6：上游瞬时故障应用层重试（503 overloaded / 429 / 5xx /
            # 连接抖动）。网关错误元数据会给出 retryable=True /
            # safe_to_auto_retry=True / max_retry_attempts=2，但 SDK 对流式
            # 请求不代劳重试，此前一次瞬时 503 就把整轮对话炸掉。
            # 安全前提：仅在本轮「零输出」（content_acc / reasoning_acc /
            # tool_use_blocks 全空，未向 builder 流出任何内容）时重试，
            # 一旦有 partial output 立即放弃，杜绝重复内容。
            stream_attempts = 0
            while True:
                try:
                    async with client.messages.stream(**request_kwargs) as stream:
                        async for event in stream:
                            etype = getattr(event, "type", None)
                            if etype == "content_block_start":
                                block = event.content_block
                                if getattr(block, "type", None) == "tool_use":
                                    tool_use_blocks[event.index] = {
                                        "id": block.id, "name": block.name, "args_json": "",
                                    }
                                    fn_args: dict[str, Any] = {}
                                    summary = _generate_initial_tool_summary(block.name, fn_args)
                                    action_desc = _generate_action_description(block.name, fn_args)
                                    builder.add_tool_item(
                                        block.id, block.name, summary,
                                        action_description=action_desc, fn_args=fn_args,
                                    )
                                    builder.request_flush(force=False)
                            elif etype == "content_block_delta":
                                delta = event.delta
                                dtype = getattr(delta, "type", None)
                                if dtype == "text_delta":
                                    text = delta.text or ""
                                    if text:
                                        content_acc += text
                                        await switch_stream("content")
                                        builder.append_stream_delta(text)
                                elif dtype == "thinking_delta":
                                    text = getattr(delta, "thinking", "") or ""
                                    if text:
                                        reasoning_acc += text
                                        await switch_stream("reasoning")
                                        builder.append_stream_delta(text)
                                elif dtype == "input_json_delta":
                                    entry = tool_use_blocks.get(event.index)
                                    if entry is not None:
                                        entry["args_json"] += getattr(delta, "partial_json", "") or ""
                                        if len(entry["args_json"]) % 40 < 4:
                                            parsed_args = _safe_parse_args(entry["args_json"])
                                            builder.update_tool_args(entry["id"], parsed_args)
                            elif etype == "message_delta":
                                usage = getattr(event, "usage", None)
                                if usage is not None:
                                    final_usage = usage
                    final_message = await stream.get_final_message()
                    if getattr(final_message, "usage", None):
                        final_usage = final_message.usage
                    # v2.5：捕获流结束原因（max_tokens = 输出上限切断），
                    # 供下方参数诊断信封定性「参数被切断」的根因。
                    stop_reason = str(getattr(final_message, "stop_reason", "") or "")
                    break  # 成功：退出重试循环
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    has_partial_output = bool(content_acc or reasoning_acc or tool_use_blocks)
                    if (
                        stream_attempts < _ANTHROPIC_STREAM_MAX_RETRIES
                        and not has_partial_output
                        and _is_retryable_stream_error(e)
                    ):
                        stream_attempts += 1
                        wait_s = _extract_retry_after_seconds(e)
                        if wait_s is None:
                            # 无上游提示时指数退避：1s → 2s（封顶 8s）
                            wait_s = min(2 ** stream_attempts, 8.0)
                        logger.warning(
                            f"[{api_label}] stream 可重试错误（零输出，{wait_s:.1f}s 后第 "
                            f"{stream_attempts}/{_ANTHROPIC_STREAM_MAX_RETRIES} 次重试）: {e}"
                        )
                        # 重置本轮累积状态（零输出前提下本应全空，双保险）。
                        # 流状态存于 current_stream_cell[0]（make_switch_stream
                        # 状态机），重试前一并复位；若重试前确有残留流，
                        # 下一次 switch_stream 会先 end_stream 再开新流。
                        content_acc = ""
                        reasoning_acc = ""
                        tool_use_blocks = {}
                        stop_reason = ""
                        current_stream_cell[0] = None
                        await asyncio.sleep(wait_s)
                        continue
                    logger.exception(f"[{api_label}] stream error: {e}")
                    raise
        except asyncio.CancelledError:
            raise
        finally:
            await stop_chat_action(builder.chat_id, "typing")

        builder.end_stream()

        # usage 归一化 + 缓存命中观测（每轮覆盖，与 openai/gemini 循环
        # 同口径）：Anthropic 原生 Usage 不转形状的话，
        # app.update_conversation_and_ledger 读 prompt_tokens /
        # completion_tokens 全部落 0 —— 表现为"XXTF 没有 usage"。
        # 归一化后的 dict 会成为下一轮返回值（token 台账 + 命中率统计）。
        final_usage = _anthropic_usage_to_openai(final_usage) or final_usage
        _log_cache_usage(api_label, final_usage, model_name=current_model)

        # 把这一轮的 tool_use 累积转换为 OpenAI 形状的 tool_calls，供
        # _run_tool_calls_and_append 复用（与另外两条循环完全同构）。
        tool_calls_list: list[dict] = []
        for idx in sorted(tool_use_blocks.keys()):
            entry = tool_use_blocks[idx]
            args_str = entry["args_json"] or "{}"
            # v2.3 Self-Correction：Anthropic 的 partial_json 拼接完成后
            # 理应是完整 JSON；解析失败时不再静默换成 "{}"（那会让工具
            # 带空参数执行、模型收不到任何参数写坏了的反馈，陷入盲重试），
            # 而是先尝试保守自动修复，失败则写入带完整诊断的可恢复信封
            # （信封本身是合法 JSON，不会污染下一轮请求），由执行层把
            # 解析器报错/位置/病因精准回传给模型。
            try:
                json.loads(args_str)
            except json.JSONDecodeError:
                repaired, repair_info = repair_json_arguments(args_str)
                if isinstance(repaired, dict):
                    # info["fixes"] 可能缺失（None）；函数体本就按 or [] 兜底，语义不变。
                    note = repair_note_for_result(repair_info.get("fixes") or [])
                    if note:
                        repaired[_JSON_REPAIR_NOTE_KEY] = note
                    args_str = json.dumps(
                        repaired, ensure_ascii=False, separators=(",", ":"))
                    logger.info(
                        "[anthropic] 工具 %s 参数 JSON 已自动修复（直接用修复后参数执行）",
                        entry["name"],
                    )
                else:
                    args_str = json.dumps(
                        build_invalid_arguments_envelope(
                            args_str, stream_finish_reason=(stop_reason or None)),
                        ensure_ascii=False, separators=(",", ":"),
                    )
                    logger.warning(
                        "[anthropic] 工具 %s 参数 JSON 非法且无法自动修复，已写入带诊断的可恢复错误"
                        "（流结束原因 stop_reason=%r）",
                        entry["name"], stop_reason,
                    )
            tool_calls_list.append({
                "id": entry["id"], "type": "function",
                "function": {"name": entry["name"], "arguments": args_str},
            })

        if reasoning_acc:
            builder.finalize_reasoning_block()

        # 块边界换草稿检查点①②（本轮最后一个块）：流已结束，最后一个思考块
        # 或文本块在此闭合，switch_stream 不会再被触发，故在此补一次检查。
        # 历史问题1（工具流式输出被拆到新草稿）已由安全点判定内的
        # _has_pending_tool_group 守卫兜住：本轮若已建工具条目而未收束，
        # 这里不会滚动，工具批次结束后的 tool.end 安全点仍会照常触发。
        # 终局轮修复：此处 tool_calls_list 已定型（本循环无伪工具调用纠正
        # 重试路径）。解耦改造：中途安全点（工具批次待执行）只发射事件，
        # 满容量时由 DraftManager 在后台滚动，Agent 立即继续（§8）；纯文本
        # 终局轮必须走 finalize_turn 同步收束，把"只永久化旧段、不创建
        # 新草稿"留给终局分支完成。否则容量预警标志会在终局轮被本检查
        # 点以 start_next_draft=True 抢先消费——创建一个永远无人写入、
        # 只显示 "Thinking..." 的幽灵草稿，随后被 get_ai_response 收尾
        # mark_dead + 删除（表现为回复交付后闪现的 Thinking 气泡）。
        will_request_again = bool(tool_calls_list)
        if will_request_again:
            # 非阻塞安全点：满容量时后台滚动；工具批次立即开始。
            builder.on_round_boundary()
            builder.request_flush()
        elif not await builder.finalize_turn():
            # 终局：等待旧段永久化（不开新草稿）；未滚动时保底刷一帧。
            builder.request_flush()

        append_assistant_message(loop_messages, new_history_entries, content_acc,
                                 tool_calls_list, reasoning_acc)

        if not tool_calls_list:
            final_content = content_acc
            finish_open_tool_group(builder)
            # 终局：同步收束旧段（只永久化、不创建新草稿）。
            await builder.finalize_turn()
            break

        status = await run_tool_batch(builder, tool_calls_list, loop_messages,
                                      new_history_entries, tool_call_count_ref,
                                      api_label, tools)

        if status == "over_limit":

            async def _synth_stream(req: tuple) -> str:
                synth_system, synth_messages = req
                synth_text = ""
                async with client.messages.stream(
                        model=current_model, system=synth_system or "You are a helpful assistant.",
                        messages=synth_messages, max_tokens=max_tokens,
                ) as synth_stream:
                    async for event in synth_stream:
                        if getattr(event, "type", None) == "content_block_delta":
                            delta = event.delta
                            if getattr(delta, "type", None) == "text_delta":
                                text = delta.text or ""
                                if text:
                                    synth_text += text
                                    builder.append_stream_delta(text)
                return synth_text

            final_content = await over_limit_final_summary(
                builder, new_history_entries,
                api_label=api_label, loop_name="_agentic_loop_anthropic",
                build_synth_request=lambda extra: _convert_messages_to_anthropic(
                    loop_messages + [extra]),
                stream_synth=_synth_stream,
            )
            break
        # status == "continue"：循环自然继续

    final_content = await ensure_final_content(builder, new_history_entries, final_content)

    return final_content, final_usage, new_history_entries

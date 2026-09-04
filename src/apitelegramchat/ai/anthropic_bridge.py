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
from typing import TYPE_CHECKING, Optional

from apitelegramchat.config import SUPPORTED_MODELS, get_sampling_params
from apitelegramchat.utils import get_logger
from apitelegramchat.chat_actions import start_chat_action, stop_chat_action

from apitelegramchat.ai._constants import MAX_TOOL_CALLS
from apitelegramchat.ai.json_repair import (
    _JSON_REPAIR_NOTE_KEY,
    build_invalid_arguments_envelope,
    repair_json_arguments,
    repair_note_for_result,
)
from apitelegramchat.ai.tool_summary import (
    _generate_action_description,
    _generate_initial_tool_summary,
    _safe_parse_args,
    _tool_limit_summary,
)
from apitelegramchat.ai.tool_call_loop import _run_tool_calls_and_append

if TYPE_CHECKING:
    from apitelegramchat.ai.rich_message_builder import RichMessageBuilder
    from anthropic import AsyncAnthropic

logger = get_logger(__name__)


# =============================================================================
# 工具 schema 转换：OpenAI function-calling 形状 -> Anthropic tool 形状
# =============================================================================
# OpenAI: {"type": "function", "function": {"name", "description", "parameters"}}
# Anthropic: {"name", "description", "input_schema"}
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
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return converted or None


# =============================================================================
# 消息格式转换：OpenAI 形状（role: system/user/assistant/tool）
#             -> Anthropic 形状（顶层 system 字符串 + messages: user/assistant，
#                                 tool 结果作为 user 消息里的 tool_result 块）
# =============================================================================
def _openai_content_to_anthropic_blocks(content) -> list:
    """把 OpenAI 的 content（str 或 content-parts 列表）转换成 Anthropic
    content 块列表。仅支持 text 与 image_url（含 data:base64 内联和公开
    URL 两种），未识别的 part 类型退化为文本占位，不中断请求。
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []

    blocks = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "")
            if text:
                blocks.append({"type": "text", "text": text})
        elif ptype == "image_url":
            url_obj = part.get("image_url") or {}
            url = url_obj.get("url", "") if isinstance(url_obj, dict) else str(url_obj)
            if url.startswith("data:"):
                try:
                    header, b64data = url.split(",", 1)
                    media_type = header.split(";")[0].split(":", 1)[1] or "image/png"
                except (ValueError, IndexError):
                    media_type, b64data = "image/png", ""
                if b64data:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64data},
                    })
            elif url:
                # Anthropic 支持公开可访问 URL 直接引用，无需先下载转 base64。
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        # 其它 part 类型（audio/video 等）Anthropic Messages API 暂不支持，
        # 静默跳过而不是抛错中断整轮请求。
    return blocks


def _convert_messages_to_anthropic(messages: list) -> tuple[str, list]:
    """把 OpenAI 形状的消息列表转换成 Anthropic 的 (system_prompt, messages)。

    规则：
      - role=system -> 拼接进顶层 system 字符串（Anthropic 无 system 角色消息）
      - role=user   -> Anthropic user 消息（content 转换为块列表）
      - role=assistant -> Anthropic assistant 消息；若含 tool_calls，追加
        对应的 tool_use 块（每个 tool_call 一个块，input 为解析后的 JSON）
      - role=tool   -> 追加/合并进下一个 Anthropic user 消息的 tool_result
        块（Anthropic 要求 tool_result 必须放在 user 消息里，且通常紧跟
        在触发它的 assistant tool_use 消息之后）
    """
    system_parts: list[str] = []
    anthropic_messages: list[dict] = []
    pending_tool_results: list[dict] = []

    def _flush_pending_tool_results():
        if pending_tool_results:
            anthropic_messages.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            c = msg.get("content")
            if isinstance(c, str) and c:
                system_parts.append(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                        system_parts.append(part["text"])
            continue

        if role == "tool":
            # tool 结果必须归入下一条 user 消息；先攒着，遇到下一个非 tool
            # 消息（或结尾）时统一 flush。
            tc_id = msg.get("tool_call_id", "")
            content = msg.get("content", "")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc_id,
                "content": [{"type": "text", "text": text}],
            })
            continue

        # 非 tool 消息出现之前，先把攒的 tool_result 落成一条 user 消息。
        _flush_pending_tool_results()

        if role == "user":
            blocks = _openai_content_to_anthropic_blocks(msg.get("content"))
            if blocks:
                anthropic_messages.append({"role": "user", "content": blocks})
            continue

        if role == "assistant":
            blocks = []
            text_content = msg.get("content")
            if isinstance(text_content, str) and text_content:
                blocks.append({"type": "text", "text": text_content})
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function", {})
                try:
                    tool_input = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    tool_input = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": fn.get("name", ""),
                    "input": tool_input,
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
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _SimpleToolCall:
    def __init__(self, id_: str, name: str, arguments: str):
        self.id = id_
        self.function = _SimpleFunctionCall(name, arguments)


class _SimpleMessage:
    """模拟 OpenAI SDK 的 resp.choices[0].message 接口（仅 subagent_tool.py
    实际读取的 .content / .tool_calls 两个属性），让调用方无需分支处理
    Anthropic 响应即可复用现有解析代码。
    """
    def __init__(self, content: str, tool_calls: list):
        self.content = content
        self.tool_calls = tool_calls


class _SimpleChoice:
    def __init__(self, message: "_SimpleMessage"):
        self.message = message


class _SimpleResponse:
    def __init__(self, choices: list, usage=None):
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
        **_ignored,
) -> _SimpleResponse:
    """非流式调用 Anthropic Messages API，返回值形状模拟
    `await client.chat.completions.create(...)` 的返回对象
    （resp.choices[0].message.content / .tool_calls），供
    subagent_tool.py 之类只需要"一次性拿完整结果"的调用方直接复用，
    无需为 Anthropic 单独写一套解析逻辑。

    入参 messages 为 OpenAI 形状（含 role=system/tool），本函数内部
    完成到 Anthropic {system, messages} 形状的转换；返回值同样转换回
    OpenAI 形状，调用方感知不到协议差异。
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
# 原生 agentic 循环
# =============================================================================
async def _agentic_loop_anthropic(
        client: "AsyncAnthropic",
        current_model: str,
        messages: list,
        builder: "RichMessageBuilder",
        tools: list = None,
        supports_tools: bool = True,
        journal: list = None,
) -> tuple[str | None, object | None, list]:
    """Anthropic 原生 Messages API 专用循环。

    对外契约与 _agentic_loop_openai_compat / _agentic_loop_gemini_native
    完全一致：入参/出参（messages、返回的 new_history_entries）都是 OpenAI
    形状，只在请求 Anthropic API 前后做边界转换（见模块头注释）。
    """
    api_label = "anthropic"
    if tools is None:
        from apitelegramchat.search_engine import SEARCH_TOOLS
        tools = SEARCH_TOOLS
    anthropic_tools = _convert_tools_to_anthropic(tools) if supports_tools else None

    loop_messages = list(messages)  # OpenAI 形状，供 _run_tool_calls_and_append 复用
    final_content: str | None = None
    final_usage = None
    tool_call_count_ref = [0]
    new_history_entries = journal if journal is not None else []

    model_info = SUPPORTED_MODELS.get(current_model)
    max_tokens = model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192
    sampling_params = get_sampling_params(model_info)
    # Anthropic 采样参数直接是顶层 temperature/top_p，与 get_sampling_params
    # 的输出字段名一致，可直接透传。

    thinking_param = None
    if model_info and getattr(model_info, "reasoning_enabled", None) is True:
        budget = getattr(model_info, "reasoning_max_tokens", None) or 10000
        thinking_param = {"type": "enabled", "budget_tokens": int(budget)}

    prompt_cache_enabled = bool(model_info and model_info.supports_prompt_cache)

    for _round in range(MAX_TOOL_CALLS):
        system_prompt, anthropic_messages = _convert_messages_to_anthropic(loop_messages)

        if prompt_cache_enabled and anthropic_messages:
            # Anthropic 多断点缓存策略（官方上限：单请求最多 4 个显式断点）
            # 断点应打在希望下一轮请求能复用的前缀末尾，按重要性排序：
            # 1. system 消息末尾（最稳定，几乎每轮都命中）
            # 2. 倒数第二条 user/assistant 消息末尾（覆盖上一轮完整内容）
            # 3. 最后一条消息末尾（覆盖本轮新输入，loop 内多轮复用）
            # 第 4 个断点额度保留给 agentic loop 内的动态追加
            cache_points_applied = 0
            MAX_CACHE_POINTS = 3  # 保留 1 个额度给 loop 内动态使用
            
            # 断点 1: system 消息
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
            "system": system_prompt or "You are a helpful assistant.",
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
        current_stream = None
        # v2.5：Anthropic 流结束原因（max_tokens / end_turn / tool_use…）。
        stop_reason = ""

        def switch_stream(target: str):
            nonlocal current_stream
            if current_stream == target:
                return
            builder.end_stream()
            if target == "reasoning":
                builder.begin_stream_reasoning()
            elif target == "content":
                builder.begin_stream_text()
            current_stream = target

        try:
            await start_chat_action(builder.chat_id, "typing")
            async with client.messages.stream(**request_kwargs) as stream:
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = event.content_block
                        if getattr(block, "type", None) == "tool_use":
                            tool_use_blocks[event.index] = {
                                "id": block.id, "name": block.name, "args_json": "",
                            }
                            fn_args = {}
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
                                switch_stream("content")
                                builder.append_stream_delta(text)
                        elif dtype == "thinking_delta":
                            text = getattr(delta, "thinking", "") or ""
                            if text:
                                reasoning_acc += text
                                switch_stream("reasoning")
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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"[{api_label}] stream error: {e}")
            raise
        finally:
            await stop_chat_action(builder.chat_id, "typing")

        builder.end_stream()

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
                    note = repair_note_for_result(repair_info.get("fixes"))
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
        await builder.flush()

        assistant_msg: dict = {"role": "assistant", "content": content_acc or None}
        if tool_calls_list:
            assistant_msg["tool_calls"] = tool_calls_list
        if reasoning_acc:
            assistant_msg["reasoning_content"] = reasoning_acc
        loop_messages.append(assistant_msg)
        new_history_entries.append(assistant_msg)

        if not tool_calls_list:
            final_content = content_acc
            if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                builder.finish_group(len(builder._tool_groups) - 1)
            await builder.rollover_at_turn_boundary(start_next_draft=False)
            break

        status = await _run_tool_calls_and_append(
            tool_calls_list, loop_messages, new_history_entries,
            tool_call_count_ref, api_label, builder, chat_id=builder.chat_id,
            tools=tools,
        )
        await builder.rollover_at_turn_boundary(start_next_draft=True)

        if status == "over_limit":
            synth_system, synth_messages = _convert_messages_to_anthropic(
                loop_messages + [{
                    "role": "user",
                    "content": (
                        f"System: Maximum tool calls ({MAX_TOOL_CALLS}) reached for this turn. "
                        "Tool usage is now DISABLED. Please immediately summarize what you have "
                        "successfully done so far, explicitly state what failed or what is left "
                        "to do, and ask the user if they want to continue the operation in the "
                        "next turn."
                    ),
                }]
            )
            try:
                await start_chat_action(builder.chat_id, "typing")
                builder.begin_stream_text()
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
                final_content = builder.end_stream_text() or synth_text
                if not final_content:
                    final_content = _tool_limit_summary()
                    builder.add_text(final_content)
            except Exception as synth_err:
                logger.warning(f"Anthropic 合成流失败: {synth_err}")
                try:
                    builder.end_stream_text()
                except Exception:
                    logger.debug("_agentic_loop_anthropic 内部忽略的异常", exc_info=True)
                final_content = _tool_limit_summary()
                builder.add_text(final_content)
            finally:
                await stop_chat_action(builder.chat_id, "typing")
            new_history_entries.append({"role": "assistant", "content": final_content})
            if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                builder.finish_group(len(builder._tool_groups) - 1)
            await builder.rollover_at_turn_boundary(start_next_draft=False)
            break
        # status == "continue"：循环自然继续

    if final_content is None:
        final_content = _tool_limit_summary()
        builder.add_text(final_content)
        new_history_entries.append({"role": "assistant", "content": final_content})
        if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
            builder.finish_group(len(builder._tool_groups) - 1)
        await builder.rollover_at_turn_boundary(start_next_draft=False)

    return final_content, final_usage, new_history_entries

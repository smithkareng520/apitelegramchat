"""四种 agentic 循环实现：OpenAI 兼容流式 / Gemini OpenAI 兼容 / 原生图片 / 原生视频。

从 ai_handlers.py 拆分而来，逻辑未做改动。
"""
import asyncio
import json
import aiohttp
import httpx
import base64
import re
import uuid
from typing import Optional
from openai import AsyncOpenAI

from apitelegramchat.config import GEMINI_API_KEY, SUPPORTED_MODELS
from apitelegramchat.utils import get_logger, escape_html, send_rich_html_message
from apitelegramchat.s3_utils import upload_bytes_to_r2
import apitelegramchat.state as state

from apitelegramchat.ai._constants import (
    MAX_TOOL_CALLS,
    MAX_PLAIN_TEXT_TOOL_CALL_RETRIES,
    OPENROUTER_PROVIDER_PREFERENCES,
    TIMEOUT,
)
from apitelegramchat.ai.error_formatting import (
    _format_api_error_notice,
    _format_image_metadata_caption,
    _format_image_safety_notice,
    _format_video_metadata_caption,
    _is_content_safety_error,
    get_error_notification_message,
)
from apitelegramchat.ai.media_generation import (
    _clean_prompt_for_image_model,
    _extract_image_items,
    _extract_native_message_text,
    _extract_native_refusal_text,
    _format_native_image_notice,
    _request_agnes_video,
    _request_modelscope_native_image,
    _request_openrouter_video,
    _response_items_to_bytes,
)
from apitelegramchat.ai.tool_summary import (
    _contains_textual_tool_call,
    _generate_action_description,
    _generate_initial_tool_summary,
    _normalize_tool_call_arguments,
    _safe_parse_args,
    _strip_textual_tool_calls,
    _tool_limit_summary,
)
from apitelegramchat.ai.tool_call_loop import _run_tool_calls_and_append
from apitelegramchat.ai.gemini_native_protocol import (
    _GEMINI_THOUGHT_SIGNATURES_KEY,
    build_assistant_msg_from_gemini_state,
    merge_gemini_part_into_state,
    openai_messages_to_gemini_contents,
    openai_tools_to_gemini_function_declarations,
)

logger = get_logger(__name__)

def _merge_tool_call_delta(accumulator: dict, index: int, delta_tc: dict):
    if index not in accumulator:
        accumulator[index] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
    entry = accumulator[index]
    if delta_tc.get("id"):
        entry["id"] = delta_tc["id"]
    fn = delta_tc.get("function", {})
    if fn.get("name"):
        entry["function"]["name"] += fn["name"]
    if fn.get("arguments"):
        entry["function"]["arguments"] += fn["arguments"]


def _openrouter_extra_body() -> dict:
    return {"provider": OPENROUTER_PROVIDER_PREFERENCES.copy()}


async def _agentic_loop_openai_compat(
        client: AsyncOpenAI, current_model: str, messages: list, api_label: str,
        builder: "RichMessageBuilder", tools: list = None, supports_tools: bool = True
) -> tuple[str | None, object | None, list]:
    if tools is None:
        from apitelegramchat.search_engine import SEARCH_TOOLS
        tools = SEARCH_TOOLS
    loop_messages = list(messages)
    final_content = None
    final_usage = None
    tool_call_count_ref = [0]
    new_history_entries = []
    plain_text_tool_attempts = 0
    parallel_tool_calls = True

    model_info = SUPPORTED_MODELS.get(current_model)
    supports_sampling = model_info.supports_sampling if model_info else True
    max_tokens = model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192

    for _round in range(MAX_TOOL_CALLS):
        added_tool_indices = set()
        last_arg_len = {}

        content_acc = ""
        reasoning_acc = ""
        tool_calls_acc: dict = {}
        in_reasoning = False
        current_stream = None
        received_any = False
        # 本轮"第一个出现的内容类型"：'tool'（先出现工具调用）或 'content'（先出现思考/文本）。
        # 只有第一次出现时才据此决定是否要关闭上一个未闭合的工具块，之后不再重复判断。
        round_leading_kind = None

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
            create_params = {
                "model": current_model,
                "messages": loop_messages,
                "stream": True,
                "max_tokens": max_tokens,
                "stream_options": {"include_usage": True},
            }
            if supports_sampling:
                create_params["temperature"] = 0.6
                create_params["top_p"] = 0.9
            if supports_tools and tools:
                create_params["tools"] = tools
                create_params["tool_choice"] = "auto"
                create_params["parallel_tool_calls"] = parallel_tool_calls
            if api_label == "openrouter":
                create_params["extra_body"] = _openrouter_extra_body()

            # 某些聚合网关会在长工具链后的首个 SSE 事件前沉默较久。
            # 只有尚未收到任何增量时，重试相同请求才是幂等且安全的；一旦已经向
            # 用户或工具状态写入增量，必须直接抛出，避免重放半个模型回合。
            for stream_attempt in range(2):
                try:
                    comp_stream = await client.chat.completions.create(**create_params)
                    async for chunk in comp_stream:
                        received_any = True
                        if getattr(chunk, "usage", None):
                            final_usage = chunk.usage
                        choices = chunk.choices or []
                        if not choices:
                            continue
                        delta = choices[0].delta
                        c_delta = getattr(delta, "content", None) or ""
                        if isinstance(c_delta, list):
                            c_delta = "".join(str(item) for item in c_delta)

                        r_delta = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None) or ""
                        if isinstance(r_delta, list):
                            r_delta = "".join(str(item) for item in r_delta)
                        if r_delta:
                            if round_leading_kind is None:
                                round_leading_kind = "content"
                                if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                                    builder.finish_group(len(builder._tool_groups) - 1)
                                    # ★ 强制刷新，确保总结先于思考内容显示 ★
                                    await builder.flush(force=True)
                            switch_stream("reasoning")
                            reasoning_acc += r_delta
                            builder.append_stream_delta(r_delta)

                        if c_delta:
                            content_acc += c_delta
                            if round_leading_kind is None:
                                round_leading_kind = "content"
                                if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                                    builder.finish_group(len(builder._tool_groups) - 1)
                                    # ★ 强制刷新，确保总结先于文本内容显示 ★
                                    await builder.flush(force=True)
                            if round_leading_kind == "tool" and builder._tool_groups and not builder._tool_groups[-1].get(
                                    "finished", False):
                                # 本轮先出现了工具调用，这段文字是同一轮里紧跟在工具调用之后的说明文字，
                                # 归入当前（同一轮新开或合并的）工具块内部。
                                builder.append_to_current_tool_group_text(c_delta)
                            else:
                                if "<think>" in c_delta:
                                    in_reasoning = True
                                    before, _, rest = c_delta.partition("<think>")
                                    if before:
                                        switch_stream("content")
                                        builder.append_stream_delta(before)
                                    switch_stream("reasoning")
                                    if "</think>" in rest:
                                        think_part, _, after = rest.partition("</think>")
                                        reasoning_acc += think_part
                                        builder.append_stream_delta(think_part)
                                        in_reasoning = False
                                        if after:
                                            switch_stream("content")
                                            builder.append_stream_delta(after)
                                        else:
                                            current_stream = None
                                    else:
                                        reasoning_acc += rest
                                        builder.append_stream_delta(rest)
                                elif in_reasoning:
                                    if "</think>" in c_delta:
                                        think_part, _, after = c_delta.partition("</think>")
                                        reasoning_acc += think_part
                                        builder.append_stream_delta(think_part)
                                        in_reasoning = False
                                        if after:
                                            switch_stream("content")
                                            builder.append_stream_delta(after)
                                        else:
                                            current_stream = None
                                    else:
                                        reasoning_acc += c_delta
                                        builder.append_stream_delta(c_delta)
                                else:
                                    switch_stream("content")
                                    builder.append_stream_delta(c_delta)

                        for tc_delta in (getattr(delta, "tool_calls", None) or []):
                            idx = getattr(tc_delta, "index", 0)
                            _merge_tool_call_delta(
                                tool_calls_acc, idx,
                                {"id": getattr(tc_delta, "id", "") or "",
                                 "function": {"name": getattr(tc_delta.function, "name", "") or "",
                                              "arguments": getattr(tc_delta.function, "arguments", "") or ""}}
                            )
                            if idx not in added_tool_indices:
                                tc = tool_calls_acc[idx]
                                tc_id = tc.get("id")
                                tc_name = tc.get("function", {}).get("name")
                                if tc_id and tc_name:
                                    if round_leading_kind is None:
                                        # 本轮第一个出现的就是工具调用：沿用/合并到上一个未闭合的工具块。
                                        round_leading_kind = "tool"
                                    elif round_leading_kind == "content" and builder._tool_groups and not builder._tool_groups[
                                        -1].get("finished", False):
                                        # 本轮先出现了文本/思考才轮到工具调用：这段文本已经在上面把旧工具块
                                        # 关闭掉了，这里创建的会是全新的独立工具块，不需要再次关闭。
                                        pass
                                    args_str = tc.get("function", {}).get("arguments", "")
                                    parsed_args = _safe_parse_args(args_str)
                                    summary = _generate_initial_tool_summary(tc_name, parsed_args)
                                    action_desc = _generate_action_description(tc_name, parsed_args)
                                    builder.add_tool_item(
                                        tc_id,
                                        tc_name,
                                        summary,
                                        action_description=action_desc,
                                        fn_args=parsed_args
                                    )
                                    added_tool_indices.add(idx)
                                    builder.request_flush(force=False)

                            if idx in added_tool_indices:
                                tc = tool_calls_acc[idx]
                                tc_id = tc.get("id")
                                if not tc_id:
                                    continue
                                # 工具调用参数在流式接收过程中不再实时渲染预览；
                                # 最终结果会在工具执行完成后按统一的 Input/Output 格式一次性展示。
                                current_args = tc.get("function", {}).get("arguments", "")
                                current_len = len(current_args)
                                if current_len - last_arg_len.get(idx, 0) >= 20:
                                    last_arg_len[idx] = current_len
                                    tc_name = tc.get("function", {}).get("name", "")
                                    parsed_args = _safe_parse_args(current_args)
                                    for group in builder._tool_groups:
                                        for item in group["items"]:
                                            if item["id"] == tc_id:
                                                item["fn_args"] = parsed_args
                                            break
                    break
                except httpx.ReadTimeout:
                    if received_any or stream_attempt >= 1:
                        raise
                    logger.warning(
                        "[%s] 第 %s 轮模型流在首个增量前读取超时，等待后重试一次",
                        api_label, _round + 1,
                    )
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"{api_label} stream error: {e}")
            raise

        builder.end_stream()
        while builder.blocks and not builder.blocks[-1].strip() and builder.block_types[-1] in ("text", "reasoning"):
            builder.blocks.pop()
            builder.block_types.pop()

        if not received_any or (not content_acc and not tool_calls_acc):
            logger.warning(f"[{api_label}] 流式无有效内容，回退到非流式请求")
            try:
                fallback_params = {
                    "model": current_model,
                    "messages": loop_messages,
                    "stream": False,
                    "max_tokens": max_tokens,
                }
                if supports_sampling:
                    fallback_params["temperature"] = 0.6
                    fallback_params["top_p"] = 0.9
                if supports_tools and tools:
                    fallback_params["tools"] = tools
                    fallback_params["tool_choice"] = "auto"
                    fallback_params["parallel_tool_calls"] = parallel_tool_calls
                if api_label == "openrouter":
                    fallback_params["extra_body"] = _openrouter_extra_body()

                resp = await client.chat.completions.create(**fallback_params)
                msg = resp.choices[0].message
                content_acc = msg.content or ""
                if supports_tools and tools and hasattr(msg, "tool_calls") and msg.tool_calls:
                    for idx, tc in enumerate(msg.tool_calls):
                        tool_calls_acc[idx] = {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                        }
                if not content_acc and not tool_calls_acc:
                    content_acc = "（模型未返回任何内容）"
                try:
                    fallback_tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())] if tool_calls_acc else []
                    logger.info(
                        f"[{api_label}] 第 {_round + 1} 轮模型原始返回(回退): tool_calls={len(fallback_tool_calls)}, "
                        f"ids={[tc.get('id', '') or '' for tc in fallback_tool_calls]}, "
                        f"names={[tc.get('function', {}).get('name', '') or '' for tc in fallback_tool_calls]}, "
                        f"content_len={len(content_acc.strip())}"
                    )
                except Exception:
                    logger.exception(f"[{api_label}] 记录回退 tool_calls 日志失败")
            except Exception as e:
                logger.exception(f"非流式回退失败: {e}")
                content_acc = "请求失败，请稍后重试。"

        tool_calls_list = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())] if tool_calls_acc else []
        try:
            tool_call_names = [tc.get("function", {}).get("name", "") or "" for tc in tool_calls_list]
            tool_call_ids = [tc.get("id", "") or "" for tc in tool_calls_list]
            logger.info(
                f"[{api_label}] 第 {_round + 1} 轮模型原始返回: tool_calls={len(tool_calls_list)}, "
                f"ids={tool_call_ids}, names={tool_call_names}, content_len={len(content_acc.strip())}, "
                f"reasoning_len={len(reasoning_acc.strip())}"
            )
        except Exception:
            logger.exception(f"[{api_label}] 记录 tool_calls 日志失败")
        for idx, tc in enumerate(tool_calls_list):
            if not tc.get("id"):
                tc["id"] = f"call_{_round}_{idx}_{uuid.uuid4().hex[:8]}"
        _normalize_tool_call_arguments(tool_calls_list, api_label, _round + 1)

        if not tool_calls_list and not content_acc.strip():
            content_acc = "（模型未返回任何内容）"

        # 个别兼容模型会将 function calling XML 错当普通正文输出。该内容已经
        # 在流式阶段写入草稿，必须先从构建器撤回，避免最终消息泄漏 <tool_call>。
        textual_tool_call = _contains_textual_tool_call(content_acc)
        if textual_tool_call:
            raw_textual_content = content_acc
            content_acc = _strip_textual_tool_calls(content_acc)
            if not builder.replace_trailing_text(raw_textual_content, content_acc):
                logger.warning(
                    "[%s] 未能在草稿中定位伪工具调用文本，已阻止其进入最终内容",
                    api_label,
                )

        if reasoning_acc:
            builder.finalize_reasoning_block(has_tool_calls=bool(tool_calls_list))
        await builder.flush()

        assistant_msg: dict = {"role": "assistant", "content": content_acc or None}
        if tool_calls_list:
            assistant_msg["tool_calls"] = [{"id": tc["id"], "type": "function",
                                            "function": {"name": tc["function"]["name"],
                                                         "arguments": tc["function"]["arguments"]}} for tc in
                                           tool_calls_list]
        if reasoning_acc:
            assistant_msg["reasoning_content"] = reasoning_acc
        loop_messages.append(assistant_msg)
        new_history_entries.append(assistant_msg)

        # 文本伪工具调用最多纠正三次；达到次数后直接给出安全状态说明，而不是
        # 把 XML 原文返回给用户，也避免模型在不可恢复状态下无限循环。
        if not tool_calls_list:
            if textual_tool_call:
                plain_text_tool_attempts += 1
                logger.warning(
                    "[%s] 模型输出了文本格式工具调用，已清理并请求标准调用：第 %s/%s 次",
                    api_label, plain_text_tool_attempts, MAX_PLAIN_TEXT_TOOL_CALL_RETRIES,
                )
                if plain_text_tool_attempts < MAX_PLAIN_TEXT_TOOL_CALL_RETRIES:
                    loop_messages.append({
                        "role": "user",
                        "content": (
                            "System: Your last response attempted a tool call as plain text. "
                            "Use the standard tool_calls API only. Do not emit <tool_call> XML as user-visible text."
                        )
                    })
                    # 这是一次完整但需要纠正的模型返回；还会重试下一请求，因此创建新草稿。
                    await builder.rollover_at_turn_boundary(start_next_draft=True)
                    continue
                final_content = content_acc or (
                    "工具调用格式连续异常，未继续执行额外操作。请重新描述需求或换一个模型后重试。"
                )
                if not content_acc:
                    builder.add_text(final_content)
            else:
                final_content = content_acc
            if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                builder.finish_group(len(builder._tool_groups) - 1)
            # 终局也统一进入滚动函数；函数只永久化旧段，不创建新草稿。
            await builder.rollover_at_turn_boundary(start_next_draft=False)
            break
        status = await _run_tool_calls_and_append(
            tool_calls_list, loop_messages, new_history_entries,
            tool_call_count_ref, api_label, builder, chat_id=builder.chat_id
        )
        # 工具批次已完整收束；后续仍会请求模型，因此在函数内创建新草稿。
        await builder.rollover_at_turn_boundary(start_next_draft=True)

        # ===== FIX: 只对 over_limit 做强制总结并退出 =====
        if status == "over_limit":
            synth_params = {
                "model": current_model,
                "messages": loop_messages + [{"role": "user",
                                              "content": f"System: Maximum tool calls ({MAX_TOOL_CALLS}) reached for this turn. Tool usage is now DISABLED. Please immediately summarize what you have successfully done so far, explicitly state what failed or what is left to do, and ask the user if they want to continue the operation in the next turn."}],
                "stream": True,
                "max_tokens": max_tokens,
            }
            if supports_sampling:
                synth_params["temperature"] = 0.6
            try:
                synth_stream = await client.chat.completions.create(**synth_params)
                builder.begin_stream_text()
                synth_text = ""
                async for chunk in synth_stream:
                    if chunk.choices:
                        c_delta = getattr(chunk.choices[0].delta, "content", None) or ""
                        if c_delta:
                            synth_text += c_delta
                            builder.append_stream_delta(c_delta)
                raw_synth_content = builder.end_stream_text() or synth_text
                final_content = _strip_textual_tool_calls(raw_synth_content)
                if final_content != raw_synth_content:
                    builder.replace_trailing_text(raw_synth_content, final_content)
                if not final_content:
                    final_content = _tool_limit_summary()
                    builder.add_text(final_content)
            except Exception as synth_err:
                # 合成流失败时使用兜底文本，避免丢失整个工具调用历史或泄漏工具 XML。
                logger.warning(f"OpenAI 合成流失败: {synth_err}")
                try:
                    builder.end_stream_text()
                except Exception:
                    pass
                final_content = _tool_limit_summary()
                builder.add_text(final_content)
            new_history_entries.append({"role": "assistant", "content": final_content})
            if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                builder.finish_group(len(builder._tool_groups) - 1)
            # 工具上限总结是终局回复；统一结束旧草稿，但不创建新草稿。
            await builder.rollover_at_turn_boundary(start_next_draft=False)
            break
        # 如果 status == "continue"（包括之前熔断返回的），循环自然继续

    # 理论上真实工具调用会先触发 over_limit；此处仍为轮次数耗尽或异常模型行为提供
    # 可见的、无工具调用标记的最终状态，避免 final_content 为 None。
    if final_content is None:
        final_content = _tool_limit_summary()
        builder.add_text(final_content)
        new_history_entries.append({"role": "assistant", "content": final_content})
        if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
            builder.finish_group(len(builder._tool_groups) - 1)
        # 轮次数耗尽后的兜底文本同样是终局内容：结束旧草稿，不创建下一段。
        await builder.rollover_at_turn_boundary(start_next_draft=False)
    return final_content, final_usage, new_history_entries


async def _agentic_loop_gemini_native(
        current_model: str,
        messages: list,
        builder: "RichMessageBuilder",
        tools: list = None,
        supports_tools: bool = True,
) -> tuple[str | None, object | None, list]:
    """Gemini 原生 :streamGenerateContent?alt=sse 流式循环。

    与 OpenAI 兼容端点不同，原生端点保留完整的 parts/thought/thoughtSignature
    结构，因此可以做到：
      1. 思考增量推送（reasoning_content 逐 token 显示，不等整段返回）；
      2. thought_signature 链跨轮保存（避免多轮推理质量退化）；
      3. thought 布尔位（区分"思考"与"可见文本"）。

    loop_messages 在内部仍以 OpenAI 兼容格式维护，便于与 _run_tool_calls_and_append
    共享。每轮请求前通过 openai_messages_to_gemini_contents 转换；流式响应通过
    merge_gemini_part_into_state 累积到 state，最后用 build_assistant_msg_from_gemini_state
    构造下一轮请求所需的 assistant_msg。
    """
    def _clean_tools_for_gemini(tools: list) -> list:
        if not tools:
            return tools
        cleaned = []
        for tool in tools:
            new_tool = {
                "type": tool.get("type", "function"),
                "function": {
                    k: v for k, v in tool.get("function", {}).items()
                    if k != "input_examples"
                }
            }
            cleaned.append(new_tool)
        return cleaned

    cleaned_tools = _clean_tools_for_gemini(tools) if tools else None
    gemini_tools = openai_tools_to_gemini_function_declarations(cleaned_tools) if cleaned_tools else None

    model_info = SUPPORTED_MODELS.get(current_model)
    supports_sampling = model_info.supports_sampling if model_info else True
    max_tokens = model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192

    # 原生端点 URL；alt=sse 让响应变成标准 Server-Sent Events 流。
    # 鉴权仍是同一个 GEMINI_API_KEY 的 Bearer token，不需要任何新密钥。
    GEMINI_NATIVE_URL = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{current_model}:streamGenerateContent?alt=sse"
    )
    req_headers = {
        "Authorization": f"Bearer {GEMINI_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    loop_messages = list(messages)
    final_content: str | None = None
    final_usage = None
    tool_call_count_ref = [0]
    new_history_entries: list = []

    for _round in range(MAX_TOOL_CALLS):
        # 每轮把 OpenAI 兼容 loop_messages 转成原生 contents。转换是幂等的，
        # 上一轮写入的 _gemini_thought_signatures 会自动还原成 thoughtSignature。
        system_instruction, contents = openai_messages_to_gemini_contents(loop_messages)
        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
            },
            # 让 Gemini 把思考过程作为独立 thought part 返回，而非塞进 content。
            "thinkingConfig": {"includeThoughts": True},
        }
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        if supports_sampling:
            payload["generationConfig"]["temperature"] = 0.6
            payload["generationConfig"]["topP"] = 0.9
        if supports_tools and gemini_tools:
            payload["tools"] = gemini_tools
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        # 流式状态
        gemini_state = {
            "reasoning_acc": "",
            "content_acc": "",
            "tool_calls": [],
            "thought_signatures": [],
            "finish_reason": None,
            "_round": _round,
        }
        current_stream = None  # "reasoning" / "content"
        round_leading_kind = None  # 与 OpenAI-compat 路径保持一致的语义

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

        received_any = False
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                async with session.post(
                        GEMINI_NATIVE_URL, headers=req_headers, json=payload
                ) as resp:
                    if resp.status not in (200, 201):
                        err_text = await resp.text()
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history,
                            status=resp.status, message=err_text,
                        )
                    # 原生 SSE 流：每行 "data: {json}\n\n"，结尾可能没有 [DONE]。
                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].lstrip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            logger.debug("[Gemini/native] 不可解析的 SSE 行: %r", data_str[:200])
                            continue

                        # 中流错误：把异常透出去，避免静默丢失工具调用配对
                        err = chunk.get("error")
                        if err:
                            raise RuntimeError(f"Gemini native stream error: {err}")

                        received_any = True
                        if chunk.get("usageMetadata"):
                            final_usage = chunk["usageMetadata"]

                        # prompt 级阻断（罕见）
                        pf = chunk.get("promptFeedback") or {}
                        if pf.get("blockReason"):
                            final_content = f"（Gemini 拒绝：{pf['blockReason']}）"
                            new_history_entries.append({"role": "assistant", "content": final_content})
                            break

                        cands = chunk.get("candidates") or []
                        if not cands:
                            continue
                        cand = cands[0]
                        if cand.get("finishReason"):
                            gemini_state["finish_reason"] = cand["finishReason"]
                        if cand.get("safetyRatings"):
                            # 不阻断；让用户看到带 safety 标签的内容
                            pass

                        content = cand.get("content") or {}
                        parts = content.get("parts") or []

                        # 跟踪本轮是否已声明工具调用，用于工具组合并/新建决策
                        tool_calls_before = len(gemini_state["tool_calls"])

                        for part in parts:
                            # 先把 part 合并到 state，再决定怎么喂 builder。
                            # 注意：functionCall part 不立刻调用 add_tool_item，
                            # 因为合并完才知道本轮工具总数与 id 顺序；统一在批
                            # 量处理时再 add_tool_item，避免按 part 顺序错位。
                            old_tool_count = len(gemini_state["tool_calls"])
                            merge_gemini_part_into_state(part, gemini_state)
                            new_tool_count = len(gemini_state["tool_calls"])

                            if part.get("thought") is True:
                                text = part.get("text") or ""
                                if text:
                                    if round_leading_kind is None:
                                        round_leading_kind = "content"
                                        if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                                            builder.finish_group(len(builder._tool_groups) - 1)
                                            await builder.flush(force=True)
                                    switch_stream("reasoning")
                                    builder.append_stream_delta(text)
                            elif "text" in part:
                                text = part.get("text") or ""
                                if text:
                                    if round_leading_kind is None:
                                        round_leading_kind = "content"
                                        if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                                            builder.finish_group(len(builder._tool_groups) - 1)
                                            await builder.flush(force=True)
                                    if round_leading_kind == "tool" and builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                                        builder.append_to_current_tool_group_text(text)
                                    else:
                                        switch_stream("content")
                                        builder.append_stream_delta(text)
                            elif "functionCall" in part and new_tool_count > old_tool_count:
                                # 新出现的工具调用：与 OpenAI-compat 流式路径一致的
                                # 时机——立刻 add_tool_item + request_flush，不等本轮结束。
                                if round_leading_kind is None:
                                    round_leading_kind = "tool"
                                elif round_leading_kind == "content" and builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                                    builder.finish_group(len(builder._tool_groups) - 1)
                                tc = gemini_state["tool_calls"][-1]
                                tc_id = tc["id"]
                                tc_name = tc["function"]["name"]
                                parsed_args = _safe_parse_args(tc["function"]["arguments"])
                                summary = _generate_initial_tool_summary(tc_name, parsed_args)
                                action_desc = _generate_action_description(tc_name, parsed_args)
                                builder.add_tool_item(
                                    tc_id,
                                    tc_name,
                                    summary,
                                    action_description=action_desc,
                                    fn_args=parsed_args,
                                )
                                builder.request_flush(force=False)
                            # thoughtSignature 单独 part / executableCode 等已由
                            # merge_gemini_part_into_state 处理；不需要再喂 builder。

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"[Gemini/native] round {_round} error: {e}")
            raise

        builder.end_stream()
        while builder.blocks and not builder.blocks[-1].strip() and builder.block_types[-1] in ("text", "reasoning"):
            builder.blocks.pop()
            builder.block_types.pop()

        if not received_any or (not gemini_state["content_acc"] and not gemini_state["tool_calls"] and not gemini_state["reasoning_acc"]):
            logger.warning(f"[gemini] 第 {_round + 1} 轮原生流式无有效内容，回退到非流式请求")
            try:
                non_stream_url = (
                    f"https://generativelanguage.googleapis.com/v1beta/"
                    f"models/{current_model}:generateContent"
                )
                non_stream_payload = dict(payload)
                non_stream_payload.pop("thinkingConfig", None)
                non_stream_payload["thinkingConfig"] = {"includeThoughts": True}
                async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                    async with session.post(
                            non_stream_url, headers=req_headers, json=non_stream_payload
                    ) as resp:
                        if resp.status not in (200, 201):
                            err_text = await resp.text()
                            raise aiohttp.ClientResponseError(
                                resp.request_info, resp.history,
                                status=resp.status, message=err_text,
                            )
                        data = await resp.json()
                cands = data.get("candidates") or []
                if cands:
                    parts = (cands[0].get("content") or {}).get("parts") or []
                    for part in parts:
                        merge_gemini_part_into_state(part, gemini_state)
                if data.get("usageMetadata"):
                    final_usage = data["usageMetadata"]
            except Exception as e:
                logger.exception(f"[Gemini/native] 非流式回退失败: {e}")
                gemini_state["content_acc"] = "请求失败，请稍后重试。"

        tool_calls_list = gemini_state["tool_calls"]
        content_acc = gemini_state["content_acc"]
        reasoning_acc = gemini_state["reasoning_acc"]
        thought_signatures = gemini_state["thought_signatures"]

        try:
            logger.info(
                f"[gemini] 第 {_round + 1} 轮原生返回: tool_calls={len(tool_calls_list)}, "
                f"content_len={len(content_acc.strip())}, reasoning_len={len(reasoning_acc.strip())}, "
                f"signatures={len(thought_signatures)}, finish={gemini_state['finish_reason']}"
            )
        except Exception:
            logger.exception("[gemini] 记录原生返回日志失败")

        for idx, tc in enumerate(tool_calls_list):
            if not tc.get("id"):
                tc["id"] = f"callgemini_{_round}_{idx}_{uuid.uuid4().hex[:8]}"
        _normalize_tool_call_arguments(tool_calls_list, "gemini", _round + 1)

        if not tool_calls_list and not content_acc.strip():
            content_acc = "（模型未返回任何内容）"
            gemini_state["content_acc"] = content_acc

        # 兼容部分 Gemini 模型把 function calling XML 写成普通文本的情况
        textual_tool_call = _contains_textual_tool_call(content_acc)
        if textual_tool_call:
            raw_textual_content = content_acc
            content_acc = _strip_textual_tool_calls(content_acc)
            gemini_state["content_acc"] = content_acc
            if not builder.replace_trailing_text(raw_textual_content, content_acc):
                logger.warning("[gemini] 未能在草稿中定位伪工具调用文本，已阻止其进入最终内容")

        if reasoning_acc:
            builder.finalize_reasoning_block(has_tool_calls=bool(tool_calls_list))
        await builder.flush()

        assistant_msg = build_assistant_msg_from_gemini_state(gemini_state)
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
            tool_call_count_ref, "gemini", builder, chat_id=builder.chat_id
        )
        await builder.rollover_at_turn_boundary(start_next_draft=True)

        if status == "over_limit":
            synth_system_text = (
                f"System: Maximum tool calls ({MAX_TOOL_CALLS}) reached for this turn. "
                "Tool usage is now DISABLED. Please immediately summarize what you have "
                "successfully done so far, explicitly state what failed or what is left "
                "to do, and ask the user if they want to continue the operation in the next turn."
            )
            synth_messages = loop_messages + [{"role": "user", "content": synth_system_text}]
            synth_system_instruction, synth_contents = openai_messages_to_gemini_contents(synth_messages)
            synth_payload: dict = {
                "contents": synth_contents,
                "generationConfig": {"maxOutputTokens": max_tokens},
                "thinkingConfig": {"includeThoughts": True},
            }
            if synth_system_instruction:
                synth_payload["systemInstruction"] = synth_system_instruction
            if supports_sampling:
                synth_payload["generationConfig"]["temperature"] = 0.6
                synth_payload["generationConfig"]["topP"] = 0.9
            try:
                builder.begin_stream_text()
                synth_text = ""
                async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                    async with session.post(
                            GEMINI_NATIVE_URL, headers=req_headers, json=synth_payload
                    ) as resp:
                        if resp.status not in (200, 201):
                            raise RuntimeError(await resp.text())
                        async for raw_line in resp.content:
                            line = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                            if not line.startswith("data:"):
                                continue
                            data_str = line[len("data:"):].lstrip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            if chunk.get("error"):
                                raise RuntimeError(chunk["error"])
                            if chunk.get("usageMetadata"):
                                final_usage = chunk["usageMetadata"]
                            cands = chunk.get("candidates") or []
                            if not cands:
                                continue
                            parts = (cands[0].get("content") or {}).get("parts") or []
                            for part in parts:
                                if part.get("thought") is True:
                                    continue  # 终局总结不再渲染思考块
                                if "text" in part:
                                    text = part.get("text") or ""
                                    if text:
                                        synth_text += text
                                        builder.append_stream_delta(text)
                raw_synth_content = builder.end_stream_text() or synth_text
                final_content = _strip_textual_tool_calls(raw_synth_content)
                if final_content != raw_synth_content:
                    builder.replace_trailing_text(raw_synth_content, final_content)
                if not final_content:
                    final_content = _tool_limit_summary()
                    builder.add_text(final_content)
            except Exception as synth_err:
                logger.warning(f"Gemini 原生合成流失败: {synth_err}")
                try:
                    builder.end_stream_text()
                except Exception:
                    pass
                final_content = _tool_limit_summary()
                builder.add_text(final_content)
            new_history_entries.append({"role": "assistant", "content": final_content or ""})
            if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                builder.finish_group(len(builder._tool_groups) - 1)
            await builder.rollover_at_turn_boundary(start_next_draft=False)
            break

    if final_content is None:
        final_content = _tool_limit_summary()
        builder.add_text(final_content)
        new_history_entries.append({"role": "assistant", "content": final_content})
        if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
            builder.finish_group(len(builder._tool_groups) - 1)
        await builder.rollover_at_turn_boundary(start_next_draft=False)
    return final_content, final_usage, new_history_entries


# 旧名兼容：保留 _agentic_loop_gemini_openai_compat 作为别名，避免外部引用方
# （如 ai_handlers 旧 import）出现 AttributeError。新代码应使用新名。
_agentic_loop_gemini_openai_compat = _agentic_loop_gemini_native


async def _agentic_loop_native_image(
        client: AsyncOpenAI,
        current_model: str,
        messages: list,
        builder: "RichMessageBuilder",
        chat_id: int,
) -> tuple[str | None, object | None, list]:
    model_info = SUPPORTED_MODELS.get(current_model)
    provider = model_info.provider if model_info else ""  # <-- 新增 provider

    def _extract_prompt_and_image_urls_from_messages(msgs: list) -> tuple[str, list[str]]:
        last_user_msg = None
        for item in reversed(msgs):
            if item.get("role") == "user":
                last_user_msg = item
                break

        if not last_user_msg:
            return "", []

        content = last_user_msg.get("content")
        prompt_parts: list[str] = []
        image_urls: list[str] = []

        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    text = str(part.get("text") or "").strip()
                    if text:
                        prompt_parts.append(text)
                elif part_type in ("image_url", "image"):
                    image_url = ""
                    if isinstance(part.get("image_url"), dict):
                        image_url = str(part["image_url"].get("url") or "").strip()
                    else:
                        image_url = str(part.get("url") or "").strip()
                    if image_url:
                        image_urls.append(image_url)
        elif isinstance(content, str):
            prompt_parts.append(content.strip())

        prompt = "\n".join(p for p in prompt_parts if p).strip()
        return prompt, image_urls

    max_tokens = model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192
    prompt_text, image_urls = _extract_prompt_and_image_urls_from_messages(messages)

    clean_prompt = _clean_prompt_for_image_model(prompt_text)

    try:
        response = None
        used_endpoint = "/v1/chat/completions"

        if provider == "modelscope":
            response_json, endpoint, error_detail, status_code, request_id = await _request_modelscope_native_image(
                # 修复 BUG：clean_prompt 已计算但未传入 _request_modelscope_native_image，
                # 后者实际收到的是原始 prompt_text。结果是 _clean_prompt_for_image_model
                # 想要剥离的 UI 元数据（chat history 标记、reasoning marker 等）会
                # 原样泄漏到图像生成模型，可能被当作 prompt 的一部分影响生成结果。
                prompt=clean_prompt,
                image_urls=image_urls,
                num_images=1,
                model=current_model,  # 传入当前模型 ID
            )
            used_endpoint = f"/v1{endpoint}"
            if response_json is None:
                if _is_content_safety_error(error_detail):
                    logger.info("[NativeImage] 请求被内容安全策略拦截: %s", error_detail[:200])
                    error_notice = _format_image_safety_notice(detail=error_detail, model=current_model)
                else:
                    error_notice = _format_api_error_notice(
                        api_name="ModelScope 图像接口",
                        error_code=status_code,
                        endpoint=used_endpoint,
                        model=current_model,
                        detail=error_detail,
                        request_id=request_id,
                    )
                return f"IMAGE_ERROR:{error_notice}", None, []

            class _ImageResponse:
                def __init__(self, payload: dict):
                    self._payload = payload
                    self.choices = [type("Choice", (), {"message": type("Msg", (), {})(), "finish_reason": None})()]
                    self.usage = payload.get("usage")

            response = _ImageResponse(response_json)
            image_bytes_list = await _response_items_to_bytes(response_json)

            if not image_bytes_list:
                try:
                    json_preview = json.dumps(response_json, ensure_ascii=False, indent=2)
                except Exception:
                    json_preview = str(response_json)
                logger.debug(
                    "[NativeImage/ModelScope] no image bytes extracted, raw response preview=%r",
                    json_preview[:5000],
                )
                error_notice = _format_api_error_notice(
                    api_name="ModelScope 图像接口",
                    error_code=200,
                    endpoint=used_endpoint,
                    model=current_model,
                    detail="接口返回成功，但未找到可用图片数据。",
                )
                return f"IMAGE_ERROR:{error_notice}", None, []

            uploaded_urls = []
            for idx, img_bytes in enumerate(image_bytes_list):
                key = f"generated/{uuid.uuid4().hex}_{idx}.png"
                url = await upload_bytes_to_r2(img_bytes, key, "image/png")
                if url:
                    uploaded_urls.append(url)

            if uploaded_urls:
                img_tags = "".join(f'<img src="{u}"/>' for u in uploaded_urls)
                caption_text = _format_image_metadata_caption(image_bytes_list[0],
                                                              current_model) if image_bytes_list else "Generated image"
                # 单图用 <figure>，多图用 <tg-slideshow> 轮播
                if len(uploaded_urls) == 1:
                    rich_html = f'<figure>{img_tags}<figcaption>{escape_html(caption_text)}</figcaption></figure>'
                else:
                    rich_html = f'<tg-slideshow>{img_tags}<figcaption>{escape_html(caption_text)}</figcaption></tg-slideshow>'
                await send_rich_html_message(chat_id, rich_html)
                final_notice = caption_text
            else:
                error_notice = _format_api_error_notice(
                    api_name="ModelScope 图像接口",
                    error_code=200,
                    endpoint=used_endpoint,
                    model=current_model,
                    detail="接口返回成功，但图片上传失败。",
                )
                return f"IMAGE_ERROR:{error_notice}", None, []

            final_content = f"IMAGE_SENT:{final_notice}" if final_notice else "IMAGE_SENT"
            history_content = f"[图片已生成] 指令: {clean_prompt or prompt_text or '(无)'} | {caption_text}"
            new_entries = [{"role": "assistant", "content": history_content}]
            return final_content, getattr(response, "usage", None), new_entries

        # ---- 非 ModelScope 的其他提供商（OpenRouter 等） ----
        try:
            response = await client.chat.completions.create(
                model=current_model,
                messages=messages,
                temperature=0.6,
                max_tokens=max_tokens,
                extra_body={"modalities": ["image", "text"], "provider": OPENROUTER_PROVIDER_PREFERENCES},
                stream=False,
            )
        except Exception as e:
            err_text = str(e)
            if "output modalities" not in err_text and "modalities" not in err_text:
                raise
            logger.warning(f"Native image model does not support image+text output, retrying image-only: {e}")
            response = await client.chat.completions.create(
                model=current_model,
                messages=messages,
                temperature=0.6,
                max_tokens=max_tokens,
                extra_body={"modalities": ["image"], "provider": OPENROUTER_PROVIDER_PREFERENCES},
                stream=False,
            )
    except Exception as e:
        logger.exception(f"Native image model request failed: {e}")
        if hasattr(e, "response") and hasattr(e.response, "text"):
            try:
                body = await e.response.text()
                logger.error(f"Response body: {body[:1000]}")
            except Exception:
                pass
        err_str = str(e)
        if _is_content_safety_error(err_str):
            logger.info("[NativeImage] 请求被内容安全策略拦截（异常路径）: %s", err_str[:200])
            error_notice = _format_image_safety_notice(detail=err_str, model=current_model)
        else:
            error_notice = await get_error_notification_message(
                chat_id,
                error_code=getattr(e, "status_code", getattr(e, "status", 500)),
                error_message=err_str,
                api_name="图像请求",
                exception=e,
                endpoint="/v1/images/generations" if image_urls else "/v1/chat/completions",
                model=current_model,
            )
        return f"IMAGE_ERROR:{error_notice}", None, []

    choice = response.choices[0]
    finish_reason = str(getattr(choice, "finish_reason", "") or "")
    content = _extract_native_message_text(getattr(choice.message, "content", ""))
    refusal_text = _extract_native_refusal_text(choice.message)

    # 使用统一的 _extract_image_items 提取图片
    try:
        msg_dump = choice.message.model_dump()
        images = _extract_image_items(msg_dump)
        # 如果返回空，尝试直接从 images 字段读取（兼容旧方式）
        if not images:
            images = getattr(choice.message, "images", []) or []
    except Exception:
        images = []

    image_bytes_list = []
    for img_data in images:
        img_url = img_data.get("image_url", {}).get("url")
        if not img_url:
            continue
        if img_url.startswith("data:image"):
            try:
                header, base64_data = img_url.split(",", 1)
                img_bytes = base64.b64decode(base64_data)
                image_bytes_list.append(img_bytes)
            except Exception as e:
                logger.error(f"Base64 decode failed: {e}")
        elif img_url.startswith("http"):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_url, timeout=30) as resp:
                        if resp.status == 200:
                            img_bytes = await resp.read()
                            image_bytes_list.append(img_bytes)
                        else:
                            logger.warning(f"Download image {img_url} failed: {resp.status}")
            except Exception as e:
                logger.error(f"Download image {img_url} error: {e}")

    uploaded_urls = []
    for idx, img_bytes in enumerate(image_bytes_list):
        key = f"generated/{uuid.uuid4().hex}_{idx}.png"
        url = await upload_bytes_to_r2(img_bytes, key, "image/png")
        if url:
            uploaded_urls.append(url)

    if uploaded_urls:
        img_tags = "".join(f'<img src="{u}"/>' for u in uploaded_urls)
        caption_text = _format_image_metadata_caption(image_bytes_list[0],
                                                      current_model) if image_bytes_list else "Generated image"
        # 单图用 <figure>，多图用 <tg-slideshow> 轮播
        if len(uploaded_urls) == 1:
            rich_html = f'<figure>{img_tags}<figcaption>{escape_html(caption_text)}</figcaption></figure>'
        else:
            rich_html = f'<tg-slideshow>{img_tags}<figcaption>{escape_html(caption_text)}</figcaption></tg-slideshow>'
        await send_rich_html_message(chat_id, rich_html)
        final_notice = caption_text
    else:
        final_notice = _format_native_image_notice(
            content_text=content,
            refusal_text=refusal_text,
            finish_reason=finish_reason,
        )
        safe_notice_html = escape_html(final_notice).replace("\n", "<br/>")
        await send_rich_html_message(chat_id, safe_notice_html)

    final_content = f"IMAGE_SENT:{final_notice}" if final_notice else "IMAGE_SENT"
    if uploaded_urls:
        history_content = f"[图片已生成] {content[:200] if content else ''} | {caption_text}".strip(' |')
    else:
        history_content = final_notice or "（已生成图片）"
    new_entries = [{"role": "assistant", "content": history_content}]
    return final_content, getattr(response, "usage", None), new_entries


async def _agentic_loop_native_video(
        client: AsyncOpenAI,  # 保留参数，但可能不用
        current_model: str,
        messages: list,
        builder: "RichMessageBuilder",
        chat_id: int,
) -> tuple[str | None, object | None, list]:
    """
    处理视频生成模型。
    目前支持 Agnes 和 OpenRouter。
    """
    # 提取 prompt
    prompt = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                prompt = content
            elif isinstance(content, list):
                texts = [part.get("text") for part in content if part.get("type") == "text"]
                prompt = " ".join(texts)
            break
    if not prompt:
        return "VIDEO_ERROR:未提供提示词", None, []

    # 可选：解析时长
    # re 已在文件顶部 import，删除本地的 `import re`（之前是冗余 import）。
    duration = 5
    # 时长解析：兼容中英文（"5秒" 与 "5 seconds" / "5s"）
    match = re.search(r'(\d+)\s*(?:秒|seconds?|secs?|s)\b', prompt, re.IGNORECASE)
    if match:
        try:
            duration = int(match.group(1))
            duration = max(3, min(duration, 30))
        except ValueError:
            duration = 5

    # 获取模型信息，确定 provider
    model_info = SUPPORTED_MODELS.get(current_model)
    if not model_info:
        return f"VIDEO_ERROR:未知模型 {current_model}", None, []

    provider = model_info.provider
    video_url = None
    error = None
    video_meta: Optional[dict] = None

    if provider == "agnes":
        video_url, error, video_meta = await _request_agnes_video(prompt, duration, current_model)
    elif provider == "openrouter":
        video_url, error, video_meta = await _request_openrouter_video(prompt, duration, current_model)
    else:
        return f"VIDEO_ERROR:不支持的视频提供商 {provider}", None, []

    if error:
        return f"VIDEO_ERROR:{error}", None, []

    if not video_url:
        return "VIDEO_ERROR:未获取到视频链接", None, []

    # ---------- 发送视频富文本消息（与图片生成路径保持一致） ----------
    # 与图片路径一样：先把视频字节下载下来，上传到 R2 并带正确的 Content-Type: video/mp4，
    # 再用 R2 URL 拼 <figure><video src=...></video><figcaption>...</figcaption></figure>
    # 通过 sendRichMessage 发送。这样可保证 Telegram 能拿到合法的 video MIME，
    # 不会触发 400 RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND（该错误并非来自 HTML 标签格式，
    # 而是来自 Telegram 拉取不到匹配 MIME 的媒体）。
    final_video_url = video_url
    video_bytes_len = 0
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as dl_session:
            async with dl_session.get(video_url) as dl_resp:
                if dl_resp.status == 200:
                    # 修复 OOM 风险：此前直接 await dl_resp.read() 把整个视频字节读进
                    # 内存，没有大小上限。一个失控/恶意的上游返回 1GB+ 的"视频"会把
                    # 进程拖垮。这里限制为 200MB（足够任何合理的 720p 视频片段），
                    # 超限则拒绝并回退到原始 URL。
                    _MAX_VIDEO_BYTES = 200 * 1024 * 1024
                    video_bytes = await dl_resp.content.read(_MAX_VIDEO_BYTES + 1)
                    if len(video_bytes) > _MAX_VIDEO_BYTES:
                        logger.warning(
                            "[NativeVideo] 视频体积超限 (>%s)，跳过 R2 上传，回退原始 URL: %s",
                            _MAX_VIDEO_BYTES, str(video_url)[:200],
                        )
                        video_bytes = b""
                        video_bytes_len = 0
                    else:
                        video_bytes_len = len(video_bytes)
                        logger.debug(
                            "[NativeVideo] video downloaded: %d bytes from %s",
                            video_bytes_len, str(video_url)[:200],
                        )
                        r2_key = f"generated/{uuid.uuid4().hex}.mp4"
                        r2_url = await upload_bytes_to_r2(video_bytes, r2_key, "video/mp4")
                        if r2_url:
                            final_video_url = r2_url
                        else:
                            logger.warning("[NativeVideo] R2 上传失败，回退使用原始视频 URL")
                else:
                    logger.warning(
                        "[NativeVideo] 视频下载非 200: status=%s url=%s，回退使用原始 URL",
                        dl_resp.status, str(video_url)[:200],
                    )
    except Exception as e:
        # 修复：原 logger.exception 把 %s 视频字符串作为参数但 %s 占位符只有
        # 一个，导致 e 本身被 logger 内部忽略。改为把 e 也传入。
        logger.exception(
            "[NativeVideo] 视频下载/上传异常，回退使用原始 URL: url=%s err=%s",
            str(video_url)[:200], e,
        )

    # 构造富文本：用 <figure>+<video>+<figcaption> 的文档推荐写法（视频只能作为独立 media block）
    # caption 走与图片一致的“元数据”风格（分辨率/帧率/帧数/大小/模型），不再附提示词。
    if video_bytes_len == 0 and video_meta:
        # 下载失败时退而用 Agnes 报告的 perf_output_size 作为大小估算
        out_size = video_meta.get("perf_output_size") if isinstance(video_meta, dict) else None
        video_bytes_len = int(out_size) if isinstance(out_size, (int, float)) else 0
    caption_text = _format_video_metadata_caption(
        file_size_bytes=video_bytes_len,
        model=current_model,
        meta=video_meta if isinstance(video_meta, dict) else None,
    )
    video_html = (
        f'<figure><video src="{final_video_url}"></video>'
        f'<figcaption>{escape_html(caption_text)}</figcaption></figure>'
    )
    send_ok = await send_rich_html_message(chat_id, video_html)
    if not send_ok:
        logger.error(
            "视频已生成，但 sendRichMessage 发送失败 final_video_url=%s",
            str(final_video_url)[:200],
        )
        return "VIDEO_ERROR:视频发送失败", None, []

    # 生成历史记录
    history_content = f"[视频已生成] 提示词: {prompt[:200]}" if prompt else "[视频已生成]"
    new_entries = [{"role": "assistant", "content": history_content}]

    final_content = f"VIDEO_SENT:{prompt[:100]}"  # 用于上游判断
    return final_content, None, new_entries



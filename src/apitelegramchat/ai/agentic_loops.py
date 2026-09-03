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
from typing import TYPE_CHECKING, Optional
from openai import AsyncOpenAI, BadRequestError

from apitelegramchat.config import (
    GEMINI_API_KEY,
    SUPPORTED_MODELS,
    get_sampling_params,
    get_reasoning_request_fields,
)
from apitelegramchat.utils import get_logger, escape_html, send_rich_html_message
from apitelegramchat.chat_actions import (
    chat_action_scope,
    start_chat_action,
    stop_chat_action,
)
from apitelegramchat.s3_utils import upload_bytes_to_r2

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
from apitelegramchat.ai.strict_tools import (
    gemini_strict_env_enabled,
    looks_like_strict_tool_rejection,
    mark_strict_tools_rejected,
    strict_tools_for_request,
)

# Anthropic 原生 Messages API 专用循环：独立实现，位于 anthropic_bridge.py
# （职责分离 + 避免本已很大的文件继续膨胀）。此处重导出保持调用方
# "from apitelegramchat.ai.agentic_loops import _agentic_loop_anthropic"
# 这一路径可用，与 _agentic_loop_openai_compat / _agentic_loop_gemini_openai_compat
# 并列，风格一致。
from apitelegramchat.ai.anthropic_bridge import _agentic_loop_anthropic  # noqa: F401

if TYPE_CHECKING:
    # 仅供类型注解使用；运行时由调用方传入，避免运行时循环导入。
    from apitelegramchat.ai.rich_message_builder import RichMessageBuilder

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


def _openrouter_session_id(chat_id: object) -> str:
    """为 OpenRouter 生成稳定的 per-chat 粘性路由键（≤256 字符）。

    背景：OpenRouter 的默认会话识别靠"首条 system + 首条非 system 消息"
    哈希。本项目的上下文窗口虽然只在自动压缩事件中收缩（事件之间前缀
    字节稳定，见 context_window.py），但事件发生时历史仍会被重写
    （滚动摘要合并、工具负载归档为指针）。显式传 session_id 后：粘性
    路由从第一次请求就生效（无需先观察到缓存命中），且不随压缩事件
    漂移；对经 OpenRouter 转发的 Z.AI/GLM 还会作为会话亲和键下发，
    进一步提升缓存命中。
    """
    if chat_id is None:
        return ""
    key = f"tg-chat-{chat_id}".strip()
    return key[:256]


def _openrouter_extra_body(
    chat_id: object = None,
    supports_prompt_cache: bool = False,
) -> dict:
    body: dict = {"provider": OPENROUTER_PROVIDER_PREFERENCES.copy()}
    session_key = _openrouter_session_id(chat_id)
    if session_key:
        body["session_id"] = session_key
    # Anthropic 系模型的"自动缓存"：顶层 cache_control 由网关翻译成打在
    # 最后一个可缓存块上的断点，并随对话增长自动前移。这让 agentic loop
    # 的每一轮（含 tool 结果之后的内容）都能作为缓存前缀被下一轮命中，
    # 下一轮用户请求也能直接命中上一轮的完整前缀（含工具调用中段）。
    # 与 _apply_cache_control 的显式断点叠加后总数不超过 Anthropic 的
    # 4 断点上限（3 显式 + 1 自动）。
    if supports_prompt_cache:
        body["cache_control"] = {"type": "ephemeral"}
    return body


def _merged_extra_body(
    api_label: str,
    reasoning_extra: Optional[dict],
    chat_id: object = None,
    supports_prompt_cache: bool = False,
) -> Optional[dict]:
    """
    合并 OpenRouter 路由偏好与推理控制字段，返回应传给 create() 的
    extra_body；两个来源都为空时返回 None（不发送 extra_body）。

    reasoning_extra 来自 config.get_reasoning_request_fields()，例如：
      openrouter  -> {"reasoning": {"enabled": True, "effort": "high"}}
      glm         -> {"thinking": {"type": "enabled"}}
      modelscope  -> {"enable_thinking": True}
    这些字段均不会与 provider 键冲突，直接字典合并即可。
    """
    body = None
    if api_label == "openrouter":
        body = _openrouter_extra_body(
            chat_id=chat_id,
            supports_prompt_cache=supports_prompt_cache,
        )
    if reasoning_extra:
        body = {**(body or {}), **reasoning_extra}
    return body


# =====================================================================
# Prompt cache 命中观测：把各家的缓存命中字段统一提取成一份小字典，
# 在每轮请求结束后打一行 INFO 日志，让"缓存命中率"变成可度量的指标。
# 字段来源：
#   OpenRouter / OpenAI : usage.prompt_tokens_details.cached_tokens
#   其他兼容 provider   : cache read 字段（如 provider 返回）
# =====================================================================
def _extract_cache_usage(usage) -> dict:
    if usage is None:
        return {}

    def _num(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        return None

    if isinstance(usage, dict):
        prompt = _num(usage.get("prompt_tokens"))
        details = usage.get("prompt_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        cached = _num(details.get("cached_tokens"))
        if cached is None:
            cached = _num(usage.get("cache_read_input_tokens"))
        if cached is None:
            cached = _num(usage.get("prompt_cache_hit_tokens"))
    else:
        prompt = _num(getattr(usage, "prompt_tokens", None))
        details = getattr(usage, "prompt_tokens_details", None)
        cached = _num(getattr(details, "cached_tokens", None)) if details is not None else None
        extra = getattr(usage, "model_extra", None)
        extra = extra if isinstance(extra, dict) else {}
        if cached is None:
            cached = _num(extra.get("cache_read_input_tokens"))
        if cached is None:
            cached = _num(extra.get("prompt_cache_hit_tokens"))

    stats: dict = {}
    if prompt:
        stats["prompt_tokens"] = prompt
        stats["cached"] = cached if cached is not None else 0
        stats["hit_ratio"] = round(stats["cached"] / max(1, prompt), 3)
    else:
        # 极端情况：usage 中没有 prompt_tokens 但有缓存字段，仍如实显示。
        if cached is not None:
            stats["cached"] = cached
    return stats


def _log_cache_usage(api_label: str, usage) -> None:
    """每轮请求结束时打一行缓存命中摘要（无缓存字段时静默跳过）。"""
    try:
        stats = _extract_cache_usage(usage)
        if stats:
            logger.info("[%s] prompt cache usage: %s", api_label, stats)
    except Exception:
        # 观测日志绝不影响主流程
        logger.debug("cache usage 日志记录失败", exc_info=True)


async def _agentic_loop_openai_compat(
        client: AsyncOpenAI, current_model: str, messages: list, api_label: str,
        builder: "RichMessageBuilder", tools: list = None, supports_tools: bool = True,
        journal: list = None,
) -> tuple[str | None, object | None, list]:
    if tools is None:
        from apitelegramchat.search_engine import SEARCH_TOOLS
        tools = SEARCH_TOOLS
    loop_messages = list(messages)
    final_content = None
    final_usage = None
    tool_call_count_ref = [0]
    # journal：由 get_ai_response 注入的轮次日志（打断保全用，见 turn_recovery.py）。
    # 注入时循环直接往里追加，轮次被打断时已完成的消息不至于丢失。
    new_history_entries = journal if journal is not None else []
    plain_text_tool_attempts = 0
    parallel_tool_calls = True

    model_info = SUPPORTED_MODELS.get(current_model)
    max_tokens = model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192
    # 采样与推理控制统一来自 config.py（含 per-model 覆盖），禁止在此硬编码。
    sampling_params = get_sampling_params(model_info)
    reasoning_top, reasoning_extra = get_reasoning_request_fields(model_info, api_label)
    prompt_cache_enabled = bool(model_info and model_info.supports_prompt_cache)

    # L0 预防层（主流：OpenAI Structured Outputs）：对能安全规范化的工具
    # 注入 strict:true + 递归 schema 规范化（全部必填 + additionalProperties:false
    # + 可选字段可空）。per-tool best-effort，schema 复杂（union/anyOf）的工具
    # 原样发送；网关拒绝时运行时自动降级（见下方 except BadRequestError）。
    # 手动总开关：环境变量 DISABLE_STRICT_TOOL_SCHEMA=1。
    request_tools = strict_tools_for_request(api_label, tools)

    for _round in range(MAX_TOOL_CALLS):
        added_tool_indices = set()
        last_arg_len = {}

        content_acc = ""
        reasoning_acc = ""
        tool_calls_acc: dict = {}
        in_reasoning = False
        current_stream = None
        received_any = False
        # v2.5：本轮流结束原因（length / stop / tool_calls / content_filter…）。
        # 初始 None = 尚未见到任何终止事件；流被完整消费却仍为 None 时，
        # 归一化层会把 ""（断流证据）传入诊断信封——空参数/截断参数的
        # 根因自此可被准确区分（输出上限 vs 断流 vs 模型自身语法错）。
        stream_finish_reason: Optional[str] = None
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
            create_params.update(sampling_params)
            create_params.update(reasoning_top)
            if supports_tools and tools:
                create_params["tools"] = request_tools
                create_params["tool_choice"] = "auto"
                create_params["parallel_tool_calls"] = parallel_tool_calls
            extra_body = _merged_extra_body(
                api_label, reasoning_extra,
                chat_id=builder.chat_id,
                supports_prompt_cache=prompt_cache_enabled,
            )
            if extra_body is not None:
                create_params["extra_body"] = extra_body

            # 某些聚合网关会在长工具链后的首个 SSE 事件前沉默较久。
            # 只有尚未收到任何增量时，重试相同请求才是幂等且安全的；一旦已经向
            # 用户或工具状态写入增量，必须直接抛出，避免重放半个模型回合。
            for stream_attempt in range(2):
                try:
                    comp_stream = await client.chat.completions.create(**create_params)
                    # typing 状态：仅在本轮真实消费流式增量（思考/文本字段）
                    # 期间显示——“模型正在打字”。状态每 4 秒循环重发：
                    # Telegram 的 chat action 最多持续约 5 秒，且 bot 发出的
                    # 任何消息（含草稿刷新）都会清除指示；/show on 时草稿
                    # 流式本身就是输入可视化，指示被反复清除属预期设计。
                    # 非流式回退（下方 stream=False）不触发 typing：一次性
                    # 返回完整文本等价于直接粘贴发送，没有输入过程。
                    await start_chat_action(builder.chat_id, "typing")
                    async for chunk in comp_stream:
                        received_any = True
                        if getattr(chunk, "usage", None):
                            final_usage = chunk.usage
                        choices = chunk.choices or []
                        if not choices:
                            continue
                        fr = getattr(choices[0], "finish_reason", None)
                        if fr:
                            stream_finish_reason = str(fr)
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
                                # 但参数中一旦解析出模型提交的简短描述（_description/_summary），
                                # 或完整 JSON 解析出 query/command/url 等字段，就立即更新摘要上屏，
                                # 不再等到整段参数流结束后才由工具批次补写。
                                current_args = tc.get("function", {}).get("arguments", "")
                                current_len = len(current_args)
                                if current_len - last_arg_len.get(idx, 0) >= 20:
                                    last_arg_len[idx] = current_len
                                    parsed_args = _safe_parse_args(current_args)
                                    builder.update_tool_args(tc_id, parsed_args)
                    break
                except BadRequestError as exc:
                    # strict 工具 schema 被网关拒绝（聚合网关对 strict 的支持
                    # 参差不齐，OpenRouter/ModelScope/agnes 等转发厂商行为各异）：
                    # 标记该 api_label（进程内记忆），摘除 strict 后用原始
                    # schema 立即重试一次，后续轮次不再撞墙。报错文本不指向
                    # schema/strict 时按原样抛出，不误降级。
                    if (request_tools is not tools
                            and looks_like_strict_tool_rejection(str(exc))):
                        mark_strict_tools_rejected(api_label, str(exc))
                        request_tools = tools
                        create_params["tools"] = tools
                        continue
                    raise
                except httpx.ReadTimeout:
                    if received_any or stream_attempt >= 1:
                        raise
                    logger.warning(
                        "[%s] 第 %s 轮模型流在首个增量前读取超时，等待后重试一次",
                        api_label, _round + 1,
                    )
                    await asyncio.sleep(1.0)
                finally:
                    # 无论本轮流式正常结束、读取超时重试还是异常/取消，
                    # 消费流式增量的过程已结束，typing 随之熄灭。
                    await stop_chat_action(builder.chat_id, "typing")
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
                fallback_params.update(sampling_params)
                fallback_params.update(reasoning_top)
                if supports_tools and tools:
                    fallback_params["tools"] = request_tools
                    fallback_params["tool_choice"] = "auto"
                    fallback_params["parallel_tool_calls"] = parallel_tool_calls
                fallback_extra_body = _merged_extra_body(
                    api_label, reasoning_extra,
                    chat_id=builder.chat_id,
                    supports_prompt_cache=prompt_cache_enabled,
                )
                if fallback_extra_body is not None:
                    fallback_params["extra_body"] = fallback_extra_body

                # 非流式回退：create 带同样的 strict 降级重试（与流式路径
                # 同一套逻辑——网关拒绝 strict schema 时摘除后立即重试一次）。
                try:
                    resp = await client.chat.completions.create(**fallback_params)
                except BadRequestError as exc:
                    if (request_tools is not tools
                            and looks_like_strict_tool_rejection(str(exc))):
                        mark_strict_tools_rejected(api_label, str(exc))
                        request_tools = tools
                        fallback_params["tools"] = tools
                        resp = await client.chat.completions.create(**fallback_params)
                    else:
                        raise
                # 非流式回退同样需要捕获 usage，否则本轮缓存命中统计会丢失
                if getattr(resp, "usage", None):
                    final_usage = resp.usage
                msg = resp.choices[0].message
                fr = str(getattr(resp.choices[0], "finish_reason", "") or "")
                if fr:
                    stream_finish_reason = fr
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
        # v2.5：流被完整消费却从未见到终止事件，且本轮确实产出了工具调用 →
        # 记为 ""（断流证据，非"无信息"），供归一化层定性空参数/截断参数。
        if stream_finish_reason is None and tool_calls_list:
            stream_finish_reason = ""
        try:
            tool_call_names = [tc.get("function", {}).get("name", "") or "" for tc in tool_calls_list]
            tool_call_ids = [tc.get("id", "") or "" for tc in tool_calls_list]
            logger.info(
                f"[{api_label}] 第 {_round + 1} 轮模型原始返回: tool_calls={len(tool_calls_list)}, "
                f"ids={tool_call_ids}, names={tool_call_names}, content_len={len(content_acc.strip())}, "
                f"reasoning_len={len(reasoning_acc.strip())}, finish_reason={stream_finish_reason!r}"
            )
        except Exception:
            logger.exception(f"[{api_label}] 记录 tool_calls 日志失败")
        # 每轮请求结束后立即打印本轮缓存命中统计。
        # 旧实现把 _log_cache_usage 放在 for 循环外面，且 final_usage 每轮被覆盖，
        # 导致多轮 agentic 循环只打印最后一轮的缓存统计，中间轮次的命中情况不可观测。
        # 此处每轮独立打印；final_usage 仍保留最后一轮的值供返回值（token 台账）使用。
        _log_cache_usage(api_label, final_usage)
        for idx, tc in enumerate(tool_calls_list):
            if not tc.get("id"):
                tc["id"] = f"call_{_round}_{idx}_{uuid.uuid4().hex[:8]}"
        _normalize_tool_call_arguments(
            tool_calls_list, api_label, _round + 1,
            stream_finish_reason=stream_finish_reason)

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
            builder.finalize_reasoning_block()
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
            tool_call_count_ref, api_label, builder, chat_id=builder.chat_id,
            tools=tools,
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
            synth_params.update(sampling_params)
            synth_params.update(reasoning_top)
            synth_extra_body = _merged_extra_body(
                api_label, reasoning_extra,
                chat_id=builder.chat_id,
                supports_prompt_cache=prompt_cache_enabled,
            )
            if synth_extra_body is not None:
                synth_params["extra_body"] = synth_extra_body
            try:
                synth_stream = await client.chat.completions.create(**synth_params)
                # 合成总结同样走流式输出：消费增量期间显示 typing（同主流语义）。
                await start_chat_action(builder.chat_id, "typing")
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
                    logger.debug("_agentic_loop_openai_compat 内部忽略的异常", exc_info=True)
                    pass
                final_content = _tool_limit_summary()
                builder.add_text(final_content)
            finally:
                await stop_chat_action(builder.chat_id, "typing")
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


async def _agentic_loop_gemini_openai_compat(
        current_model: str,
        messages: list,
        builder: "RichMessageBuilder",
        tools: list = None,
        supports_tools: bool = True,
        journal: list = None,
) -> tuple[str | None, object | None, list]:
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

    # L0 预防层（默认关闭）：Gemini OpenAI-compat 层官方文档未承诺对
    # strict 工具 schema 的支持，默认保持 cleaned_tools 原样（现有 schema
    # 在该端点已稳定工作）。需要实验时设置 ENABLE_STRICT_TOOL_SCHEMA_GEMINI=1，
    # 同样带网关拒绝自动降级（见下方请求重试结构）。
    gemini_strict_tools = (
        strict_tools_for_request("gemini", cleaned_tools)
        if gemini_strict_env_enabled() and cleaned_tools else cleaned_tools
    )

    model_info = SUPPORTED_MODELS.get(current_model)
    max_tokens = model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192
    # 采样与推理控制统一来自 config.py。Gemini 官方 OpenAI 兼容层：
    # reasoning_effort 为顶层字段；google.thinking_config.thinkingBudget
    # 通过 extra_body 下发（本循环用原始 JSON payload，等价于顶层 google 键）。
    sampling_params = get_sampling_params(model_info)
    reasoning_top, reasoning_extra = get_reasoning_request_fields(model_info, "gemini")

    GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    req_headers = {
        "Authorization": f"Bearer {GEMINI_API_KEY}",
        "Content-Type": "application/json",
    }

    loop_messages = list(messages)
    final_content: str | None = None
    final_usage = None
    tool_call_count_ref = [0]
    # journal：由 get_ai_response 注入的轮次日志（打断保全用）。
    new_history_entries: list = journal if journal is not None else []

    for _round in range(MAX_TOOL_CALLS):
        payload: dict = {
            "model": current_model,
            "messages": loop_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        payload.update(sampling_params)
        payload.update(reasoning_top)
        if reasoning_extra:
            payload.update(reasoning_extra)
        if supports_tools and gemini_strict_tools:
            payload["tools"] = gemini_strict_tools
            payload["tool_choice"] = "auto"

        try:
            data = None
            for _strict_attempt in range(2):
                async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                    async with session.post(
                            GEMINI_OPENAI_URL, headers=req_headers, json=payload
                    ) as resp:
                        if resp.status in (200, 201):
                            data = await resp.json()
                            break
                        err_text = await resp.text()
                        # strict schema 被网关拒绝：摘除后用原始工具列表
                        # 立即重试一次，并记住该 label 不再尝试。
                        if (_strict_attempt == 0
                                and payload.get("tools") is not cleaned_tools
                                and looks_like_strict_tool_rejection(err_text)):
                            mark_strict_tools_rejected("gemini", err_text)
                            payload["tools"] = cleaned_tools
                            continue
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history,
                            status=resp.status, message=err_text,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"[Gemini/aiohttp] round {_round} error: {e}")
            raise

        choices = data.get("choices") or []
        if not choices:
            final_content = "（Gemini 未返回内容）"
            new_history_entries.append({"role": "assistant", "content": final_content})
            break

        raw_msg = choices[0].get("message", {})
        content_acc: str = raw_msg.get("content") or ""
        final_usage = data.get("usage")
        gemini_finish_reason = str(choices[0].get("finish_reason") or "") or None

        tool_calls_list: list[dict] = []
        for tc in (raw_msg.get("tool_calls") or []):
            tc_entry: dict = {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", ""),
                },
            }
            # Gemini OpenAI-compat expects the signature to be returned in the
            # original part structure. The compat format mirrors this via
            # extra_content.google.thought_signature.
            thought_signature = tc.get("thought_signature")
            if thought_signature is None:
                extra_content = tc.get("extra_content") or {}
                thought_signature = (
                    extra_content.get("google", {}).get("thought_signature")
                )
            if thought_signature is not None:
                tc_entry["extra_content"] = {
                    "google": {
                        "thought_signature": thought_signature,
                    }
                }
                # Keep the legacy field too for maximum compatibility.
                tc_entry["thought_signature"] = thought_signature
            tool_calls_list.append(tc_entry)
        _normalize_tool_call_arguments(
            tool_calls_list, "gemini", _round + 1,
            stream_finish_reason=gemini_finish_reason)

        # Gemini/OpenAI 兼容端也可能把函数调用 XML 放在普通 content 中；在写入
        # 草稿和历史前先清理，避免最终消息暴露内部调用语法。
        textual_tool_call = not tool_calls_list and _contains_textual_tool_call(content_acc)
        if textual_tool_call:
            content_acc = _strip_textual_tool_calls(content_acc)

        reasoning_acc: str = raw_msg.get("reasoning_content") or ""
        if reasoning_acc:
            builder.begin_stream_reasoning()
            builder.append_stream_delta(reasoning_acc)
            builder.end_stream()
            builder.finalize_reasoning_block()

        if tool_calls_list and content_acc:
            # ===== 规范第一部分第4点：文本+工具组合需要新开一个独立的工具折叠块，
            # 因此要先把上一个尚未总结的工具块（可能来自连续的纯工具轮次）总结掉。=====
            if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                builder.finish_group(len(builder._tool_groups) - 1)
            builder.start_new_tool_group()
            builder.append_to_current_tool_group_text(content_acc)

        if not tool_calls_list and content_acc:
            builder.add_text(content_acc)

        await builder.flush()

        assistant_msg: dict = dict(raw_msg)
        assistant_msg["role"] = "assistant"
        assistant_msg["content"] = content_acc or None
        if tool_calls_list:
            assistant_msg["tool_calls"] = tool_calls_list
        else:
            assistant_msg.pop("tool_calls", None)
        if reasoning_acc:
            assistant_msg["reasoning_content"] = reasoning_acc
        elif "reasoning_content" in assistant_msg:
            assistant_msg.pop("reasoning_content", None)

        loop_messages.append(assistant_msg)
        new_history_entries.append(assistant_msg)
        # 每轮请求结束后立即打印本轮缓存命中统计（与 OpenAI 兼容 loop 一致）。
        # 旧实现只在循环外打印一次，导致多轮工具调用中间轮次的缓存命中不可观测。
        _log_cache_usage("gemini", final_usage)
        if not tool_calls_list:
            final_content = content_acc
            if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                builder.finish_group(len(builder._tool_groups) - 1)
            # 无工具调用即为终局响应；统一结束旧草稿，不额外开新草稿。
            await builder.rollover_at_turn_boundary(start_next_draft=False)
            break
        status = await _run_tool_calls_and_append(
            tool_calls_list, loop_messages, new_history_entries,
            tool_call_count_ref, "gemini", builder, chat_id=builder.chat_id,
            tools=tools,
        )
        # Gemini 工具批次后仍会继续请求模型，因此在函数内创建新草稿。
        await builder.rollover_at_turn_boundary(start_next_draft=True)

        if status == "over_limit":
            synth_payload = {
                k: v for k, v in payload.items() if k not in ("tools", "tool_choice")
            }
            # 采样/推理参数已随 payload 拷贝带入，无需重复覆盖。
            synth_payload["messages"] = loop_messages + [
                {"role": "user",
                 "content": f"System: Maximum tool calls ({MAX_TOOL_CALLS}) reached for this turn. Tool usage is now DISABLED. Please immediately summarize what you have successfully done so far, explicitly state what failed or what is left to do, and ask the user if they want to continue the operation in the next turn."}
            ]
            synth_payload["max_tokens"] = max_tokens
            try:
                async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                    async with session.post(
                            GEMINI_OPENAI_URL, headers=req_headers, json=synth_payload
                    ) as resp:
                        if resp.status == 200:
                            synth_data = await resp.json()
                            synth_choices = synth_data.get("choices") or []
                            if synth_choices:
                                raw_synth_content = (
                                    synth_choices[0].get("message", {}).get("content") or ""
                                )
                                final_content = _strip_textual_tool_calls(raw_synth_content)
                                if final_content:
                                    builder.add_text(final_content)
            except Exception as e:
                logger.exception(f"[Gemini] synthesis error: {e}")
                final_content = ""
            if not final_content:
                final_content = _tool_limit_summary()
                builder.add_text(final_content)
            new_history_entries.append(
                {"role": "assistant", "content": final_content or ""}
            )
            if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                builder.finish_group(len(builder._tool_groups) - 1)
            # 工具上限总结后没有后续模型轮次：结束旧草稿，禁止创建空新草稿。
            await builder.rollover_at_turn_boundary(start_next_draft=False)
            break
    if final_content is None:
        final_content = _tool_limit_summary()

        builder.add_text(final_content)
        new_history_entries.append({"role": "assistant", "content": final_content})
        if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
            builder.finish_group(len(builder._tool_groups) - 1)
        # 轮次数耗尽后的兜底文本没有后续轮次：结束旧草稿，但不创建新草稿。
        await builder.rollover_at_turn_boundary(start_next_draft=False)
    return final_content, final_usage, new_history_entries


async def _agentic_loop_native_image(
        client: AsyncOpenAI,
        current_model: str,
        messages: list,
        builder: "RichMessageBuilder",
        chat_id: int,
        journal: list = None,
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
    # 图像模型采样参数同样从 config 读取（默认不发送、走供应商默认）；
    # 推理控制不适用于图像生成端点，不发送。
    sampling_params = get_sampling_params(model_info)
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

            # 旧实现构造了带伪 choices/_payload 的 _ImageResponse 对象，
            # 但下游只读取 usage；直接取值即可。
            usage = response_json.get("usage")
            image_bytes_list = await _response_items_to_bytes(response_json)

            if not image_bytes_list:
                try:
                    json_preview = json.dumps(response_json, ensure_ascii=False, indent=2)
                except Exception:
                    logger.debug("_agentic_loop_native_image 内部忽略的异常", exc_info=True)
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
            if journal is not None:
                journal.extend(new_entries)
            return final_content, usage, new_entries

        # ---- 非 ModelScope 的其他提供商（OpenRouter 等） ----
        try:
            response = await client.chat.completions.create(
                model=current_model,
                messages=messages,
                max_tokens=max_tokens,
                extra_body={"modalities": ["image", "text"], "provider": OPENROUTER_PROVIDER_PREFERENCES},
                stream=False,
                **sampling_params,
            )
        except Exception as e:
            err_text = str(e)
            # "output modalities" 含子串 "modalities"，前一条件恒被后者包含。
            if "modalities" not in err_text:
                raise
            logger.warning(f"Native image model does not support image+text output, retrying image-only: {e}")
            response = await client.chat.completions.create(
                model=current_model,
                messages=messages,
                max_tokens=max_tokens,
                extra_body={"modalities": ["image"], "provider": OPENROUTER_PROVIDER_PREFERENCES},
                stream=False,
                **sampling_params,
            )
    except Exception as e:
        logger.exception(f"Native image model request failed: {e}")
        if hasattr(e, "response") and hasattr(e.response, "text"):
            try:
                body = await e.response.text()
                logger.error(f"Response body: {body[:1000]}")
            except Exception:
                logger.debug("_agentic_loop_native_image 内部忽略的异常", exc_info=True)
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
        logger.debug("_agentic_loop_native_image 内部忽略的异常", exc_info=True)
        images = []

    image_bytes_list = []
    for img_data in images:
        img_url = img_data.get("image_url", {}).get("url")
        if not img_url:
            continue
        if img_url.startswith("data:image"):
            try:
                _, base64_data = img_url.split(",", 1)
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
    if journal is not None:
        journal.extend(new_entries)
    return final_content, getattr(response, "usage", None), new_entries


async def _agentic_loop_native_video(
        current_model: str,
        messages: list,
        builder: "RichMessageBuilder",
        chat_id: int,
        journal: list = None,
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
    # 中文"秒"与后续汉字都是 \w，末尾的 \b 对中文分支永不成立（导致
    # "生成5秒的视频" 匹配失败、时长恒为默认值）——中文分支不加边界。
    match = re.search(r'(\d+)\s*(?:秒|seconds?\b|secs?\b|s\b)', prompt, re.IGNORECASE)
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

    # chat action 语义（与 chat_actions.py 的白名单约定一致）：
    # - 生成阶段（调用生视频模型的轮询/生成）→ record_video（bot 正在
    #   “录制”视频），每 4 秒循环重发，覆盖动辄数十秒到数分钟的生成过程；
    # - 发送阶段（视频下载 / R2 上传 / sendRichMessage 携带 <video>）
    #   → upload_video（bot 正在发送视频）。
    #   注意与 generate_video 工具路径的区别：原生视频模型的输出就是
    #   最终要发给用户的视频，不存在「工具结果返回给模型」的中间环节，
    #   因此下载 / R2 上传全程属于发送动作；而工具路径的同类下载 / 上传
    #   属于 AI 接收信息，不触发 upload_video（见 search_engine.
    #   execute_generate_video 的注释）。
    if provider == "agnes":
        async with chat_action_scope(chat_id, "record_video"):
            video_url, error, video_meta = await _request_agnes_video(prompt, duration, current_model)
    elif provider == "openrouter":
        async with chat_action_scope(chat_id, "record_video"):
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
    # upload_video 状态：从下载视频字节、上传 R2（bot 上传视频）直到
    # 发送完成，全程显示“正在发送视频”。下载/上传可能耗时数十秒，
    # 4 秒循环重发保证指示不闪断；最终 sendRichMessage 携带 <video>
    # 时，utils.send_rich_html_message 内部会再次确保 upload_video 激活
    # （同一动作引用计数，不会重复发送状态）。
    await start_chat_action(chat_id, "upload_video")
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
    finally:
        # 下载 / R2 上传阶段结束，熄灭 upload_video（发送动作由
        # send_rich_html_message 内部的 upload_video 钩子接管）。
        await stop_chat_action(chat_id, "upload_video")

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
    if journal is not None:
        journal.extend(new_entries)

    final_content = f"VIDEO_SENT:{prompt[:100]}"  # 用于上游判断
    return final_content, None, new_entries



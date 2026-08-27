"""并行执行模型请求的工具调用，并将结果写回消息历史。

从 ai_handlers.py 拆分而来，逻辑未做改动。
"""
import asyncio
import json
import re
import uuid

from apitelegramchat.utils import get_logger, escape_html
from apitelegramchat.token_budget import truncate_to_token_budget
from apitelegramchat.tool_executors import (
    dispatch_tool_call,
    format_tool_result,
    _truncate_tool_result,
    tool_semaphore,
    _TOOL_TIMEOUT_MARKER,
)
from apitelegramchat.ask_user_tool import (
    create_ask_user_interaction,
    wait_for_answer,
    answer_to_tool_result,
)
from apitelegramchat.ai._constants import (
    MAX_TOOL_CALLS,
    TOOL_CALL_TIMEOUT,
    LONG_RUNNING_TOOLS,
    LONG_TOOL_CALL_TIMEOUT,
    BASH_TOOLS,
    BASH_TOOL_CALL_TIMEOUT,
    SUBAGENT_TOOLS,
    SUBAGENT_OUTER_TIMEOUT,
    MEDIA_GEN_TOOLS,
    TOOL_ERROR_STREAK_LIMIT,
)
from apitelegramchat.ai.error_formatting import extract_domain
from apitelegramchat.ai.tool_summary import (
    _generate_action_description,
    _generate_initial_tool_summary,
    _generate_tool_summary_done,
    _tool_result_is_failure,
    _INVALID_TOOL_ARGUMENTS_KEY,
)

logger = get_logger(__name__)

async def _run_tool_calls_and_append(
        tool_calls: list,
        loop_messages: list,
        new_history_entries: list,
        tool_call_count_ref: list,
        api_label: str,
        builder: "RichMessageBuilder",
        chat_id: int = None,
) -> str:
    valid_tool_calls = []
    skipped_tool_calls = []
    remaining_budget = max(0, MAX_TOOL_CALLS - tool_call_count_ref[0])
    for tc in tool_calls:
        if len(valid_tool_calls) < remaining_budget:
            valid_tool_calls.append(tc)
        else:
            # 模型可能在同一轮请求多个函数；超出余量的调用绝不能继续执行。
            # 稍后仍会为这些 ID 补充 tool 消息，避免下一次总结请求出现未配对调用。
            skipped_tool_calls.append(tc)
    if not valid_tool_calls and not skipped_tool_calls:
        # 收到空 tool_calls 批次：仍需收束当前未完成工具组，避免下一轮复用跨回合的组
        # 影响 UI 草稿滚动边界。
        if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
            builder.finish_group(len(builder._tool_groups) - 1)
        await builder.flush()
        return "continue"

    tool_call_count_ref[0] += len(valid_tool_calls)

    group_idx = builder._get_current_group() if valid_tool_calls else -1

    tool_tasks = []
    for tc in valid_tool_calls:
        if isinstance(tc, dict):
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                fn_args = {}
            tc_id = tc["id"]
        else:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, AttributeError, TypeError):
                fn_args = {}
            tc_id = tc.id

        search_query = None
        domain = None
        if fn_name == "web_search":
            search_query = fn_args.get("query", "")
        elif fn_name == "fetch_url":
            url = fn_args.get('url', '')
            domain = extract_domain(url)

        initial_summary = _generate_initial_tool_summary(fn_name, fn_args)
        action_desc = _generate_action_description(fn_name, fn_args)

        builder.add_tool_item(
            tc_id,
            fn_name,
            initial_summary,
            action_description=action_desc,
            search_query=search_query,
            domain=domain,
            fn_args=fn_args,  # 存储参数用于后续 summary
        )
        tool_tasks.append((fn_name, fn_args, tc_id))

    await builder.flush(force=False)

    has_image_tool = any(fn_name in MEDIA_GEN_TOOLS for fn_name, _, _ in tool_tasks)
    has_bash_tool = any(fn_name in BASH_TOOLS for fn_name, _, _ in tool_tasks)
    has_ask_user_tool = any(fn_name == "ask_user" for fn_name, _, _ in tool_tasks)
    # 草稿构建器的全局刷新循环会在静默超时后，对当前活跃草稿统一执行
    # force flush。工具批次无需另建心跳任务；图片、视频和普通工具均复用
    # 同一机制，状态变更仍由前面的普通 flush 立即推送。

    async def run_one(fn_name, fn_args, tc_id):
        async with tool_semaphore:
            # 图像 / 视频工具不设超时（内部已有轮询超时控制）
            # 子 agent 走 930s 超时（内部默认 900s，用户可配到 1800s）
            # bash 走 310s（内层沙箱 300s + 10s 外层缓冲）
            # 网络类工具（web_search / fetch_url / text_editor）走 45s 宽松超时，避免外层 12s 误杀
            # 其他工具保持 12 秒
            if fn_name in MEDIA_GEN_TOOLS:
                timeout = None
            elif fn_name in SUBAGENT_TOOLS:
                timeout = SUBAGENT_OUTER_TIMEOUT
            elif fn_name in BASH_TOOLS:
                timeout = BASH_TOOL_CALL_TIMEOUT
            elif fn_name in LONG_RUNNING_TOOLS:
                timeout = LONG_TOOL_CALL_TIMEOUT
            else:
                timeout = TOOL_CALL_TIMEOUT

            # 子 agent 专用：每轮 LLM 调用前 / 工具执行前向 builder 推送进度，
            # 实时刷新草稿，避免 90s 黑屏。
            tool_progress_callback = None
            if fn_name in SUBAGENT_TOOLS or fn_name in BASH_TOOLS:
                label = "🤖 子 agent 运行中" if fn_name in SUBAGENT_TOOLS else "🖥️ Bash 运行中"

                async def tool_progress_callback(status_text: str, _tc_id=tc_id, _label=label):
                    try:
                        text = status_text or "正在执行…"
                        preview = f"<i>{escape_html(truncate_to_token_budget(text, 300, suffix='…'))}</i>"
                        builder.update_tool_preview(_tc_id, preview, summary=_label)
                        await builder.flush(force=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass  # 进度推送失败不能影响工具本身

                # 先立即把工具状态从“刚开始”变成“正在执行”，避免长工具在
                # 第一次真实 stdout/进度出现之前前端没有任何可见变化。
                await tool_progress_callback("正在执行…")


            try:
                invalid_arguments = fn_args.get(_INVALID_TOOL_ARGUMENTS_KEY)
                if invalid_arguments:
                    result_str = (
                        f"Error: tool {fn_name} was not executed because the model returned malformed JSON "
                        f"arguments ({invalid_arguments}). Reissue the same tool call with a valid JSON object."
                    )
                elif fn_name == "ask_user":
                    question = fn_args.get("question", "")
                    options = fn_args.get("options", [])
                    multiple = bool(fn_args.get("multiple", False))
                    allow_custom = bool(fn_args.get("allow_custom", True))
                    interaction = await create_ask_user_interaction(
                        builder.chat_id,
                        question,
                        options,
                        multiple=multiple,
                        allow_custom=allow_custom,
                    )
                    builder.update_tool_item(
                        tc_id,
                        "Waiting for your answer",
                        f"<p>{escape_html(truncate_to_token_budget(str(question), 64, suffix='…'))}</p>",
                        status="waiting",
                    )
                    await builder.flush(force=True)
                    answer = await wait_for_answer(interaction)
                    result_str = answer_to_tool_result(answer)
                else:
                    result_str = await asyncio.wait_for(
                        dispatch_tool_call(
                            fn_name, fn_args, chat_id=builder.chat_id,
                            progress_callback=tool_progress_callback,
                        ),
                        timeout=timeout
                    )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.error(f"[tool] {fn_name} timed out after {timeout}s ...")
                # 使用统一标记，让 UI 展示友好状态、模型仍收到可操作的超时说明。
                result_str = _TOOL_TIMEOUT_MARKER
            except Exception as e:
                logger.exception(f"[tool] {fn_name} failed: {e}")
                result_str = f"Exception: tool {fn_name} failed - {truncate_to_token_budget(str(e), 64, suffix='…')}"
            # 修复：_truncate_tool_result / format_tool_result 处理的是工具的原始
            # 输出（可能是任意格式的字符串），二者内部有大量字符串切分/正则/索引
            # 操作，遇到非预期形状的内容时可能抛出未捕获异常（IndexError /
            # KeyError / AttributeError 等）。此前这类异常会直接冒泡出
            # run_one，被外层 asyncio.gather(return_exceptions=True) 捕获成
            # 一个裸 Exception，导致该 tool_call_id 既没有配对的 tool 消息，
            # 也没有更新 builder 状态（UI 上表现为该折叠块永远停在"运行中"）。
            # 这里已经拿到了真实的工具执行结果 result_str，不应该因为格式化
            # 阶段的 bug 丢掉它——格式化失败就退化为纯文本展示，而不是让整个
            # 工具调用从模型上下文和 UI 里"消失"。
            try:
                # bash 结果走「头尾保留」截断：报错几乎总在输出末尾，
                # 纯头部截断会让模型看不到失败原因。
                safe_content = _truncate_tool_result(result_str, fn_name=fn_name)
            except Exception as e:
                logger.exception(f"[tool] {fn_name} _truncate_tool_result 失败: {e}")
                safe_content = "Error: tool output could not be safely constrained to its token budget."
            try:
                # 我们不再使用 format_tool_result 的摘要，而是自己生成
                formatted_summary, details_html = await format_tool_result(fn_name, fn_args, safe_content)
                # 但我们会用自定义生成摘要替换 formatted_summary
                # 所以这里保留 details_html，但摘要我们后面自己生成
            except Exception as e:
                logger.exception(f"[tool] {fn_name} format_tool_result 失败: {e}")
                formatted_summary = f"{fn_name} completed (formatting failed)"
                details_html = f"<p>{escape_html(truncate_to_token_budget(str(safe_content), 256, suffix='…'))}</p>"
            if safe_content == _TOOL_TIMEOUT_MARKER:
                llm_content = f"Error: tool {fn_name} timed out. Please try again or refine the request."
            else:
                llm_content = safe_content
            return (fn_name, tc_id, formatted_summary, details_html, llm_content, fn_args, safe_content)

    results = await asyncio.gather(
        *[run_one(fn, args, tid) for fn, args, tid in tool_tasks],
        return_exceptions=True
    )

    await builder.flush(force=False)

    # ===== 修改：根据结果标记状态 =====
    # tool_tasks 与 results 顺序一一对应（asyncio.gather 保序），用于在
    # run_one 抛出未捕获异常时（如 format_tool_result 内部报错）仍能拿到
    # 原始 tc_id / fn_name，补齐 tool 消息与 builder 状态。
    # 修复：此前这里对未捕获异常直接 log + continue，导致：
    #   1) 该 tool_call_id 永远没有配对的 tool 消息 -> 下一轮请求里
    #      assistant.tool_calls 与 tool 消息数量不一致，多数供应商会
    #      直接 400，或者模型陷入重试/困惑的死循环；
    #   2) builder 里对应的工具条目 status 永远停在 "running"，UI 上
    #      表现为一个折叠块永远转圈、草稿只在无关地方微调 —— 也就是
    #      "刷新但没有新信息、后端却仍在跑" 的现象。
    # 现在无论 run_one 是否抛出未捕获异常，都保证每个 tool_call 都会：
    #   a) 得到一次 builder.update_tool_item(..., status=...) 调用；
    #   b) 追加一条配对的 role=tool 消息回传给模型。
    for idx, res in enumerate(results):
        if isinstance(res, asyncio.CancelledError):
            raise res
        if isinstance(res, Exception):
            # 不在 except 块内，使用 exc_info 显式附加 traceback
            logger.error("工具执行异常: %s", res, exc_info=res)
            try:
                fn_name, fn_args, tc_id = tool_tasks[idx]
            except (IndexError, ValueError):
                fn_name, fn_args, tc_id = "unknown", {}, f"call_error_{uuid.uuid4().hex[:8]}"
            err_text = f"Exception: tool {fn_name} failed - {str(res)[:200]}"
            final_summary = f"⚠️ {fn_name} failed"
            details_html = f"<p>{escape_html(err_text)}</p>"
            builder.update_tool_item(tc_id, final_summary, details_html, status="error")
            tool_msg = {"role": "tool", "tool_call_id": tc_id, "name": fn_name, "content": err_text}
            loop_messages.append(tool_msg)
            new_history_entries.append(tool_msg)
            continue
        # 元组字段顺序: (fn_name, tc_id, formatted_summary, details_html, llm_content, fn_args, safe_content)
        fn_name, tc_id, formatted_summary, details_html, llm_content, fn_args, safe_content = res

        # 失败工具不进入工具组成功统计。
        is_error = _tool_result_is_failure(fn_name, fn_args, safe_content, details_html)
        if is_error:
            # 优先展示格式化器生成的可读标题；模型上下文仍保留完整的可操作错误文本。
            final_summary = formatted_summary or (llm_content[:100] if len(llm_content) > 100 else llm_content)
            status = "error"
        else:
            # 成功：使用 _generate_tool_summary_done 生成描述
            final_summary = _generate_tool_summary_done(fn_name, fn_args, safe_content)
            status = "done"

        builder.update_tool_item(tc_id, final_summary, details_html, status=status)

        # ========== bash 退出码告警（仅用于日志，不影响最终成功判断） ==========
        if fn_name == "bash":
            exit_match = re.search(r"Exit code:\s*(\d+)", str(safe_content or ""))
            if exit_match and exit_match.group(1) != "0":
                logger.warning(
                    f"[bash] 非零退出码，命令可能失败: {safe_content[:300]!r}"
                )

        # 向 LLM 发送实际工具输出（safe_content），以便 LLM 准确推理
        tool_msg = {"role": "tool", "tool_call_id": tc_id, "name": fn_name, "content": safe_content}
        loop_messages.append(tool_msg)
        new_history_entries.append(tool_msg)
    # 对本批因预算而跳过的调用补齐标准 tool 消息，保证后续无工具总结请求的
    # assistant/tool 配对完整，同时向模型明确说明这些调用没有被执行。
    if skipped_tool_calls:
        for tc in skipped_tool_calls:
            if isinstance(tc, dict):
                skipped_id = tc.get("id") or f"call_skipped_{uuid.uuid4().hex[:8]}"
                skipped_name = tc.get("function", {}).get("name") or "unknown"
            else:
                skipped_id = getattr(tc, "id", "") or f"call_skipped_{uuid.uuid4().hex[:8]}"
                skipped_name = getattr(getattr(tc, "function", None), "name", "unknown") or "unknown"
            skipped_content = (
                f"Not executed: the per-turn tool-call budget of {MAX_TOOL_CALLS} was reached. "
                "Do not retry this call in this turn; provide a final status summary instead."
            )
            tool_msg = {
                "role": "tool", "tool_call_id": skipped_id,
                "name": skipped_name, "content": skipped_content,
            }
            loop_messages.append(tool_msg)
            new_history_entries.append(tool_msg)
        logger.warning(
            "[%s] 工具调用预算已耗尽：已执行=%s，跳过=%s，上限=%s",
            api_label, tool_call_count_ref[0], len(skipped_tool_calls), MAX_TOOL_CALLS,
        )
    # 一个模型返回中声明的全部工具已得到最终状态；这才是允许草稿切换的原子边界。
    if group_idx >= 0:
        builder.finish_group(group_idx)
    await builder.flush()

    if tool_call_count_ref[0] >= MAX_TOOL_CALLS:
        logger.warning(f"[{api_label}] 工具调用超限 ({MAX_TOOL_CALLS})")
        return "over_limit"

    error_msgs = []
    for res in results:
        if isinstance(res, tuple) and len(res) >= 5:
            llm_content = res[4]
            if isinstance(llm_content, str) and (
                    llm_content.startswith("Error:") or llm_content.startswith("Exception:")
            ):
                error_msgs.append(llm_content[:80])
    if error_msgs and len(set(error_msgs)) == 1 and len(error_msgs) == len(results):
        key = f"_streak:{error_msgs[0]}"
        prev = getattr(builder, key, 0)
        curr = prev + 1
        setattr(builder, key, curr)
        if curr >= TOOL_ERROR_STREAK_LIMIT:
            logger.warning(
                f"[{api_label}] 检测到工具连续相同错误熔断: {error_msgs[0]!r} x{curr}"
            )
            loop_messages.append({
                "role": "user",
                "content": (
                    f"System: tool '{error_msgs[0]}' has failed {curr} times in a row with the same error. "
                    "STOP retrying the same operation. Switch strategy (use str_replace to edit, "
                    "or view first, or give up and explain to the user). Do NOT call the same "
                    "tool with the same arguments again."
                )
            })
            setattr(builder, key, 0)
            return "continue"
    else:
        for attr in list(vars(builder).keys()):
            if attr.startswith("_streak:"):
                delattr(builder, attr)

    return "continue"



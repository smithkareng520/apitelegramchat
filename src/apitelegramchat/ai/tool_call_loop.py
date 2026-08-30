"""并行执行模型请求的工具调用，并将结果写回消息历史。

从 ai_handlers.py 拆分而来，逻辑未做改动。
"""
import asyncio
import json
import re
import time
import uuid
from typing import TYPE_CHECKING

from apitelegramchat.utils import get_logger, escape_html
from apitelegramchat.token_budget import truncate_to_token_budget
from apitelegramchat.tool_result_condense import condense_for_model
from apitelegramchat.tool_executors import (
    dispatch_tool_call,
    format_tool_result,
    _truncate_tool_result,
    tool_semaphore,
    _TOOL_TIMEOUT_MARKER,
)
from apitelegramchat.message_user_tool import (
    create_message_user_interaction,
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
    CONSUMER_TOOLS,
)
from apitelegramchat.ai.error_formatting import extract_domain
from apitelegramchat.ai.tool_summary import (
    _generate_action_description,
    _generate_initial_tool_summary,
    _generate_tool_summary_done,
    _tool_result_is_failure,
    _INVALID_TOOL_ARGUMENTS_KEY,
)

if TYPE_CHECKING:
    # 仅供类型注解使用；运行时由调用方传入，避免运行时循环导入。
    from apitelegramchat.ai.rich_message_builder import RichMessageBuilder

logger = get_logger(__name__)


# ---------- 子 agent 进度预览渲染 ----------
# 子 agent 在 _subagent_agentic_loop 里通过 _report 推送的 status_text 是
# 一组带固定模式的中文短句（"第 X/Y 轮：LLM 思考中…（已耗时 Xs）"、
# "完成：X 轮，N 次工具调用，Xs" 等）。这里把它们解析成结构化字段，
# 渲染成 Telegram Rich Message 块级 HTML，让用户能直接读到当前阶段、
# 当前轮数、已耗时、正在执行的工具名 —— 而不是一行被 italic 化、被
# 截断到 300 token 的灰色状态句。

# 顺序敏感：先匹配「完成 / 结束 / 超时 / 失败」再匹配「执行工具」、
# 最后兜底「启动」。
_SUBAGENT_PROGRESS_PHASE_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("done",     re.compile(r"^完成[:：]")),
    ("terminal", re.compile(r"^结束[:：]")),
    ("timeout",  re.compile(r"整体超时|LLM 调用超时|轮.*超时")),
    ("error",    re.compile(r"失败|解析失败")),
    ("tools",    re.compile(r"执行工具")),
    ("thinking", re.compile(r"LLM 思考中")),
    ("start",    re.compile(r"^启动子")),
]

_SUBAGENT_ROUND_RE = re.compile(r"第\s*(\d+)\s*[/／]\s*(\d+)\s*轮")
_SUBAGENT_PLAIN_ROUND_RE = re.compile(r"第\s*(\d+)\s*轮")
_SUBAGENT_ELAPSED_RE = re.compile(r"已耗时\s*([0-9.]+)\s*[s秒]")
# 「完成：X 轮，N 次工具调用，Xs」与「结束：…（X 轮，N 次工具调用）」
# 共享同一个轮次+工具调用次数模式；秒数仅在完成行出现，故设为可选。
_SUBAGENT_TOTAL_TIME_RE = re.compile(
    r"(\d+)\s*轮[，,]\s*(\d+)\s*次工具调用"
    r"(?:[，,]\s*([0-9.]+)\s*[s秒])?"
)
_SUBAGENT_TOOL_NAMES_RE = re.compile(r"执行工具\s*(.+?)\s*[（(]?\s*已耗时")
_SUBAGENT_MODEL_RE = re.compile(r"模型\s*([^，,（(]+?)\s*[，,]")


def _subagent_progress_phase(status_text: str) -> str:
    """从 status_text 里抽出当前阶段，用于节流决策（同一阶段内合并刷新）。"""
    if not status_text:
        return "unknown"
    for phase, pattern in _SUBAGENT_PROGRESS_PHASE_PATTERNS:
        if pattern.search(status_text):
            return phase
    return "unknown"


def _format_subagent_progress_html(status_text: str) -> str:
    """把子 agent 的中文状态短句渲染成结构化富文本卡片。

    返回的 HTML 片段由若干 ``<p>`` 块级元素组成，可直接嵌入工具卡片的
    ``<details>``。所有外露文本均经 ``escape_html`` 转义，避免模型或
    子 agent 控制的字符串破坏 Rich Message 结构。
    """
    text = status_text or "正在执行…"
    phase = _subagent_progress_phase(text)
    phase_meta = {
        "start":    ("🤖", "启动子 agent"),
        "thinking": ("🧠", "LLM 思考中"),
        "tools":    ("🔧", "正在调用工具"),
        "done":     ("✅", "子 agent 已完成"),
        "terminal": ("⚠️", "已结束"),
        "timeout":  ("⏱️", "超时"),
        "error":    ("❌", "出错"),
        "unknown":  ("…",  "进行中"),
    }.get(phase, ("…", "进行中"))
    icon, phase_label = phase_meta

    round_match = _SUBAGENT_ROUND_RE.search(text)
    plain_round_match = _SUBAGENT_PLAIN_ROUND_RE.search(text)
    elapsed_match = _SUBAGENT_ELAPSED_RE.search(text)
    total_match = _SUBAGENT_TOTAL_TIME_RE.search(text)
    tool_names_match = _SUBAGENT_TOOL_NAMES_RE.search(text)
    model_match = _SUBAGENT_MODEL_RE.search(text)

    rows = []
    if model_match:
        rows.append(f"<b>模型</b>：{escape_html(model_match.group(1).strip())}")
    if round_match:
        rows.append(
            f"<b>轮次</b>：{escape_html(round_match.group(1))} / "
            f"{escape_html(round_match.group(2))}"
        )
    elif plain_round_match and phase not in ("done", "terminal"):
        rows.append(f"<b>轮次</b>：{escape_html(plain_round_match.group(1))}")
    if tool_names_match:
        # 子 agent 推送的 status_text 在工具名后追加了「…」，原样展示会
        # 把省略号当成工具名一部分。统一去除尾部省略号 / 点号。
        raw_names = tool_names_match.group(1).strip().rstrip("….").strip()
        tool_list = [t.strip() for t in raw_names.split("+") if t.strip()]
        if len(tool_list) > 6:
            tool_display = " + ".join(tool_list[:6]) + f" 等 {len(tool_list)} 个"
        else:
            tool_display = " + ".join(tool_list)
        rows.append(f"<b>调用工具</b>：{escape_html(tool_display)}")
    if total_match:
        rounds_s = escape_html(total_match.group(1))
        tool_calls_s = escape_html(total_match.group(2))
        seconds_s = escape_html(total_match.group(3)) if total_match.group(3) else None
        if phase == "done":
            label = "完成"
        elif phase == "terminal":
            label = "结束"
        else:
            label = "进度"
        if seconds_s:
            rows.append(
                f"<b>{label}</b>：{rounds_s} 轮 · "
                f"{tool_calls_s} 次工具调用 · "
                f"{seconds_s}s"
            )
        else:
            rows.append(
                f"<b>{label}</b>：{rounds_s} 轮 · "
                f"{tool_calls_s} 次工具调用"
            )
    elif elapsed_match:
        rows.append(f"<b>已耗时</b>：{escape_html(elapsed_match.group(1))}s")

    header = f"<p>{icon} <b>{escape_html(phase_label)}</b></p>"
    if rows:
        body = "<p>" + " · ".join(rows) + "</p>"
    else:
        # 兜底：状态文本本身已结构化失败，原样展示但截断到合理长度。
        safe = escape_html(text[:160])
        body = f"<p><i>{safe}</i></p>"
    return header + body


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

    # 草稿构建器的全局刷新循环会在静默超时后，对当前活跃草稿统一执行
    # force flush。工具批次无需另建心跳任务；图片、视频和普通工具均复用
    # 同一机制，状态变更仍由前面的普通 flush 立即推送。
    # （旧实现此处还计算过 has_image_tool / has_bash_tool /
    #  has_message_user_tool 三个从未被读取的变量，属心跳任务删除后的残留。）

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

            # ===== 进度预览策略（v2.3 重构） =====
            # - Bash：完全不推送实时预览。原始 stdout 对用户价值有限
            #   （多为命令日志，最终结果卡片已包含头尾完整输出），
            #   频繁刷新草稿只换来视觉抖动 + Telegram API 限流压力。
            #   卡片保持初始摘要（命令片段），由最终 update_tool_item
            #   一次性写入完整结果卡片。
            # - 子 agent：把每轮 LLM 调用前 / 工具执行前的状态渲染成
            #   结构化进度卡片（轮数 / 已耗时 / 当前阶段 / 工具名），
            #   让用户能真正看到子 agent 在干什么，而不是一行 italic
            #   化的灰色状态句。
            # - 刷新节流：子 agent 进度回调本身加 2.0s 硬节流（phase
            #   切换时立即突破节流，保证关键状态变化可见），同时
            #   不再调 builder.flush(force=True)，改由 update_tool_preview
            #   内部的 request_flush(force=False) 走全局合并循环，避免
            #   每次进度回调都立即触发一次草稿 patch。
            tool_progress_callback = None
            if fn_name in SUBAGENT_TOOLS:
                label = "🤖 子 agent 运行中"
                # 用 list 作闭包可变容器，避免 nonlocal 在嵌套 async
                # 函数与外层 run_one 之间引发意外作用域。
                _phase_ref = [None]
                _emit_ref = [0.0]

                async def tool_progress_callback(status_text: str,
                                                  _tc_id=tc_id,
                                                  _label=label,
                                                  _phase_ref=_phase_ref,
                                                  _emit_ref=_emit_ref):
                    try:
                        now = time.monotonic()
                        phase = _subagent_progress_phase(status_text)
                        # 同一 phase 内 2s 节流；phase 切换立即推送，
                        # 保证「思考 → 执行工具 → 完成」等关键状态变化
                        # 不被合并丢掉。
                        if phase == _phase_ref[0] and now - _emit_ref[0] < 2.0:
                            return
                        _phase_ref[0] = phase
                        _emit_ref[0] = now
                        preview_html = _format_subagent_progress_html(status_text)
                        builder.update_tool_preview(_tc_id, preview_html, summary=_label)
                        # update_tool_preview 内部已调用
                        # request_flush(force=False)，由 builder 全局
                        # _stream_flush_loop 按 STREAM_FLUSH_INTERVAL
                        # 节流合并发送；不再 force=True。
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.debug("tool_progress_callback 内部忽略的异常", exc_info=True)
                        pass  # 进度推送失败不能影响工具本身

                # 立即把卡片从「刚开始」推到「运行中」，避免用户在子
                # agent 首次 LLM 调用完成前看到空进度。
                await tool_progress_callback("启动子 agent…")
            # bash 走默认分支：tool_progress_callback 保持 None，
            # execute / _execute_heredoc_isolated 内部 emit_progress
            # 因 progress_callback is None 直接 return，整段 bash 期间
            # 不再有进度预览，卡片仅展示命令片段摘要。


            try:
                invalid_arguments = fn_args.get(_INVALID_TOOL_ARGUMENTS_KEY)
                if invalid_arguments:
                    result_str = (
                        f"Error: tool {fn_name} was not executed because the model returned malformed JSON "
                        f"arguments ({invalid_arguments}). Reissue the same tool call with a valid JSON object."
                    )
                elif fn_name == "message_user":
                    if getattr(builder, "silent", False):
                        # 静默（TIMER 后台唤醒）回合不允许阻塞等待用户输入：
                        # 按钮消息会绕过 主动消息 的纯文本原则，
                        # 且回合会被用户的下一条消息打断。引导模型改用
                        # 主动消息 以自然语言提问后结束回合。
                        result_str = (
                            "失败：message_user 在系统后台唤醒回合中不可用。"
                            "如需向用户提问，请改用 主动消息 发送一句自然、"
                            "口语化的纯文本提问，然后结束本回合等待用户回复。"
                        )
                    else:
                        question = fn_args.get("question", "")
                        options = fn_args.get("options", [])
                        multiple = bool(fn_args.get("multiple", False))
                        allow_custom = bool(fn_args.get("allow_custom", True))
                        interaction = await create_message_user_interaction(
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
                # 模型视图：按工具剔除对模型无价值的字段（weather 的月相/
                # 露点/低频概率与超出 hours 的逐时条目、subagent 的任务回声
                # 字段等）。UI 草稿的 details_html 已在上方从完整 safe_content
                # 生成，用户可见的展示不受影响；发给模型的 tool 消息与历史
                # 存档均使用精简后的 llm_content。
                #
                # 顺序刻意是「先精简、后截断」：对原始 result_str 精简后再套
                # token 预算，截断预算只花在有价值的字段上——若先截断再精简，
                # 20k 预算会被 24h 低价值逐时数据/路线坐标串吃光，把 POI
                # 名称、导航步骤等真正有用的字段挤出模型视野（且被截断的
                # JSON 无法再解析，精简层会整体失效）。
                try:
                    model_view = condense_for_model(fn_name, fn_args, result_str)
                except Exception:
                    logger.debug("run_one 内部忽略的异常", exc_info=True)
                    model_view = result_str
                if model_view == result_str:
                    # 未发生精简（非目标工具/解析失败/错误文本）：
                    # 直接复用已按预算截断的 safe_content，避免重复计数。
                    llm_content = safe_content
                else:
                    try:
                        llm_content = _truncate_tool_result(model_view, fn_name=fn_name)
                    except Exception:
                        # 精简版截断失败：退回完整视图的截断结果。
                        logger.debug("run_one 内部忽略的异常", exc_info=True)
                        llm_content = safe_content
            return (fn_name, tc_id, formatted_summary, details_html, llm_content, fn_args, safe_content)

    # ====== 串行化"消费者"工具：同批既含 producer（如 bash cp）又含
    # consumer（如 present_files）时，consumer 必须在 producer 落盘后才能
    # 正确读取 upload/。如果让它们一起进 asyncio.gather，consumer 会在
    # producer 完成前看到空目录并报"file not found"——这是 2026-08-27
    # 生产事故的直接原因（模型需要多花 2 轮补救）。
    # 修复策略：把 tool_tasks 拆成 [producers..., consumers...] 两批，
    # 顺序 gather。仅在两批都非空时启用串行化，否则走原来的单批并行路径，
    # 不影响纯查询类多工具并发（如 多个 web_search）的性能。
    producer_indices = [
        i for i, (fn_name, _, _) in enumerate(tool_tasks) if fn_name not in CONSUMER_TOOLS
    ]
    consumer_indices = [
        i for i, (fn_name, _, _) in enumerate(tool_tasks) if fn_name in CONSUMER_TOOLS
    ]
    if producer_indices and consumer_indices:
        # Phase 1: 并行执行所有 producer（如 bash 复制文件到 upload/）
        phase1_results = await asyncio.gather(
            *[run_one(*tool_tasks[i]) for i in producer_indices],
            return_exceptions=True,
        )
        # Phase 2: producer 全部完成后，并行执行所有 consumer（如 present_files）
        phase2_results = await asyncio.gather(
            *[run_one(*tool_tasks[i]) for i in consumer_indices],
            return_exceptions=True,
        )
        # 按 tool_tasks 的原始位置重组 results，后续的状态写入和
        # tool_msg 配对逻辑都基于原始顺序，不需要改动。
        results = [None] * len(tool_tasks)
        for i, r in zip(producer_indices, phase1_results):
            results[i] = r
        for i, r in zip(consumer_indices, phase2_results):
            results[i] = r
    else:
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

        # 向 LLM 发送精简后的模型视图（llm_content）：完整输出先经
        # condense_for_model 剔除无价值字段，再进入本轮请求与持久化历史。
        # UI 侧的 details_html / 失败判定 / bash 退出码检查仍基于完整
        # safe_content（见上方各处），二者互不影响。
        tool_msg = {"role": "tool", "tool_call_id": tc_id, "name": fn_name, "content": llm_content}
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
            # 流式路径已为全部 tool call 建过 UI 条目；被跳过的调用若不
            # 显式收尾，折叠块会永远停留在 "Running..."。openai-compat
            # 流式路径已在 agentic_loops 里 add_tool_item（update 在此处
            # 生效）；未建条目的路径（如 Gemini）update_tool_item 静默跳过。
            builder.update_tool_item(
                skipped_id,
                "Not executed (budget)",
                f"<p>{escape_html(skipped_content)}</p>",
                status="error",
            )
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
            if isinstance(llm_content, str) and llm_content.startswith(("Error:", "Exception:")):
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



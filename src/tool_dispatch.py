"""dispatch_tool_call：全部工具的统一路由分发 + deliver_reply / 结果截断（自 tool_executors.py 拆出）。"""

import os
import json
import asyncio
from typing import Any, Callable, cast
from collections.abc import Awaitable

from config import MAX_CONCURRENT_TOOLS
from workspace_paths import workspace_namespace
from token_budget import truncate_to_token_budget, truncate_to_token_budget_head_tail
from chat_actions import chat_action_scope
from search_engine import (
    execute_web_search,
    execute_fetch_url,
    execute_wikipedia,
    execute_exchange_rate,
    execute_book_lookup,
    execute_weather,
    execute_news,
    execute_crypto_price,
    execute_geocode,
    execute_qr_code,
    execute_generate_image,
    execute_generate_video,
    execute_keyword_search,
    execute_nearby_search,
    execute_poi_details,
    execute_route,
    execute_distance,
    execute_text_editor,
)
from todo_tool import execute_todo
from memory_tool import execute_memory
from subagent_tool import execute_subagent
from bash_session import execute_bash
from file_delivery import execute_present_files, _REMOVED_TOOL_HINTS

import logging

logger = logging.getLogger(__name__)


_TOOL_TIMEOUT_MARKER = "__TOOL_TIMEOUT__"
# ---------- 信号量控制并发工具调用 ----------
tool_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TOOLS)

# 查找位置类工具（amap 地图族）：模型调用期间显示 find_location。
# 全部经 dispatch_tool_call 分发，含子 agent 内的同名调用。
LOCATION_LOOKUP_TOOLS = frozenset({
    "geocode",
    "route",
    "distance",
    "poi_keyword_search",
    "poi_nearby_search",
    "poi_details",
})

TOOL_RESPONSE_TOKEN_BUDGET = int(os.getenv("TOOL_RESPONSE_TOKEN_BUDGET", "20000"))
def _truncate_tool_result(result: str, fn_name: str | None = None) -> str:
    """Bound every model-facing tool result by an exact 20k-token budget.

    bash 结果改用「头尾保留」策略：命令输出的报错几乎总在结尾，纯头部
    截断会让模型看不到失败原因，进而盲目重试浪费请求。
    """
    if fn_name == "bash":
        return truncate_to_token_budget_head_tail(
            result,
            TOOL_RESPONSE_TOKEN_BUDGET,
        )
    return truncate_to_token_budget(
        result,
        TOOL_RESPONSE_TOKEN_BUDGET,
        suffix="\n…[内容过长，已按 token 预算截断]",
    )
async def execute_deliver_reply(chat_id: int, content: Any) -> str:
    """deliver_reply：静默模式（/show off）下交付最终回复给用户。

    语义：发送的是 agent 轮次最后一条助手消息的 content 字段本身——由
    ai/tool_call_loop.run_one 在 send 解析为 true 时从轮次日志里回溯得到后
    传入（通常就是当前这条含 deliver_reply 调用的消息的 content），也不会
    附带 reasoning 等其他字段。send 的缺省值按事件源区分（run_one 内经
    turn_recovery.default_send_value 解析）：静默 USER 回合（用户主动发
    消息）不填按 true 处理，静默 TIMER 回合（后台巡检）不填按 false
    处理（必须显式 true）。本函数只负责发送与交付标记：通过
    sendRichMessage 发送永久富文本消息（不经过草稿）；发送成功后在
    turn_recovery 里标记"本轮已主动交付"，get_ai_response 收尾时据此决定
    静默 USER 回合是否还需要按默认 true 兜底发送（已交付则不再兜底，
    避免双发；兜底路径发送的也是同一段最后一条非空 assistant 正文——
    同样复用 _last_assistant_text 回溯，两条路径交付内容完全同源）；
    TIMER 回合没有兜底直发，不调用（或不显式填 true）本轮
    就不会有任何内容送达用户。

    工具结果刻意不携带 message_id 与正文预览：旧版结果里的
    "已发送给用户（message_id=…）：正文预览"会诱导模型在后续轮次把
    "已确认：deliver_reply 工具已成功调用"之类的回执当成新正文再次交付，
    造成冗余消息链。message_id 只写入服务端日志。
    """
    if not isinstance(content, str) or not content.strip():
        return (
            "失败：deliver_reply 没有可发送的正文。请把完整、自包含的最终回复直接写成"
            "当前消息的正文（Telegram Rich HTML），并在同一条消息中再次调用本工具"
            "（send=true，系统会发送该正文）。"
        )
    from utils import send_rich_html_message
    import turn_recovery
    try:
        result = await send_rich_html_message(chat_id, content, reassert_draft=False)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"[deliver_reply] 发送失败: {e}")
        return "失败：消息发送异常，可稍后重试。"
    if isinstance(result, int) and not isinstance(result, bool) and result > 0:
        turn_recovery.mark_reply_delivered(chat_id)
        logger.info("[deliver_reply] chat=%s 已交付最终回复 message_id=%s chars=%s", chat_id, result, len(content))
        return (
            "已发送：本轮最后一条消息正文已永久发送给用户，交付完成。"
            "不要再调用 deliver_reply，也不要输出\"已发送/已确认\"之类的确认正文——"
            "用户已经收到，重复确认只会造成冗余消息。"
        )
    if result is True:
        # HTTP 200 但未解析到 message_id：按成功处理。
        turn_recovery.mark_reply_delivered(chat_id)
        return (
            "已发送：本轮最后一条消息正文已永久发送给用户，交付完成。"
            "不要再调用 deliver_reply，也不要输出\"已发送/已确认\"之类的确认正文——"
            "用户已经收到，重复确认只会造成冗余消息。"
        )
    return "失败：消息发送失败（网络或 Telegram 错误），可稍后重试。"


async def dispatch_tool_call(name: str, arguments: dict, chat_id: int, progress_callback: Callable[[str], Awaitable[None]] | None = None) -> str:
    if chat_id is None:
        # 早期失败：避免创建 ./workspace/None 造成跨会话数据泄漏
        return json.dumps({"error": "chat_id is required for tool dispatch"})
    # Resolve workspace identity exactly once for this tool invocation.
    # Every workspace-facing operation below receives this explicit namespace, so
    # async tasks/subtasks cannot accidentally resolve a different ContextVar.
    resolved_namespace = workspace_namespace(chat_id)
    try:
        if name == "web_search":
            return await execute_web_search(
                arguments.get("query"),
                arguments.get("num_results"),
                arguments.get("offset"),
                mode=arguments.get("mode", "search"),
                image_url=arguments.get("image_url"),
                gl=arguments.get("gl"),
                hl=arguments.get("hl"),
                tbs=arguments.get("tbs"),
            )
        elif name == "fetch_url":
            # execute_fetch_url 内部已自带逐次重试循环，并自行把
            # TimeoutError 转成失败文案返回——外层的重试包装永远捕获不到
            # 异常，属于死逻辑（最多让同一 URL 被抓 2x2 次），直接透传。
            return await execute_fetch_url(arguments.get("url", ""))
        elif name == "wikipedia":
            return await execute_wikipedia(arguments.get("query", ""), arguments.get("lang", "zh"))
        elif name == "exchange_rate":
            return await execute_exchange_rate(arguments.get("base", "USD"), arguments.get("target"))
        elif name == "book_lookup":
            return await execute_book_lookup(arguments.get("query", ""))
        elif name == "weather":
            return await execute_weather(arguments.get("city", ""), arguments.get("unit", "c"),
                                         arguments.get("hours", 6))
        elif name == "news":
            return await execute_news(arguments.get("source", "bbc"), arguments.get("limit", 5))
        elif name == "crypto_price":
            return await execute_crypto_price(arguments.get("coin", ""), arguments.get("currency", "usd"))
        elif name == "qr_code":
            return await execute_qr_code(arguments.get("text", ""))
        elif name == "generate_image_from_text":
            return await execute_generate_image(
                prompt=cast(str, arguments.get("prompt")),
                model=cast(str, arguments.get("model")),
                aspect_ratio=arguments.get("aspect_ratio", "1:1"),
                image_size=arguments.get("image_size", "1K"),
                num_images=arguments.get("num_images", 1),
                image_url=None  # 强制无参考图
            )
        elif name == "edit_image_with_reference":
            return await execute_generate_image(
                prompt=cast(str, arguments.get("prompt")),
                model=cast(str, arguments.get("model")),
                aspect_ratio=arguments.get("aspect_ratio", "1:1"),
                image_size=arguments.get("image_size", "1K"),
                num_images=arguments.get("num_images", 1),
                image_url=arguments.get("image_url")  # 带参考图
            )
        elif name == "generate_video":
            return await execute_generate_video(
                prompt=cast(str, arguments.get("prompt")),
                model=cast(str, arguments.get("model")),
                duration=arguments.get("duration", 5),
                chat_id=chat_id,
            )
        # 地图工具：模型调用查找位置类方法期间显示 find_location。
        # 同批次并发的多个地图工具共享同一条指示（引用计数），全部结束
        # 才熄灭；单次查询通常数秒内完成，长查询由 4 秒循环保活。
        elif name in LOCATION_LOOKUP_TOOLS:
            async with chat_action_scope(chat_id, "find_location"):
                if name == "geocode":
                    return await execute_geocode(arguments.get("address", ""))
                elif name == "route":
                    return await execute_route(
                        arguments.get("origin", ""),
                        arguments.get("destination", ""),
                        arguments.get("mode", "driving"),
                        arguments.get("city"),
                        arguments.get("cityd"),
                    )
                elif name == "distance":
                    return await execute_distance(
                        arguments.get("origin", ""),
                        arguments.get("destination", ""),
                    )
                elif name == "poi_keyword_search":
                    return await execute_keyword_search(
                        arguments.get("keywords", ""),
                        arguments.get("city"),
                    )
                elif name == "poi_nearby_search":
                    return await execute_nearby_search(
                        arguments.get("keywords", ""),
                        arguments.get("location", ""),
                        arguments.get("radius"),
                    )
                elif name == "poi_details":
                    return await execute_poi_details(arguments.get("id", ""))
                else:
                    return f"失败：未知工具: {name}。"
        elif name == "text_editor":
            return await execute_text_editor(
                chat_id=chat_id,
                namespace=resolved_namespace,
                command=arguments.get("command", ""),
                path=arguments.get("path", ""),
                view_range=arguments.get("view_range"),
                old_str=arguments.get("old_str"),
                new_str=arguments.get("new_str"),
                insert_line=arguments.get("insert_line"),
                insert_text=arguments.get("insert_text"),
                file_text=arguments.get("file_text"),
            )
        # ========== Bash 工具分支 ==========
        # v2.3：bash 执行期间不再推送任何进度预览（progress_callback
        # 不再传递给 execute_bash；卡片摘要保持命令片段，最终结果由
        # update_tool_item 一次性写入）。
        elif name == "bash":
            return await execute_bash(
                chat_id=chat_id,
                namespace=resolved_namespace,
                command=arguments.get("command", ""),
                restart=arguments.get("restart", False),
            )
        # ========== Todo 工具分支 ==========
        # 任务 / 待办清单。返回 JSON 字符串给 AI 上下文；UI 渲染由 format_tool_result 处理。
        elif name == "todo":
            result_str = await execute_todo(
                chat_id=chat_id,
                action=arguments.get("action", "list"),
                title=arguments.get("title"),
                todo_id=arguments.get("todo_id"),
                priority=arguments.get("priority"),
                tags=arguments.get("tags"),
                note=arguments.get("note"),
                due_at=arguments.get("due_at"),
                filter=arguments.get("filter"),
                tag=arguments.get("tag"),
            )
            return result_str
        # ========== Memory 工具分支 ==========
        # 长期记忆库。返回 JSON 给 AI；UI 由 format_tool_result 渲染富文本卡片。
        elif name == "memory":
            return await execute_memory(
                chat_id=chat_id,
                action=arguments.get("action", "list"),
                content=arguments.get("content"),
                memory_id=arguments.get("memory_id"),
                category=arguments.get("category"),
                tags=arguments.get("tags"),
                importance=arguments.get("importance"),
                query=arguments.get("query"),
                scope=arguments.get("scope"),
                limit=arguments.get("limit", 50),
                source=arguments.get("source", "agent"),
            )
        # ========== Subagent 工具分支 ==========
        # 子 agent。返回 JSON（含 answer / rounds / tool_calls）给父 agent 阅读；
        # UI 由 format_tool_result 渲染成「子 agent 已完成」卡片。
        # progress_callback 让子 agent 每轮能向主 agent 的草稿推送进度，避免 90s 黑屏。
        elif name == "subagent":
            return await execute_subagent(
                chat_id=chat_id,
                task=arguments.get("task", ""),
                context=arguments.get("context"),
                model=arguments.get("model"),
                allowed_tools=arguments.get("allowed_tools"),
                timeout=arguments.get("timeout"),
                progress_callback=progress_callback,
            )
        elif name == "present_files":
            paths = arguments.get("paths", [])
            if isinstance(paths, str):
                paths = [paths]
            return await execute_present_files(chat_id, paths, namespace=resolved_namespace)
        elif name == "deliver_reply":
            # 防御路径：正常情况下 deliver_reply 由 tool_call_loop.run_one 的
            # 专用分支处理（send 解析为 true 时自动携带「本轮最后一条助手
            # 消息正文」；send 缺省值按事件源区分——静默 USER 回合 true、
            # 静默 TIMER 回合 false）。仅当其他路径（如子 agent 误用）直达
            # dispatch 时才走到这里——此时没有轮次日志可回溯，统一按未发送
            # 处理，避免误发。
            return (
                "未发送：deliver_reply 只能在主对话的静默回合中生效"
                "（send=true 时由系统发送本轮最后一条助手消息正文），"
                "当前路径无法执行交付。"
            )
        elif name in _REMOVED_TOOL_HINTS:
            # stage_upload / fetch_download / list_download / list_upload / ip_geo 已移除；
            # 迁移提示让模型立即改用 bash 直访，避免无意义的重试。
            return f"失败：{_REMOVED_TOOL_HINTS[name]}"
        else:
            return f"失败：未知工具: {name}。"
    except asyncio.CancelledError:
        # 关键：CancelledError 必须向上传播，否则用户发新消息无法取消正在执行的工具调用，
        # agentic 循环会把取消信号当成普通工具失败吞掉，导致旧任务继续跑。
        raise
    except Exception as e:
        # 顶层异常：只记录日志，返回用户友好消息，不暴露内部细节
        logger.exception(f"dispatch_tool_call 顶层异常 [{name}]: {e}")
        return "⚠️ 工具执行出错，请稍后重试或换一种方式。"

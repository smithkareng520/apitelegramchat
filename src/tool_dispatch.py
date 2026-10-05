"""dispatch_tool_call —— 工具统一路由分发。

架构（MCP 化重构后）
--------------------
工具分两类：

1. MCP 工具（名字以 ``mcp__`` 开头，即 mcp__<server>__<tool>）：
   全部经 mcp_manager.call_tool 走 MCP 协议执行 —— 外部服务器
   （gaode_mcp，streamable_http）与内部服务器（internal_search / todo /
   memory / workspace / bash，stdio 子进程）同一条路径，没有任何本地
   包装层。模型视图裁剪（condense_for_model）与用户视图渲染
   （format_tool_result）由 tool_call_loop 分层处理。

2. host 内建工具：message_user / deliver_reply / subagent /
   generate_image / generate_video / present_files —— 依赖宿主进程的
   Telegram 会话、草稿流与 LLM 编排，在 BUILTIN_HANDLERS 表内直接分发
   （代替旧版巨型 if/elif 链）。

deliver_reply 的专用分支在 ai/tool_call_loop.run_one（需要轮次日志回溯）；
本模块只保留其防御路径。
"""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import tool_names as tn
from config import MAX_CONCURRENT_TOOLS
from token_budget import truncate_to_token_budget, truncate_to_token_budget_head_tail
from workspace_paths import workspace_namespace

logger = logging.getLogger(__name__)

# ---------- 信号量控制并发工具调用 ----------
tool_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TOOLS)

_TOOL_TIMEOUT_MARKER = "__TOOL_TIMEOUT__"

TOOL_RESPONSE_TOKEN_BUDGET = int(os.getenv("TOOL_RESPONSE_TOKEN_BUDGET", "20000"))


def _truncate_tool_result(result: str, fn_name: str | None = None) -> str:
    """Bound every model-facing tool result by an exact 20k-token budget.

    bash 结果改用「头尾保留」策略：命令输出的报错几乎总在结尾，纯头部
    截断会让模型看不到失败原因，进而盲目重试浪费请求。
    """
    if fn_name in {tn.BASH, "bash"}:
        return truncate_to_token_budget_head_tail(
            result,
            TOOL_RESPONSE_TOKEN_BUDGET,
        )
    return truncate_to_token_budget(
        result,
        TOOL_RESPONSE_TOKEN_BUDGET,
        suffix="\n…[内容过长，已按 token 预算截断]",
    )

# 统一图像工具（generate_image）的旧名兼容别名（2026-09-08 工具合并前
# 的两个入口）。它们不再进入工具清单，但 dispatch 仍接受：历史会话
# 上下文里的旧 tool_call 重放、以及模型偶发的旧名幻觉调用，都能被正确
# 路由到统一实现，而不是报"未知工具"。
_IMAGE_TOOL_LEGACY_ALIASES = frozenset({
    "generate_image_from_text",
    "edit_image_with_reference",
})


def _error_json(message: str, *, code: str = "tool_error") -> str:
    return json.dumps({"status": "error", "code": code, "message": message}, ensure_ascii=False)


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


# =====================================================================
# host 内建工具 handler 表
# =====================================================================
BuiltinHandler = Callable[
    [int, dict, str, Callable[[str], Awaitable[None]] | None], Awaitable[str]
]
# 签名: (chat_id, arguments, resolved_namespace, progress_callback) -> result_str


async def _handle_generate_image(chat_id: int, arguments: dict, _ns: str, _cb: Any) -> str:
    from search.media_tools import execute_generate_image

    image_url = arguments.get("image_url")
    if chat_id and arguments.get("_legacy_name") == "generate_image_from_text":
        # 旧名 generate_image_from_text 的历史语义是"强制无参考图"，
        # 别名分发时保持该语义（忽略误带的 image_url）。
        image_url = None
    return await execute_generate_image(
        prompt=arguments.get("prompt") or "",
        model=arguments.get("model") or "",
        aspect_ratio=arguments.get("aspect_ratio", "1:1"),
        image_size=arguments.get("image_size", "1K"),
        num_images=arguments.get("num_images", 1),
        image_url=image_url,
        # 厂商专属附加参数透传（如 Agnes extra_body.response_format 覆盖）；
        # 未传时为 None，execute_generate_image 内部按空 dict 处理。
        extra_params=arguments.get("extra_params"),
    )


async def _handle_generate_video(chat_id: int, arguments: dict, _ns: str, _cb: Any) -> str:
    from search.media_tools import execute_generate_video

    return await execute_generate_video(
        prompt=arguments.get("prompt") or "",
        model=arguments.get("model") or "",
        duration=arguments.get("duration", 5),
        chat_id=chat_id,
    )


async def _handle_subagent(chat_id: int, arguments: dict, ns: str, progress_callback: Any) -> str:
    """路由优先级与 bash 一致：task_action → run_in_background → 前台执行。"""
    if arguments.get("task_action"):
        import subagent_background

        return await subagent_background.query_task(
            chat_id, ns, action=arguments["task_action"], task_id=arguments.get("task_id"),
        )
    if arguments.get("run_in_background"):
        import subagent_background

        # 后台模式不挂进度回调：聊天里的工具卡片在启动调用返回时即定稿，
        # 之后的进度只存进任务，供 task_action=status/output 查询。
        return await subagent_background.start_background_task(
            chat_id,
            ns,
            task=arguments.get("task", ""),
            context=arguments.get("context"),
            model=arguments.get("model"),
            allowed_tools=arguments.get("allowed_tools"),
            timeout=arguments.get("timeout"),
            description=str(arguments.get("description") or ""),
        )
    from subagent_tool import execute_subagent

    return await execute_subagent(
        chat_id=chat_id,
        task=arguments.get("task", ""),
        context=arguments.get("context"),
        model=arguments.get("model"),
        allowed_tools=arguments.get("allowed_tools"),
        timeout=arguments.get("timeout"),
        progress_callback=progress_callback,
    )


async def _handle_present_files(chat_id: int, arguments: dict, resolved_namespace: str, _cb: Any) -> str:
    from file_delivery import execute_present_files

    paths = arguments.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]
    return await execute_present_files(chat_id, paths, namespace=resolved_namespace)


BUILTIN_HANDLERS: dict[str, BuiltinHandler] = {
    tn.GENERATE_IMAGE: _handle_generate_image,
    tn.GENERATE_VIDEO: _handle_generate_video,
    tn.SUBAGENT: _handle_subagent,
    tn.PRESENT_FILES: _handle_present_files,
}


# =====================================================================
# 主分发入口
# =====================================================================
async def dispatch_tool_call(
    name: str,
    arguments: dict,
    chat_id: int,
    progress_callback: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    if chat_id is None:
        # 早期失败：避免创建 ./workspace/None 造成跨会话数据泄漏
        return _error_json("chat_id is required for tool dispatch")

    # Resolve workspace identity exactly once for this tool invocation.
    # Every host-built-in workspace-facing operation receives this explicit
    # namespace, so async tasks/subtasks cannot accidentally resolve a
    # different ContextVar.（MCP 工具的 scope 由 mcp_manager 在拉起子进程
    # 时注入，同样源于该命名空间。）
    resolved_namespace = workspace_namespace(chat_id)

    try:
        legacy_name = None
        if name in _IMAGE_TOOL_LEGACY_ALIASES:
            legacy_name = name
            name = tn.GENERATE_IMAGE

        handler = BUILTIN_HANDLERS.get(name)
        if handler is not None:
            if legacy_name:
                arguments = {**arguments, "_legacy_name": legacy_name}
            # 地图/位置类工具执行期间显示 find_location（MCP 工具在
            # _dispatch_mcp 内统一包裹）。
            return await handler(chat_id, arguments, resolved_namespace, progress_callback)

        if tn.is_mcp_name(name):
            return await _dispatch_mcp(name, arguments, chat_id)

        if name == tn.MESSAGE_USER:
            # message_user 的正式分支在 tool_call_loop.run_one（需要 builder
            # 与回复等待）；这里仅防御误路由。
            return "未执行：message_user 必须由主对话循环处理，当前路径无法执行。"
        if name == tn.DELIVER_REPLY:
            # 防御路径：正常情况下 deliver_reply 由 tool_call_loop.run_one 的
            # 专用分支处理（send 解析为 true 时自动携带「本轮最后一条助手
            # 消息正文」）。仅当其他路径（如子 agent 误用）直达 dispatch 时
            # 才走到这里——此时没有轮次日志可回溯，统一按未发送处理。
            return (
                "未发送：deliver_reply 只能在主对话的静默回合中生效"
                "（send=true 时由系统发送本轮最后一条助手消息正文），"
                "当前路径无法执行交付。"
            )
        return _error_json(f"未知工具: {name}。", code="unknown_tool")
    except asyncio.CancelledError:
        # 关键：CancelledError 必须向上传播，否则用户发新消息无法取消正在
        # 执行的工具调用，agentic 循环会把取消信号当成普通工具失败吞掉，
        # 导致旧任务继续跑。
        raise
    except Exception as e:
        # 顶层异常：只记录日志，返回用户友好消息，不暴露内部细节
        logger.exception(f"dispatch_tool_call 顶层异常 [{name}]: {e}")
        return "⚠️ 工具执行出错，请稍后重试或换一种方式。"


async def _dispatch_mcp(name: str, arguments: dict, chat_id: int) -> str:
    """MCP 工具统一执行路径（外部 streamable_http 与内部 stdio 同构）。"""
    from mcp_manager import mcp_manager, MCPToolError

    try:
        if name in tn.LOCATION_LOOKUP_TOOLS:
            # 位置查询期间给用户 find_location 聊天动作反馈；同批次并发
            # 的多个地图调用共享同一条指示（引用计数）。
            from chat_actions import chat_action_scope

            async with chat_action_scope(chat_id, "find_location"):
                return await mcp_manager.call_tool(name, arguments, chat_id)
        return await mcp_manager.call_tool(name, arguments, chat_id)
    except MCPToolError as exc:
        # 分类后的错误说明直接给模型（已脱敏），并附一句话行动指引：
        # 该重试的重试、不该重试的第一时间向用户说明原因 —— 模型无需
        # 用失败轮次自行摸索重试策略。
        return _error_json(
            f"{str(exc) or exc.user_message()} {exc.model_hint()}".strip(),
            code=f"mcp_{exc.category}",
        )

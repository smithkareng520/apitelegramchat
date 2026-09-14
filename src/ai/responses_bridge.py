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

3. 服务端会话状态（真正的 stateful Responses，2026-09 新增）：
   ==================================================
   上面两条原则描述的是"边界转换"这一层，与是否使用服务端会话无关，
   继续对全部调用方成立。本次新增的是**请求侧的传输优化**：当调用方
   通过 ``turn``（conversation_state.TurnState）接入了对话状态层时，
   本文件会优先复用 OpenAI 的 ``conversation`` 服务端会话对象——只把
   "尚未发给服务端的增量消息"放进 ``input``，不再每轮全量重发
   canonical history；cursor 失效（跨协议切换回来 / 历史被压缩 /
   `/clear` 之后）时自动退化为一次性全量"自举"，建立新的会话对象。

   这一优化完全不影响原则 1/2：
     - canonical history（loop_messages / new_history_entries）依然是
       全量的内部 Message 列表，只读，从不因为"发没发给服务端"而被
       裁剪或改写；
     - 只有"即将序列化成 Responses input"这一步会按 sent_count 切片，
       且切片只影响这一次网络请求的 payload，不影响任何持久化路径。

   详细设计与 fencing（TIMER/USER 并发、/clear、模型切换）见
   conversation_state.py 模块头注释；本文件内的接入点集中在
   "Conversation State：服务端会话（真正的 stateful Responses）"一节。
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
    append_truncation_notice_if_needed,
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
import conversation_state as _conv_state

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


_RESPONSES_EXPLICIT_CACHE_MARKS = 2
_RESPONSES_TTL = "30m"


def _is_responses_text_content(part: Any) -> bool:
    """显式缓存断点只挂在 Responses 支持 breakpoint 的文本 content block 上。"""
    return isinstance(part, dict) and part.get("type") == "input_text"


def _apply_responses_cache_breakpoints(input_items: list[dict]) -> int:
    """只手动打 2 个显式断点（system 段首尾），尾部交给 Responses 的
    implicit 自动缓存（_add_responses_cache_options 里设置的
    prompt_cache_options.mode="implicit"），不在这里遍历消息找尾部
    位置手动打 prompt_cache_breakpoint。

    复刻项目 Anthropic 显式缓存策略的结构（与 anthropic_bridge /
    attachment_content._apply_cache_control 三处保持同一套编号）：
      1) 断点 1：开头连续 system/developer 消息段的第一个可用文本
         block——对应 ai_handlers.build_system_prompt 的 base_segment，
         字节最稳定，不随模型能力/角色/技能目录变化；
      2) 断点 2：开头连续 system/developer 消息段的最后一个可用文本
         block——对应 extra_segment（技能目录/工具说明/角色 prompt/
         时间戳，以及 TIMER、静默模式追加的说明性消息）。这段更易
         失效，单独打点后失效不连累断点 1；段内只有一条消息时与
         断点 1 落在同一 block，安全退化。
      3) 断点 3：不打。
      4) 断点 4：不手动打。Responses 的 implicit 缓存模式本来就是
         "没有显式断点时按最长匹配前缀自动命中"的默认行为，不是在
         显式断点之外额外覆盖尾部的机制——手动去找一个尾部 block 打
         explicit 标记既不必要，也可能和 implicit 模式的命中逻辑
         产生冲突（同一份请求里 explicit 标记越多，implicit 能自由
         匹配的空间越受限）。

    注意：Responses API 当前的 prompt_cache_options.ttl 对整次请求统一
    为 30m，不能逐断点设置不同 TTL，因此这里的"断点 1/2"只是位置上
    复刻原策略的缓存层次，不能像 Anthropic 原生请求那样对断点 1/2
    单独设置 1h。
    返回实际添加的显式断点数。
    """
    # 开头连续的 system/developer 消息段：Responses 用 role in
    # ("system", "developer") 表达系统级指令，build_system_prompt 的
    # 两段以及 TIMER/静默模式追加的说明性消息都在这个开头连续段内。
    system_run_end = 0
    while system_run_end < len(input_items):
        item = input_items[system_run_end]
        if not isinstance(item, dict) or item.get("role") not in ("system", "developer"):
            break
        system_run_end += 1

    def _first_text_candidate(start: int, stop: int, *, reverse: bool) -> tuple[int, int] | None:
        rng = range(stop - 1, start - 1, -1) if reverse else range(start, stop)
        for item_index in rng:
            item = input_items[item_index]
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            part_rng = range(len(content) - 1, -1, -1) if reverse else range(len(content))
            for part_index in part_rng:
                if _is_responses_text_content(content[part_index]):
                    return (item_index, part_index)
        return None

    selected: list[tuple[int, int]] = []

    # 断点 1：system 段第一个可用文本 block。
    first_system = _first_text_candidate(0, system_run_end, reverse=False)
    if first_system:
        selected.append(first_system)

    # 断点 2：system 段最后一个可用文本 block（与断点 1 相同时去重，
    # 退化为一个断点）。
    last_system = _first_text_candidate(0, system_run_end, reverse=True)
    if last_system and last_system not in selected:
        selected.append(last_system)

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
    """注入稳定 key 和自动缓存；按开关可附加最多 2 个显式断点（system
    段首尾，见 _apply_responses_cache_breakpoints）。

    默认模式只发送 ``mode=implicit``，兼容只支持自动缓存的中转；
    implicit 模式本身也是尾部内容的缓存机制，不需要额外的显式尾部
    断点。显式模式在此基础上叠加最多 2 个 explicit breakpoint（只打在
    system 段），implicit 继续覆盖尾部——不是"implicit + explicit
    分别覆盖不同范围"，而是 explicit 断点精确锁定 system 段的两个
    稳定/半稳定边界，implicit 兜底其余部分的最长前缀匹配。
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
# Conversation State：服务端会话（真正的 stateful Responses）
# =============================================================================
# 背景（务必先读 conversation_state.py 模块头注释）：本节把
# _agentic_loop_openai_responses 从"每轮全量重发 canonical history"改造
# 成"cursor 有效时只发本轮增量"。策略：
#
#   1) 回合开始（第一次进入下面的 for _round 循环）时，查询
#      conversation_state 里该 chat 的 Responses cursor 是否仍然对当前
#      canonical_revision 有效（is_valid_for）。
#        - 有效 -> 增量模式：只转换 loop_messages 里"尚未发送过"的
#          消息（通常就是本轮新增的 user 消息），请求携带
#          conversation=cursor.conversation_id，不携带 input 之外的
#          历史；
#        - 无效（从未建立过 / 模型此前不是 Responses / 历史被压缩 /
#          `/clear` 之后第一次）-> 自举模式：全量转换 loop_messages
#          （与旧行为一致），请求携带 conversation=<新建的 conversation
#          对象 id>，建立服务端会话起点。
#   2) 工具调用继续同一轮对话（同一个 for _round 迭代继续）时，
#      不需要重新判断 cursor 有效性——同一个 turn 内本来就是同一个
#      conversation，只需要把"本轮循环内新产生的" assistant/tool
#      消息（上一次请求之后 loop_messages 新增的部分）作为下一次请求
#      的增量 input 发送。用 _sent_count 追踪"loop_messages 里已经
#      发给 Responses 服务端的前缀长度"即可，逻辑与 revision 机制正交
#      （revision 只用于跨轮次/跨协议判断 cursor 是否需要重新自举）。
#   3) response.completed 时（循环末尾拿到 resp_obj.conversation.id /
#      resp_obj.id）尝试 commit 新 cursor——只有 TurnState fencing 校验
#      通过（回合发起后没有发生 /clear）才真正写入，避免过期回合的
#      迟到响应污染当前状态（见 conversation_state.ConversationState.
#      is_turn_current）。
#
# 环境开关（默认开启）：出于稳妥考虑保留一个总开关，允许在观察到
# 网关侧对 `conversation` 参数支持不稳定时整体回退到旧的"每轮全量
# 自举"行为（等价于把 cursor 永远判定为无效），不影响功能正确性，
# 只是放弃 token/延迟优化。
import os as _os


def _responses_stateful_enabled() -> bool:
    raw = _os.getenv("RESPONSES_STATEFUL_CONVERSATION_ENABLED", "true")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


async def _resolve_conversation_plan(
    chat_id: Any,
    turn: Optional["_conv_state.TurnState"],
) -> tuple[Optional[str], bool]:
    """返回 (conversation_id_to_reuse, need_bootstrap)。

    - chat_id 为 None（无法定位会话，如 subagent 一次性调用）或本轮未
      提供 TurnState（调用方未接入 conversation_state，如旧版直接调用
      _agentic_loop_openai_responses 的测试/脚本）-> 一律不使用服务端
      会话，退回旧的"全量自举，不复用"行为，保证向后兼容。
    - conversation_id_to_reuse 非空 -> 复用该会话，只发增量；
    - need_bootstrap=True 且 conversation_id_to_reuse 为空 -> 需要新建
      一个 conversation 对象后再首次全量自举。
    """
    if chat_id is None or turn is None or not _responses_stateful_enabled():
        return None, False
    st = await _conv_state.get_conversation_state(chat_id)
    cursor = st.responses
    if cursor.is_valid_for(st.canonical_revision):
        return cursor.conversation_id, False
    return None, True


async def _create_responses_conversation(client: "AsyncOpenAI") -> Optional[str]:
    """新建一个空的 Responses conversation 对象，返回其 id。

    失败（网关不支持 conversations 端点 / 网络错误）时返回 None，
    调用方据此退回"本轮不使用服务端会话，仍走全量 input"的安全路径
    ——不影响功能正确性，只是这一轮放弃增量优化。
    """
    try:
        conv = await client.conversations.create()
        return getattr(conv, "id", None)
    except Exception:
        logger.info(
            "[openai_responses] 创建 Responses conversation 失败，本轮回退为无状态全量请求",
            exc_info=True,
        )
        return None


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
        turn: Optional["_conv_state.TurnState"] = None,
) -> tuple[str | None, object | None, list]:
    """OpenAI 原生 Responses API（/v1/responses）专用循环。

    对外契约与 _agentic_loop_openai_compat / _agentic_loop_anthropic /
    _agentic_loop_gemini_native 完全一致：入参/出参（messages、返回的
    new_history_entries）统一为内部 Message（core/messages），只在请求
    Responses API 前做内部 -> 原生协议的边界转换（见模块头注释）。

    ``turn``：本回合的 conversation_state.TurnState 快照（由
    get_ai_response 在回合开始时创建、经协议适配器透传到这里）。为
    None 时（旧调用点 / 独立脚本 / subagent 一次性调用）本函数退回
    "每轮全量重发"的旧行为，完全向后兼容——真正的服务端会话状态只在
    调用方接入了 conversation_state 时才生效（见
    protocols/openai_responses.py 与 ai_handlers.get_ai_response 的
    改动点）。
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

    # ---- Conversation State：本回合是否使用服务端会话 ----------------
    # chat_id 取自 builder（DraftManager 透传自 get_ai_response，全部
    # 调用路径统一可用）；resolve 只在回合的第一轮之前做一次，工具调用
    # 触发的后续轮次复用同一个 conversation_id，不重新判断。
    chat_id = getattr(builder, "chat_id", None)
    conversation_id, need_bootstrap = await _resolve_conversation_plan(chat_id, turn)
    if need_bootstrap:
        conversation_id = await _create_responses_conversation(client)
        if conversation_id is None:
            # 建会话失败：整轮退回旧的"无状态全量"行为（既不复用也不
            # 新建），下一轮自然会再次尝试自举，不影响正确性。
            need_bootstrap = False
    using_stateful_conversation = bool(conversation_id)
    # sent_count：loop_messages 中已经作为 input 发送过的前缀长度。
    # 增量模式下，每次请求只转换 loop_messages[sent_count:]；工具调用
    # 轮次结束后 loop_messages 会追加新的 assistant/tool 消息，下一次
    # 请求据此自然只发"新增部分"。首轮：
    #   - 使用服务端会话（无论是复用已存在的还是刚新建的）时，"首轮"
    #     的语义不同——复用场景只发本轮新消息（sent_count 从"已同步
    #     revision 对应的消息条数"起算）；自举场景第一次仍需全量发送
    #     （sent_count=0），之后才转为增量。
    #   - 不使用服务端会话（cursor 判定/新建失败/未接入 TurnState）时
    #     sent_count 恒为 0，等价于旧行为（每轮全量转换 loop_messages）。
    sent_count = 0
    if using_stateful_conversation and not need_bootstrap and chat_id is not None:
        # 复用现有 cursor：cursor.synced_revision 对应的是"canonical
        # history 在建立/续接 cursor 那一刻的条数"，而 loop_messages 的
        # 前半段就是那份 canonical history（select_request_context 未
        # 触发兜底裁剪时是全量透传，见 context_manager.py）。因此可以
        # 直接用 canonical_revision 的语义近似为"loop_messages 里对应
        # 那部分的长度"——两者在未压缩历史的常态路径下条数一致。
        # 兜底：任何长度不匹配（历史被压缩导致条数与 revision 不再
        # 一一对应）一律退化为 sent_count=0（全量发送，安全但少一次
        # 优化），不会产生错误的截断。
        st = await _conv_state.get_conversation_state(chat_id)
        candidate = st.responses.synced_revision
        if 0 <= candidate <= len(loop_messages):
            sent_count = candidate
        logger.debug(
            "[openai_responses] 复用服务端会话 conversation=%s synced=%s "
            "loop_len=%s -> sent_count=%s",
            conversation_id, candidate, len(loop_messages), sent_count,
        )
    elif using_stateful_conversation and need_bootstrap:
        logger.info(
            "[openai_responses] chat=%s 自举新 Responses 会话 conversation=%s",
            chat_id, conversation_id,
        )

    for _round in range(MAX_TOOL_CALLS):
        if using_stateful_conversation:
            # 增量模式：只转换尚未发送过的部分。首轮自举时 sent_count=0，
            # 等价于全量转换（与旧行为逐字节一致）；工具调用续轮时
            # sent_count 已推进到上一次请求发出后的 loop_messages 长度，
            # 这里自然只转换本轮新增的 assistant(tool_calls)/tool 消息。
            delta_messages = loop_messages[sent_count:]
            instructions, input_items = _convert_messages_to_responses_input(delta_messages)
            # instructions（系统提示）只在自举轮携带一次：Responses 的
            # conversation 是"新建即空白"的容器，系统指令作为 canonical
            # history 最前面的 system 消息，天然会在 delta_messages 里
            # 只出现一次（sent_count=0 的那一轮）；后续增量轮
            # delta_messages 不含 system 消息，instructions 自然为空，
            # 不会重复携带——这正是我们想要的语义（Responses 的
            # instructions 参数是"本次请求覆盖"而非"追加"，重复携带
            # 反而更省事但没有必要，服务端已经记得第一轮的 instructions）。
        else:
            instructions, input_items = _convert_messages_to_responses_input(loop_messages)
        if RESPONSES_EXPLICIT_CACHE_ENABLED and not using_stateful_conversation:
            # 显式缓存断点只在"仍然全量发送 input"的传统模式下有意义
            # （断点挂在 system 段首尾）；增量模式下 delta_messages 通常
            # 根本不含 system 段，打断点没有目标、也没有必要——服务端
            # 会话本身就是比 prompt cache 更彻底的"不重复计算"机制。
            _apply_responses_cache_breakpoints(input_items)

        request_kwargs: dict[str, Any] = {
            "model": current_model,
            "input": input_items,
            "stream": True,
            "max_output_tokens": max_tokens,
        }
        if using_stateful_conversation:
            request_kwargs["conversation"] = conversation_id
            # 这次请求即将把 loop_messages[sent_count:len(loop_messages)]
            # 作为 input 发出去；把 sent_count 推进到当前长度，下一轮
            # （工具调用续轮）自然只转换从这里往后新增的部分。必须在
            # 发请求"之前"就推进（而不是等响应回来再推进）——即使这次
            # 请求最终失败/被打断，循环也不会再次进入下一轮迭代（要么
            # 抛出异常终止整个回合，要么正常 break），不存在"同一段
            # 消息被重复计入 sent_count 又被重复发送"的重复计数风险。
            sent_count = len(loop_messages)
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
        # 服务端会话 commit 素材：response.completed 事件里读取，回合
        # 正常结束（无更多工具调用）后用于 conversation_state.
        # commit_responses_cursor。工具调用续轮也会被覆盖为最新一次，
        # 只有循环最终退出时的值参与 commit——语义上"这一整个 turn
        # 最终同步到了哪个 response"，而不是中间某一轮。
        resolved_conversation_id: Optional[str] = conversation_id if using_stateful_conversation else None
        resolved_response_id: Optional[str] = None

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
                    if resp_obj is not None:
                        resolved_response_id = getattr(resp_obj, "id", None) or resolved_response_id
                        # 网关若未回传 conversation 字段（例如不支持该
                        # 功能的中转直接透传给不认识的字段），保留请求时
                        # 已知的 conversation_id（本来就是我们自己传入
                        # 或自建的），不会因为响应缺字段而误判失效。
                        resp_conv = getattr(resp_obj, "conversation", None)
                        resp_conv_id = getattr(resp_conv, "id", None) if resp_conv is not None else None
                        if resp_conv_id:
                            resolved_conversation_id = resp_conv_id

                elif etype in ("response.failed", "response.incomplete"):
                    resp_obj = getattr(event, "response", None)
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

    # ---- Conversation State：回合结束，尝试 commit 服务端会话游标 ----
    # 只有本回合确实用了服务端会话（resolved_conversation_id 非空）且
    # 调用方接入了 conversation_state（turn 非 None）才尝试写入；写入
    # 前的 fencing 校验（turn.generation 是否仍是发起回合时的那个）在
    # commit_responses_cursor 内部完成，过期回合（回合进行期间发生了
    # /clear）的迟到结果会被安全丢弃，不会复活一个本该失效的 cursor。
    if chat_id is not None and turn is not None and resolved_conversation_id:
        try:
            committed = _conv_state.commit_responses_cursor(
                chat_id, turn,
                conversation_id=resolved_conversation_id,
                response_id=resolved_response_id,
                model=current_model,
            )
            logger.debug(
                "[openai_responses] chat=%s conversation cursor commit=%s conversation=%s",
                chat_id, committed, resolved_conversation_id,
            )
        except Exception:
            # commit 失败绝不能影响本轮已经产出的正常回复——降级为
            # "下一轮重新自举"，用户侧无感知，只是错失一次增量优化。
            logger.debug("[openai_responses] commit_responses_cursor 异常（忽略）", exc_info=True)

    return final_content, final_usage, new_history_entries


__all__ = [
    "openai_responses_chat_completions_create",
    "_agentic_loop_openai_responses",
    "_convert_messages_to_responses_input",
    "_convert_tools_to_responses",
    "_responses_usage_to_openai",
    "_resolve_conversation_plan",
    "_create_responses_conversation",
    "_responses_stateful_enabled",
]

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

3. 服务端会话状态（多厂商同步状态机，2026-09 按需求文档重写）：
   ==================================================
   上面两条原则描述的是"边界转换"这一层，与是否使用服务端会话无关，
   继续对全部调用方成立。本节描述的是**请求侧的同步机制**：当调用方
   通过 ``turn``（conversation_state.TurnState）接入了对话状态层时，
   本文件按 conversation_state 的多厂商状态机运作——日常态（增量优先）
   复用服务端 ``conversation`` 对象，只把"尚未发给服务端的增量消息"
   放进 ``input``；分叉态（本地压缩 / 跨厂商写入 / 传统模型登记缺失 /
   ``/clear`` 之后）作废旧会话 id，以本地全量上下文一次性"自举"建立
   新会话（服务端前缀匹配自动命中 Prompt Cache）；检测到服务端压缩
   事件时登记回拉同步（server_compaction 持锁覆盖本地镜像）。

   这一机制完全不影响原则 1/2：
     - canonical history（loop_messages / new_history_entries）依然是
       全量的内部 Message 列表，只读，从不因为"发没发给服务端"而被
       裁剪或改写；
     - 只有"即将序列化成 Responses input"这一步会按对象身份切片
       （sent_ids / local_assistant_ids，见 _TurnSyncContext），切片
       只影响这一次网络请求的 payload，不影响任何持久化路径。

   详细设计与 fencing（TIMER/USER 并发、/clear、模型切换）见
   conversation_state.py 模块头注释；本文件内的接入点集中在
   "Conversation State：多厂商会话同步状态机接入"一节。
"""
import hashlib
import json
import uuid
from dataclasses import dataclass, field
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


_RESPONSES_TTL = "30m"


def _add_responses_cache_options(
    request_kwargs: dict[str, Any],
    *,
    api_label: str,
    model: str,
    chat_id: Any,
    enabled: bool = True,
) -> None:
    """注入稳定 key 和自动缓存（implicit 模式）。

    只发送 ``mode=implicit``，兼容只支持自动缓存的中转；implicit 模式
    本身就是按最长匹配前缀自动命中的缓存机制，不需要额外的显式尾部
    断点（是否打断点由模型级 supports_prompt_cache 开关决定）。
    """
    if not enabled:
        return
    request_kwargs["prompt_cache_key"] = _responses_prompt_cache_key(
        api_label, model, chat_id
    )
    # implicit 自动缓存；ttl 当前只有 30m 这一档。
    request_kwargs["prompt_cache_options"] = {
        "mode": "implicit",
        "ttl": _RESPONSES_TTL,
    }


# =============================================================================
# Conversation State：多厂商会话同步状态机接入（需求文档 一/二）
# =============================================================================
# 本节把 _agentic_loop_openai_responses 接入 conversation_state 的多厂商
# 状态机（详见该模块 docstring）。回合级语义：
#
#   1) 回合开始（第一次进入 for _round 循环之前）：
#      - 由 model_info 推导厂商分区键 vendor_key（provider|endpoint|
#        protocol，厂商间 ID 强隔离）；
#      - 为请求视图中未发号的 Message 补 seq（镜像副本经 meta 携带既有
#        seq；本回合私有新条目——TIMER 合成 user 消息、中段 system 通知
#        ——发号后按厂商无关写入者记台账）；
#      - plan_vendor_request 判定增量 vs 自举：
#          * 日常态（incremental）：ref 有效、结构纪元匹配、水位之后镜像
#            条目的写入者全部 ∈ {"user", 本厂商} ⇒ 复用 conversation_id，
#            本轮只发送"seq > 水位 / 本回合新增"的增量 input；
#          * 分叉态（bootstrap）：本地压缩 / 跨厂商写入 / 传统模型登记
#            缺失 / 会话不存在 ⇒ 作废旧 id，新建 conversation 对象，以
#            本地全量上下文做首轮自举（服务端前缀匹配命中 Prompt Cache）。
#   2) 工具调用续轮（同一个 for _round 迭代继续）：不重新判定有效性——
#      同一回合共用同一个 conversation_id。增量切片按"对象身份"追踪：
#      sent_ids 记录已发出的视图条目，local_assistant_ids 记录本回合由
#      服务端响应产生的 assistant 消息（其 output item 已随响应自动进入
#      服务端会话，重发会造成重复条目）；每次请求后 loop_messages 新增的
#      tool 结果自然成为下一轮的增量 input。
#   3) 服务端压缩监听（需求文档 二.1）：流式事件与 response.completed
#      元数据中检测到 compaction / 历史截断标识 ⇒ 登记待回拉同步；
#      回合收尾后由 server_compaction.run_pending_server_sync 持 chat 锁
#      执行"GET items -> Adapter 清洗 -> 覆盖本地镜像"。
#   4) 回合结束 commit（fencing）：写入厂商会话的水位（本回合已发号最大
#      seq）与结构纪元；/clear 后迟到的 commit 被 generation fencing 拒绝。
#   5) 异常兜底：回合中途异常 / 被打断 ⇒ 作废本回合使用的厂商会话
#      （分叉态重建，避免服务端残留半轮内容与本地镜像静默错位）；
#      增量首轮请求失败（4xx 会话类错误）⇒ 当场作废并以全量自举重试一次。
#
# 环境开关（默认开启）：允许在观察到网关侧对 `conversation` 参数支持
# 不稳定时整体回退到旧的"每轮全量重发"行为（等价于永远 stateless），
# 不影响功能正确性，只是放弃 token/延迟优化。
import os as _os


def _responses_stateful_enabled() -> bool:
    raw = _os.getenv("RESPONSES_STATEFUL_CONVERSATION_ENABLED", "true")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class _TurnSyncContext:
    """单个回合内的服务端会话同步上下文（桥接层私有）。

    sent_ids：请求视图中"已经作为 input 发出"的条目对象 id——增量切片
        按对象身份而非列表下标，天然免疫出站视图裁剪与中段 system 通知
        带来的下标错位（旧实现 revision≈条数 假设的缺陷根因）。
    local_assistant_ids：本回合由服务端响应产生的 assistant 消息对象 id
        ——这些内容的 output item 已随响应自动登记进服务端会话，不能
        再次作为增量 input 重发。
    max_seq：本回合已发号的最大镜像 seq（commit 时作为同步水位——覆盖
        user 提前持久化 + 回合内全部新增单元）。
    dispatched：是否已有请求真正发往服务端（增量失败兜底与中断作废的
        判据）。
    """

    vendor_key: str
    mode: str  # "incremental" | "bootstrap"
    conversation_id: str
    sent_ids: set[int] = field(default_factory=set)
    local_assistant_ids: set[int] = field(default_factory=set)
    max_seq: int = 0
    dispatched: bool = False
    bootstrap_retried: bool = False


async def _begin_turn_sync(
    client: "AsyncOpenAI",
    chat_id: Any,
    turn: Optional["_conv_state.TurnState"],
    model_info: Any,
    current_model: str,
    loop_messages: list,
) -> Optional[_TurnSyncContext]:
    """回合开始时建立（或放弃）服务端会话同步上下文。

    返回 None 表示本回合走无状态全量路径（未接入 TurnState / 总开关
    关闭 / 自举建会话失败 / 状态机异常兜底），调用方按旧行为每轮全量
    转换 loop_messages——任何异常都不阻断回合主流程（需求文档 三.2
    边界条件异常捕获）。
    """
    if chat_id is None or turn is None or not _responses_stateful_enabled():
        return None
    try:
        return await _begin_turn_sync_inner(
            client, chat_id, turn, model_info, current_model, loop_messages,
        )
    except Exception:
        logger.warning(
            "[openai_responses] chat=%s 会话同步计划异常，本轮回退无状态全量",
            chat_id, exc_info=True,
        )
        return None


async def _begin_turn_sync_inner(
    client: "AsyncOpenAI",
    chat_id: Any,
    turn: Optional["_conv_state.TurnState"],
    model_info: Any,
    current_model: str,
    loop_messages: list,
) -> Optional[_TurnSyncContext]:
    if chat_id is None or turn is None or not _responses_stateful_enabled():
        return None
    vendor_key = _conv_state.derive_vendor_key(model_info)
    state = await _conv_state.get_conversation_state(chat_id)

    # 为视图内未发号的 Message 补 seq。镜像副本经 meta 携带既有 seq；
    # 回合私有新条目（TIMER 合成 user 消息 / 中段 system 通知）发号后
    # 按厂商无关写入者（user）记台账——它们要么随 append-back 进入镜像，
    # 要么只存在于本轮请求（不入镜像的合成条目，其 seq 留在台账中无副作用）。
    for msg in loop_messages:
        if isinstance(msg, Message) and _conv_state.SEQ_META_KEY not in msg.meta:
            msg.meta[_conv_state.SEQ_META_KEY] = state.next_seq()
            if msg.role in ("user", "system"):
                state.record_append([msg], _conv_state.WRITER_USER)

    # 候选同步单元的序列号（非 system 全量参与写入者检查；system 条目
    # 走 instructions，且均已有台账或水位覆盖）。非 Message 形状（旧 dict
    # 兼容路径）以 None 占位 → plan 判定为未发号条目 → 保守分叉。
    candidate_seqs: list[Optional[int]] = []
    for msg in loop_messages:
        if not isinstance(msg, Message):
            candidate_seqs.append(None)
            continue
        if msg.role == "system":
            continue
        seq = msg.meta.get(_conv_state.SEQ_META_KEY)
        candidate_seqs.append(seq if isinstance(seq, int) else None)

    plan = _conv_state.plan_vendor_request(
        chat_id, vendor_key, current_model, candidate_seqs, _head_instructions_key(loop_messages)
    )
    sync_max_seq = state.last_seq

    if plan.mode == "incremental" and plan.conversation_id:
        # 日常态增量：预置"水位已覆盖"的条目为已发送。
        sent_ids: set[int] = set()
        for msg in loop_messages:
            if not isinstance(msg, Message):
                continue
            seq = msg.meta.get(_conv_state.SEQ_META_KEY)
            if isinstance(seq, int) and seq <= plan.synced_through_seq:
                sent_ids.add(id(msg))
        ctx = _TurnSyncContext(
            vendor_key=vendor_key,
            mode="incremental",
            conversation_id=plan.conversation_id,
            sent_ids=sent_ids,
            max_seq=sync_max_seq,
        )
        logger.info(
            "[openai_responses] chat=%s 日常态增量复用会话 conversation=%s "
            "水位=%s 视图单元=%s 增量单元=%s",
            chat_id, plan.conversation_id, plan.synced_through_seq,
            len(candidate_seqs), len(loop_messages) - len(sent_ids),
        )
    else:
        # 分叉态自举（或首次建立）：作废已由 plan 完成，新建 conversation。
        new_conversation_id = await _create_responses_conversation(client)
        if new_conversation_id is None:
            # 建会话失败：整轮回退无状态全量；下一轮自然再次尝试自举。
            return None
        ctx = _TurnSyncContext(
            vendor_key=vendor_key,
            mode="bootstrap",
            conversation_id=new_conversation_id,
            max_seq=sync_max_seq,
        )
        logger.info(
            "[openai_responses] chat=%s 自举新 Responses 会话 conversation=%s "
            "（原因=%s，全量上下文 %s 条）",
            chat_id, new_conversation_id, plan.reason, len(loop_messages),
        )
    turn.sync_ctx = ctx  # 供回合中断/异常路径作废对应厂商会话
    return ctx


def _invalidate_on_interrupt(builder: Any, turn: Optional["_conv_state.TurnState"]) -> None:
    """回合异常 / 被打断时作废本回合使用的厂商会话（分叉态兜底）。

    仅在"确有请求发往服务端"（dispatched）时作废——请求未出网时服务端
    会话未被污染，保留 ref 可继续享受增量。本地镜像是单一事实来源，
    作废后的下一轮请求会以本地全量上下文重建新会话。
    """
    ctx = getattr(turn, "sync_ctx", None) if turn is not None else None
    if ctx is None or not getattr(ctx, "dispatched", False):
        return
    chat_id = getattr(builder, "chat_id", None)
    if chat_id is None:
        return
    try:
        _conv_state.invalidate_vendor_session(
            chat_id, ctx.vendor_key, "turn_interrupted_midflight"
        )
        logger.info(
            "[openai_responses] chat=%s 回合中断/异常，作废厂商会话进入分叉态 "
            "vendor=%s",
            chat_id, ctx.vendor_key,
        )
    except Exception:
        logger.debug("[openai_responses] 中断作废厂商会话失败（忽略）", exc_info=True)


def _head_instructions_key(loop_messages: list) -> str:
    """头部 system 段（服务端会话 instructions 的来源）的稳定指纹。

    增量轮不重发头部 system 段——本地系统提示变化（技能激活 / 能力面
    变化等）通过指纹比对感知：不匹配 ⇒ 结构分叉，作废重建，保证模型
    始终拿到当前系统提示。
    """
    parts: list[str] = []
    for msg in loop_messages:
        if isinstance(msg, Message) and msg.role == "system":
            parts.append(msg.text())
            continue
        break
    return hashlib.sha256("\n\n".join(parts).encode("utf-8")).hexdigest()


def _is_conversation_error(exc: BaseException) -> bool:
    """判定增量首轮请求失败是否属于"会话类 4xx"错误。

    服务端会话过期 / conversation 对象不存在 / 条目校验失败等都会以
    4xx 状态返回；401/403（鉴权）与 429（限流）不属于会话问题，
    不做自举重试（避免重复撞限）。非 SDK 状态错误（连接异常等）
    同样不重试——由上层统一的错误路径处理。
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        return False
    return 400 <= status < 500 and status not in (401, 403, 429)


def _compaction_detect_signal(event: Any) -> Optional[str]:
    """转发 server_compaction 的事件检测（惰性导入避免环）。"""
    from server_compaction import detect_compaction_signal

    return detect_compaction_signal(event)


def _compaction_detect_metadata(resp_obj: Any) -> Optional[str]:
    """转发 server_compaction 的响应元数据检测（惰性导入避免环）。"""
    from server_compaction import detect_compaction_metadata

    return detect_compaction_metadata(resp_obj)


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

    异常兜底（需求文档 三.2）：回合中途异常 / 被打断时，作废本回合使用
    的厂商会话（分叉态）——服务端残留的半轮内容不允许与本地镜像静默
    错位，下一轮请求以本地全量上下文重建新会话。
    """
    try:
        return await _agentic_loop_openai_responses_impl(
            client, current_model, messages, builder,
            api_label=api_label, tools=tools, supports_tools=supports_tools,
            journal=journal, turn=turn,
        )
    except BaseException:
        _invalidate_on_interrupt(builder, turn)
        raise


async def _agentic_loop_openai_responses_impl(
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

    # ---- Conversation State：建立本回合的服务端会话同步上下文 --------
    # chat_id 取自 builder（DraftManager 透传自 get_ai_response，全部
    # 调用路径统一可用）。_begin_turn_sync 完成三件事：厂商分区键推导、
    # 视图发号、plan（增量 vs 自举）。工具调用触发的后续轮次复用同一个
    # conversation_id，不重新判定。
    chat_id = getattr(builder, "chat_id", None)
    sync_ctx = await _begin_turn_sync(
        client, chat_id, turn, model_info, current_model, loop_messages,
    )
    using_stateful_conversation = sync_ctx is not None
    conversation_id = sync_ctx.conversation_id if sync_ctx else None

    for _round in range(MAX_TOOL_CALLS):
        if sync_ctx is not None:
            # 增量模式：只转换"尚未发出且非本回合服务端产物"的条目。
            # - sent_ids（对象身份）：水位内条目 + 本回合已发出的条目；
            #   按对象身份而非列表下标切片，免疫出站视图裁剪与中段
            #   system 通知造成的下标错位。
            # - local_assistant_ids：本回合由服务端响应产生的 assistant
            #   消息——其 output item 已随响应自动进入服务端会话，重发
            #   会造成重复条目；只有配对的 tool 结果（function_call_output）
            #   需要作为增量发送。
            # - 头部 system 段（instructions 来源）：自举轮已随首轮
            #   instructions 登记进服务端会话；增量轮不重发（变化由
            #   _head_instructions_key 指纹守卫触发分叉重建）；中段/
            #   尾部的 system 通知（TIMER / 静默提示）仍随本轮 delta。
            # 首轮自举时 sent_ids 为空，等价于全量转换（与旧行为一致）。
            delta_messages: list = []
            _in_head_system = sync_ctx.mode == "incremental"
            for _msg in loop_messages:
                if _in_head_system and isinstance(_msg, Message) and _msg.role == "system":
                    continue
                _in_head_system = False
                if id(_msg) in sync_ctx.sent_ids or id(_msg) in sync_ctx.local_assistant_ids:
                    continue
                delta_messages.append(_msg)
            instructions, input_items = _convert_messages_to_responses_input(delta_messages)
            # instructions（系统提示）语义：自举轮携带头部 system 段一次
            # （服务端会话记住它）；后续增量轮 delta 不含头部 system，
            # instructions 自然为空，不会重复携带；系统提示变化通过指纹
            # 比对触发分叉重建（见 _head_instructions_key）。TIMER /
            # 静默回合的尾部 system 通知属"本轮新增条目"，随本轮 delta
            # 作为 instructions 携带——与旧版行为一致。
        else:
            instructions, input_items = _convert_messages_to_responses_input(loop_messages)

        request_kwargs: dict[str, Any] = {
            "model": current_model,
            "input": input_items,
            "stream": True,
            "max_output_tokens": max_tokens,
        }
        if sync_ctx is not None:
            request_kwargs["conversation"] = sync_ctx.conversation_id
            # 这次请求即将把 delta_messages 作为 input 发出；先把它们
            # 登记为已发送（对象身份），下一轮（工具调用续轮）自然只转换
            # 之后新增的部分。必须在发请求"之前"登记——即使这次请求
            # 最终失败/被打断，循环也不会再次进入下一轮迭代（要么抛出
            # 异常终止整个回合（中断兑底作废会话），要么正常 break），
            # 不存在同一段条目被重复发送的重复计数风险。
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
        # commit_vendor_sync。工具调用续轮也会被覆盖为最新一次，
        # 只有循环最终退出时的值参与 commit——语义上"这一整个 turn
        # 最终同步到了哪个 response"，而不是中间某一轮。
        resolved_conversation_id: Optional[str] = conversation_id if sync_ctx is not None else None
        resolved_response_id: Optional[str] = None

        switch_stream = make_switch_stream(builder, current_stream_cell)

        try:
            await start_chat_action(builder.chat_id, "typing")
            if sync_ctx is not None:
                # 本轮 delta 即将发出：先按对象身份登记为已发送（见上方
                # request_kwargs["conversation"] 处的说明）。
                sync_ctx.sent_ids.update(id(m) for m in delta_messages)
            try:
                stream = await client.responses.create(**request_kwargs)
            except Exception as create_exc:
                if sync_ctx is not None and sync_ctx.dispatched:
                    raise
                if (
                    sync_ctx is None
                    or chat_id is None
                    or sync_ctx.mode != "incremental"
                    or sync_ctx.bootstrap_retried
                ):
                    raise
                if not _is_conversation_error(create_exc):
                    raise
                # 日常态增量首轮请求失败（会话类 4xx，例如服务端会话
                # 已过期 / 条目校验失败）：作废会话进入分叉态，当场以
                # "全量自举"重试一次（下一轮迭代 sent_ids 已清空，
                # delta = 全量视图，携带新 conversation 建立新会话）。
                sync_ctx.bootstrap_retried = True
                _conv_state.invalidate_vendor_session(
                    chat_id, sync_ctx.vendor_key, "incremental_request_failed"
                )
                retried_conversation_id = await _create_responses_conversation(client)
                if retried_conversation_id is None:
                    raise
                logger.warning(
                    "[openai_responses] chat=%s 增量请求失败（%r），作废旧会话并以"
                    "全量自举重试 conversation=%s",
                    chat_id, create_exc, retried_conversation_id,
                )
                sync_ctx.conversation_id = retried_conversation_id
                sync_ctx.mode = "bootstrap"
                sync_ctx.sent_ids.clear()
                continue
            if sync_ctx is not None:
                sync_ctx.dispatched = True
                conversation_id = sync_ctx.conversation_id
            async for event in stream:
                etype = getattr(event, "type", None)

                # ---- 服务端压缩事件监听（需求文档 二.1）----------------
                # 只登记待回拉事项（每厂商去重），绝不在流式中途抢占
                # 镜像；回合收尾后由 server_compaction 持锁执行回拉覆盖。
                compaction_reason = _compaction_detect_signal(event)
                if compaction_reason and sync_ctx is not None and chat_id is not None:
                    _conv_state.request_server_sync(
                        chat_id, sync_ctx.vendor_key, f"stream:{compaction_reason}"
                    )
                    logger.info(
                        "[openai_responses] chat=%s 检测到服务端压缩事件 %s（已登记回拉同步）",
                        chat_id, compaction_reason,
                    )

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
                        # ---- 服务端压缩标识（响应体元数据，二.1）------
                        metadata_reason = _compaction_detect_metadata(resp_obj)
                        if metadata_reason and sync_ctx is not None and chat_id is not None:
                            _conv_state.request_server_sync(
                                chat_id, sync_ctx.vendor_key, f"metadata:{metadata_reason}"
                            )
                            logger.info(
                                "[openai_responses] chat=%s 响应元数据检测到压缩标识 %s（已登记回拉同步）",
                                chat_id, metadata_reason,
                            )

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
        finalized_assistant_msg = live_slot.finalize(loop_messages, content_acc, tool_calls_list, reasoning_acc)
        if sync_ctx is not None:
            # 本回合由服务端响应产生的 assistant 消息：其 output item 已
            # 随响应自动进入服务端会话，后续增量轮不再重发（否则服务端
            # 会话会出现重复条目）。
            sync_ctx.local_assistant_ids.add(id(finalized_assistant_msg))

        if not tool_calls_list:
            final_content = content_acc
            finish_open_tool_group(builder)
            await builder.finalize_turn()
            break

        status = await run_tool_batch(builder, tool_calls_list, loop_messages,
                                      new_history_entries, tool_call_count_ref,
                                      api_label, tools)

        if sync_ctx is not None and chat_id is not None:
            # 为回合内新增条目（run_tool_batch 追加的 tool 结果、错误占位
            # 等）补发 seq 并推进水位追踪；同时捕获所有尚未发出的
            # assistant 对象（防御：异常分支可能直接追加 assistant 错误
            # 消息），避免它们被误当作增量重发。
            new_last_seq = _conv_state.ensure_mirror_sequenced(chat_id, loop_messages)
            if new_last_seq > sync_ctx.max_seq:
                sync_ctx.max_seq = new_last_seq
            for _m in loop_messages:
                if (
                    isinstance(_m, Message)
                    and _m.role == "assistant"
                    and id(_m) not in sync_ctx.sent_ids
                    and id(_m) not in sync_ctx.local_assistant_ids
                ):
                    sync_ctx.local_assistant_ids.add(id(_m))

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

    # ---- Conversation State：回合结束，提交厂商会话同步账本（fencing）----
    # 只有本回合确实使用了服务端会话且调用方接入了 conversation_state
    # （turn 非 None）才提交；提交前的 generation fencing 校验在
    # commit_vendor_sync 内部完成——回合进行期间发生了 /clear 的过期
    # 回合，其迟到结果会被安全丢弃，绝不复活已作废的会话。
    # 水位 = 本回合已发号最大 seq（覆盖 user 提前持久化 + 回合内全部
    # 新增单元；append-back 稍后把同一批对象落入镜像，seq 已预发号，
    # 无需等待追加完成即可对齐）。
    if sync_ctx is not None and chat_id is not None and turn is not None and resolved_conversation_id:
        try:
            committed = _conv_state.commit_vendor_sync(
                chat_id, turn,
                vendor_key=sync_ctx.vendor_key,
                conversation_id=resolved_conversation_id,
                model=current_model,
                synced_through_seq=sync_ctx.max_seq,
                instructions_hash=_head_instructions_key(loop_messages),
            )
            logger.info(
                "[openai_responses] chat=%s 厂商会话同步提交=%s vendor=%s mode=%s "
                "conversation=%s 水位=%s",
                chat_id, committed, sync_ctx.vendor_key, sync_ctx.mode,
                resolved_conversation_id, sync_ctx.max_seq,
            )
            if committed:
                # 存在待执行的服务端压缩回拉（本回合或更早回合登记的）：
                # 派发后台同步（持 chat 锁 + Adapter 覆盖镜像 + 兑底）。
                from server_compaction import maybe_spawn_server_sync
                maybe_spawn_server_sync(chat_id)
        except Exception:
            # commit 失败绝不能影响本轮已经产出的正常回复——降级为
            # "下一轮重新自举"，用户侧无感知，只是错失一次增量优化。
            logger.debug("[openai_responses] commit_vendor_sync 异常（忽略）", exc_info=True)

    return final_content, final_usage, new_history_entries


__all__ = [
    "openai_responses_chat_completions_create",
    "_agentic_loop_openai_responses",
    "_convert_messages_to_responses_input",
    "_convert_tools_to_responses",
    "_responses_usage_to_openai",
    "_begin_turn_sync",
    "_create_responses_conversation",
    "_responses_stateful_enabled",
]

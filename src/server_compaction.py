# -*- coding: utf-8 -*-
"""服务端压缩（Server-side Compaction）监听与同步（需求文档 二.1）。

职责（与 conversation_state.py 的状态机配合）：
================================================================
1. **事件监听**：在 Responses API 的 Streaming 事件流及响应体元数据中
   监听 ``compaction``、历史截断或上下文压缩标识（``detect_compaction_signal``
   / ``detect_compaction_metadata``）。检测命中后**只登记**待同步事项
   （``conversation_state.request_server_sync``），绝不在回合流式中途
   抢占镜像——覆盖动作一律延后到回合收尾、持 chat 锁执行。

2. **回拉覆盖**：一旦登记了压缩事件，``run_pending_server_sync`` 立即
   （或在当前回合收尾后）发起异步请求调用
   ``GET /v1/conversations/{conversation_id}``（SDK 的
   ``conversations.items.list``，分页拉全量 items），拉取服务端最新的
   真实消息列表。

3. **数据清洗（Adapter）**：``adapt_items_to_messages`` 把拉取到的专有
   items（message / function_call / function_call_output；reasoning、
   tool 内部状态等专有形状）清洗并适配为通用的标准 ``messages`` 结构
   （内部 ``core.messages.Message``），随后覆盖并更新本地上下文历史镜像
   （保留镜像头部的 system 摘要槽位，替换对话主体；重新发号并记入
   写入者台账 ``server_sync``）。

4. **并发加锁**：回拉覆盖执行期间持有该 chat 的会话锁（chat lock）并把
   ref 相位置为 ``SYNCING``，确保同步完成前下一轮用户输入不能抢占写入
   本地镜像；若存在在途回合（``active_turn_ids``），回拉自动让路并重新
   登记，由该回合收尾后再触发。

5. **异常兜底（需求文档 三.2）**：
   - 拉取失败（网络 / 超时 / 网关不支持 items 端点 / 客户端缺失）⇒ 本地
     镜像**原样保留**（本地镜像是单一事实来源，绝不因同步失败被破坏），
     作废该厂商会话进入分叉态，下一轮以本地全量上下文重建新会话；
   - 超限捕获（条目数 / 页数超上限 ``PullOverflowError``）⇒ 同上，保守
     作废重建；
   - /clear 时对已绑定的服务端会话做尽力而为的后台删除（可关），失败
     静默——服务端遗留会话不影响本地状态机的正确性。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import TYPE_CHECKING, Any, Optional

from utils import get_logger

import conversation_state as _conv_state
from conversation_state import ConversationPhase, SEQ_META_KEY

if TYPE_CHECKING:
    from openai import AsyncOpenAI

from core.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

logger = get_logger(__name__)


# =============================================================================
# 环境开关与上限
# =============================================================================
def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# 回拉分页上限（超限视为服务端状态异常，保守作废重建——超限捕获）。
SYNC_MAX_PAGES = _env_int("RESPONSES_SYNC_MAX_PAGES", 40)
SYNC_PAGE_LIMIT = max(1, min(_env_int("RESPONSES_SYNC_PAGE_LIMIT", 100), 100))
# 单次 items 拉取 / 删除调用的超时（秒）。
SYNC_ITEM_TIMEOUT = _env_float("RESPONSES_SYNC_TIMEOUT", 15.0)
# 回拉清洗后的镜像条目数上限（超出保留最近 N 条，更早的条目放弃——
# 它们属于历史内容，本地镜像以"能继续增量对话"为准）。
SYNC_MAX_ITEMS = _env_int("RESPONSES_SYNC_MAX_ITEMS", 2000)
# /clear 时是否尽力删除服务端会话对象（不阻塞 /clear，失败静默）。
DELETE_CONVERSATION_ON_CLEAR = _env_bool("RESPONSES_DELETE_CONVERSATION_ON_CLEAR", True)

# 服务端压缩事件的类型匹配模式（小写子串）。除内置模式外，可通过
# RESPONSES_COMPACTION_EVENT_PATTERNS 追加网关自定义事件名（逗号分隔）。
_BUILTIN_EVENT_PATTERNS: tuple[str, ...] = (
    "compaction",
    "compacted",
    "conversation.truncated",
    "history.truncated",
    "context.truncated",
    "conversation_items.truncated",
    "context_window.compact",
)
_EXTRA_EVENT_PATTERNS: tuple[str, ...] = tuple(
    p.strip().lower()
    for p in str(os.getenv("RESPONSES_COMPACTION_EVENT_PATTERNS", "")).split(",")
    if p.strip()
)


class PullOverflowError(RuntimeError):
    """回拉条目数 / 页数超出安全上限（超限捕获：转入分叉重建兜底）。"""


# =============================================================================
# 1. 事件监听：Streaming 事件流 + 响应体元数据
# =============================================================================
def _event_patterns() -> tuple[str, ...]:
    return _BUILTIN_EVENT_PATTERNS + _EXTRA_EVENT_PATTERNS


def detect_compaction_signal(event: Any) -> Optional[str]:
    """从一条 Streaming 事件中识别服务端压缩 / 截断 / 上下文压缩标识。

    命中返回事件类型字符串（作为登记原因），未命中返回 None。识别规则
    刻意保守（子串匹配 + 语义限定），避免把普通业务事件误判为压缩。
    """
    etype = getattr(event, "type", None)
    if not etype or not isinstance(etype, str):
        return None
    low = etype.lower()
    for pattern in _event_patterns():
        if pattern in low:
            return etype
    # 截断语义必须同时出现"上下文/历史/会话/条目"限定词，避免把
    # 输出截断（例如 response.incomplete + max_output_tokens）误判为
    # 服务端对历史会话的压缩。
    if "truncat" in low and any(
        key in low for key in ("history", "context", "conversation", "item")
    ):
        return etype
    return None


def detect_compaction_metadata(resp_obj: Any) -> Optional[str]:
    """从响应体元数据中识别服务端压缩标识。

    兼容两类形状：
      - 专用字段：``response.compaction``（部分网关在发生历史压缩后于
        响应本体上直接给出标记 / 摘要信息）；
      - metadata 键：``response.metadata`` 中带 compaction/compacted/
        truncat 语义的键（值为真值才算命中）。
    """
    if resp_obj is None:
        return None
    compaction = getattr(resp_obj, "compaction", None)
    if compaction:
        return "response.compaction"
    metadata = getattr(resp_obj, "metadata", None)
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            low = str(key).lower()
            hit = (
                "compaction" in low
                or "compacted" in low
                or ("truncat" in low and any(k in low for k in ("history", "context", "conversation", "item")))
            )
            if hit and value:
                return f"metadata.{key}"
    return None


# =============================================================================
# 2. 回拉：GET /v1/conversations/{conversation_id}（items 分页拉全量）
# =============================================================================
async def pull_conversation_items(client: "AsyncOpenAI", conversation_id: str) -> list[Any]:
    """分页拉取服务端会话的全部 items（升序 = 会话时间线顺序）。

    带一次重试（瞬时网络故障）；页数超限抛 ``PullOverflowError``（不重试
    ——服务端状态超出可安全清洗的规模，交由分叉重建兜底）。
    """
    if client is None:
        raise RuntimeError("no client for conversation pull")
    items: list[Any] = []
    last_error: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            page_items: list[Any] = []
            after: Optional[str] = None
            pages = 0
            while True:
                kwargs: dict[str, Any] = {
                    "limit": SYNC_PAGE_LIMIT,
                    "order": "asc",
                    "timeout": SYNC_ITEM_TIMEOUT,
                }
                if after:
                    kwargs["after"] = after
                page = await client.conversations.items.list(conversation_id, **kwargs)
                data = getattr(page, "data", None) or []
                page_items.extend(data)
                pages += 1
                if pages > SYNC_MAX_PAGES:
                    raise PullOverflowError(
                        f"conversation items pages>{SYNC_MAX_PAGES} (conv={conversation_id})"
                    )
                if not bool(getattr(page, "has_more", False)):
                    break
                after = getattr(page, "last_id", None)
                if not after:
                    # 游标缺失但声明还有更多：防御性终止，按已拉取内容处理。
                    logger.warning(
                        "[server_compaction] 分页游标缺失（has_more=True 但无 last_id），"
                        "按已拉取的 %s 条继续",
                        len(page_items),
                    )
                    break
            return page_items
        except PullOverflowError:
            raise
        except Exception as exc:  # 网络抖动重试一次
            last_error = exc
            if attempt == 2:
                break
            await asyncio.sleep(0.5)
    raise RuntimeError(f"conversation items pull failed: {last_error!r}")


# =============================================================================
# 3. 数据清洗（Adapter）：专有 items -> 通用标准 messages 结构
# =============================================================================
def _item_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
            if isinstance(data, dict):
                return data
        except Exception:  # pragma: no cover - SDK 形状异常
            pass
    # 兜底：按属性读取关键域
    return {
        "type": getattr(item, "type", None),
        "role": getattr(item, "role", None),
        "content": getattr(item, "content", None),
        "call_id": getattr(item, "call_id", None),
        "name": getattr(item, "name", None),
        "arguments": getattr(item, "arguments", None),
        "output": getattr(item, "output", None),
    }


def _content_parts_to_blocks(parts: Any) -> list[Any]:
    """Responses message item 的 content parts -> 内部内容块。

    未识别的 part 类型跳过（与 responses_bridge 出站转换的防御策略一致）。
    """
    blocks: list[Any] = []
    if isinstance(parts, str):
        if parts:
            blocks.append(TextBlock(parts))
        return blocks
    if not isinstance(parts, list):
        return blocks
    for part in parts:
        if isinstance(part, str):
            if part:
                blocks.append(TextBlock(part))
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("output_text", "input_text", "text", "summary_text"):
            text = part.get("text")
            if isinstance(text, str) and text:
                blocks.append(TextBlock(text))
        elif ptype in ("input_image", "image"):
            url = part.get("image_url") or part.get("url") or ""
            if url:
                blocks.append(ImageBlock(url=str(url)))
        elif ptype in ("input_file", "file"):
            # 文件内容不回放（二进制不可重建），降级为文本占位。
            name = part.get("filename") or part.get("file_id") or "document"
            blocks.append(TextBlock(f"[document] {name}"))
        else:
            continue
    return blocks


def _output_to_text(output: Any) -> str:
    """function_call_output.output -> 字符串（结构化形状 json 序列化）。"""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(output)


def adapt_items_to_messages(items: list[Any]) -> tuple[list[Message], dict[str, int]]:
    """服务端会话 items -> 标准内部 ``Message`` 列表（数据清洗 Adapter）。

    清洗规则（需求文档 二.1：专有 items 如 reasoning、tool 内部状态等
    清洗为通用标准 messages 结构）：
      - ``message`` item：按 role 还原 user / assistant 文本（与多模态）
        消息；assistant 纯文本与 function_call 拆分存储于服务端，还原时
        按时间线各自成条（与出站转换的单向展开对称，重放语义等价）。
      - ``function_call``：暂存为待配对调用，遇到配对的
        ``function_call_output`` 时成组物化
        （assistant(tool_calls) + tool(result)，保证出站重放的配对完整
        性，绝不产生悬空 tool_use）。并行多调用的输出乱序/分批到达时，
        已物化的 call_id 直接补对应 tool 结果（配对在已发出的
        assistant 消息上依然成立）。
      - ``function_call_output``：配对物化；无主输出（其 function_call
        已被服务端压缩丢弃）跳过并计数——孤立 tool 结果重放会破坏协议
        配对约束。
      - ``reasoning`` / 未知类型：跳过并计数（推理内部状态不入通用镜像；
        未知形状保守丢弃，不让单个异常 item 阻断整个同步）。
      - 末尾仍未配对的 function_call：丢弃并计数（同悬空 tool_use 规则）。
    """
    messages: list[Message] = []
    stats: dict[str, int] = {
        "message": 0,
        "function_call": 0,
        "function_call_output": 0,
        "skipped_reasoning": 0,
        "skipped_unknown": 0,
        "skipped_orphan_call": 0,
        "skipped_orphan_output": 0,
        "skipped_empty": 0,
    }
    # 待配对调用（时间线顺序）：(call_id, name, arguments_json)
    pending_calls: list[tuple[str, str, str]] = []
    # 已随某条 assistant 消息物化的 call_id（并行调用的输出分批到达时，
    # 配对目标在已物化的 assistant 消息上，依然成立）。
    materialized_call_ids: set[str] = set()

    def _flush_calls_through(call_id: str) -> None:
        """把 pending_calls 中从头到 call_id（含）的调用物化为一条
        assistant(tool_calls) 消息，并登记已物化 id。"""
        idx = next((i for i, pc in enumerate(pending_calls) if pc[0] == call_id), -1)
        if idx < 0:
            return
        batch = pending_calls[: idx + 1]
        del pending_calls[: idx + 1]
        tool_calls = [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
            for cid, name, args in batch
        ]
        messages.append(Message.assistant_with_tool_calls("", tool_calls))
        materialized_call_ids.update(cid for cid, _n, _a in batch)

    for raw in items:
        data = _item_to_dict(raw)
        itype = data.get("type")

        if itype == "message":
            # 服务端把 assistant 文本与 function_call 拆为独立 item；
            # 文本 item 物化前先冲刷已配对完成的调用组，保持时间线顺序。
            if pending_calls:
                # 文本插在未配对调用之间：先物化这批调用（无输出配对，
                # 视为服务端已确认完成的一组），保持时间线单调。
                for cid, _n, _a in list(pending_calls):
                    _flush_calls_through(cid)
            role = data.get("role") or "user"
            blocks = _content_parts_to_blocks(data.get("content"))
            if not blocks:
                stats["skipped_empty"] += 1
                continue
            norm_role = role if role in ("user", "assistant") else "user"
            messages.append(Message(role=norm_role, blocks=blocks))
            stats["message"] += 1
            continue

        if itype == "function_call":
            call_id = str(data.get("call_id") or "")
            if not call_id:
                stats["skipped_orphan_call"] += 1
                continue
            pending_calls.append((
                call_id,
                str(data.get("name") or ""),
                _output_to_text(data.get("arguments") or "{}"),
            ))
            stats["function_call"] += 1
            continue

        if itype == "function_call_output":
            call_id = str(data.get("call_id") or "")
            name = _call_name_by_id(pending_calls, call_id)
            if call_id not in materialized_call_ids and name is None:
                # 无主输出：其 function_call 已被服务端压缩丢弃。
                stats["skipped_orphan_output"] += 1
                continue
            _flush_calls_through(call_id)  # call_id 在 pending 时先物化配对组
            output_text = _output_to_text(data.get("output"))
            messages.append(Message.tool_result(call_id, name or "", output_text))
            stats["function_call_output"] += 1
            continue

        if itype == "reasoning":
            stats["skipped_reasoning"] += 1
            continue

        stats["skipped_unknown"] += 1

    # 末尾未配对的 function_call：丢弃（悬空 tool_use 不可重放）。
    if pending_calls:
        stats["skipped_orphan_call"] += len(pending_calls)
        pending_calls.clear()

    # 超限捕获：条目数超上限时保留最近 N 条（历史条目让位于增量连续性）。
    if len(messages) > SYNC_MAX_ITEMS:
        dropped = len(messages) - SYNC_MAX_ITEMS
        messages = messages[-SYNC_MAX_ITEMS:]
        stats["skipped_overflow_tail"] = dropped
    return messages, stats


def _call_name_by_id(pending_calls: list[tuple[str, str, str]], call_id: str) -> Optional[str]:
    for cid, name, _args in pending_calls:
        if cid == call_id:
            return name
    return None


# =============================================================================
# 4. 回拉覆盖执行器（持 chat 锁 + SYNCING 相位）
# =============================================================================
# 每 chat 的回拉任务防重入登记。
_sync_tasks: set[int] = set()


def _client_for_model_name(model_name: Optional[str]) -> Optional["AsyncOpenAI"]:
    """按 ref.model 定位该厂商的 SDK 客户端（api_client 按模型缓存）。

    api_client 对 openai_responses 协议模型一律返回 AsyncOpenAI（原生
    Responses 复用 OpenAI SDK）；返回类型以 Any 承载以兼容底层联合类型。
    """
    if not model_name:
        return None
    try:
        from config import SUPPORTED_MODELS
        from api_client import api_client

        model_info: Any = SUPPORTED_MODELS.get(model_name)
        if model_info is None:
            return None
        client: Any = api_client.get_client_for_model(model_info)
        return client
    except Exception:
        logger.debug("[server_compaction] 定位厂商客户端失败", exc_info=True)
        return None


def maybe_spawn_server_sync(chat_id: int) -> None:
    """存在待回拉登记时派发后台同步任务（防重入；不阻塞调用方）。"""
    if not _conv_state.has_pending_server_sync(chat_id):
        return
    if chat_id in _sync_tasks:
        return
    try:
        task = asyncio.get_running_loop().create_task(run_pending_server_sync(chat_id))
    except RuntimeError:
        return
    _sync_tasks.add(chat_id)

    def _done(t: "asyncio.Task[Any]") -> None:
        _sync_tasks.discard(chat_id)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning(
                "[server_compaction] chat=%s 回拉同步任务异常（已兜底处理）: %r",
                chat_id, exc,
            )

    task.add_done_callback(_done)


async def run_pending_server_sync(chat_id: int) -> None:
    """执行全部待回拉登记：GET items -> Adapter 清洗 -> 覆盖本地镜像。

    逐厂商处理；每个厂商独立持锁、独立兜底：
      - 成功：镜像对话主体被服务端最新真实列表覆盖（保留 system 摘要
        槽位），ref 重新对齐水位 / 结构纪元，相位回到 DAILY；
      - 失败：本地镜像原样保留，作废该厂商会话（分叉态），下一轮请求
        以本地全量上下文重建新会话（拉取失败兜底）。
    """
    state = await _conv_state.get_conversation_state(chat_id)
    pendings = state.take_pending_server_syncs()
    if not pendings:
        return
    from state import get_chat_lock  # 惰性导入避免环

    lock = await get_chat_lock(chat_id)
    for vendor_key, reason in pendings.items():
        ref = state.get_vendor_ref(vendor_key)
        if ref is None or not ref.conversation_id or ref.phase != ConversationPhase.DAILY:
            # 会话已不存在 / 已因其他路径作废：无需回拉。
            continue
        async with lock:
            if state.active_turn_ids:
                # 有在途回合（本轮用户输入已开始抢占）：让路，重新登记，
                # 由该回合收尾后的 maybe_spawn_server_sync 再次触发——
                # 确保"同步完成前不被下一轮用户输入抢占写入"的相反面：
                # 也不在回合流式中途抢占镜像工作集。
                state.request_server_sync(vendor_key, reason)
                return
            ref.phase = ConversationPhase.SYNCING
            conversation_id = ref.conversation_id
            try:
                client = _client_for_model_name(ref.model)
                if client is None:
                    raise RuntimeError("no client for vendor (model missing or relocated)")
                items = await pull_conversation_items(client, conversation_id)
                adapted, stats = adapt_items_to_messages(items)
                _override_mirror(chat_id, state, ref, adapted)
                logger.info(
                    "[server_compaction] chat=%s vendor=%s 回拉覆盖完成 conv=%s 原因=%s "
                    "items=%s stats=%s",
                    chat_id, vendor_key, conversation_id, reason,
                    len(adapted), stats,
                )
            except Exception as exc:
                # 兜底：本地镜像原样保留（单一事实来源不可破坏），
                # 作废会话进入分叉态，下一轮全量自举重建。
                logger.warning(
                    "[server_compaction] chat=%s vendor=%s 回拉失败（作废会话 conv=%s "
                    "reason=%s）：%r",
                    chat_id, vendor_key, conversation_id, reason, exc,
                )
                state.invalidate_vendor(vendor_key, f"server_sync_failed:{type(exc).__name__}")
            finally:
                if ref.phase == ConversationPhase.SYNCING:
                    ref.phase = ConversationPhase.FORK
                    ref.fork_reason = ref.fork_reason or "server_sync_incomplete"


def _override_mirror(
    chat_id: int,
    state: "_conv_state.ConversationState",
    ref: "_conv_state.VendorConversationRef",
    adapted: list[Message],
) -> None:
    """把 Adapter 清洗后的服务端真实列表覆盖进本地镜像（持锁调用）。

    - 保留镜像头部的 system 槽位（滚动摘要 digest 等——服务端 items 不含
      系统提示，出站时按轮重建 instructions，头部槽位属于本地结构）；
    - 对话主体整体替换为 adapted（重新发号，writer=server_sync）；
    - 结构纪元 +1：其他厂商 ref 的镜像基准已被替换，全部作废（分叉态）；
    - 本厂商 ref 重新对齐水位 / 纪元 / 相位（DAILY）。
    """
    from state import get_or_init_context  # 惰性导入避免环

    ctx = get_or_init_context(chat_id)
    history = ctx.setdefault("conversation_history", [])

    head: list[Any] = []
    for entry in history:
        role = getattr(entry, "role", None) if not isinstance(entry, dict) else entry.get("role")
        if role == "system":
            head.append(entry)
        else:
            break

    state.record_append(adapted, _conv_state.WRITER_SERVER_SYNC)
    history[:] = head + adapted

    state.structural_epoch += 1
    for vendor_key, other in state.vendor_conversations.items():
        if vendor_key == ref.vendor_key:
            continue
        if other.conversation_id:
            other.invalidate("mirror_overridden_by_server_sync")
    ref.synced_through_seq = state.last_seq
    ref.synced_structural_epoch = state.structural_epoch
    ref.phase = ConversationPhase.DAILY
    ref.fork_reason = None
    ref.updated_at = time.time()
    # 结构性替换后旧 token 台账不再与剩余历史一一对应（与本地压缩 L2 同策略）。
    ctx["token_ledger"] = []
    ctx["last_prompt_tokens"] = 0
    ctx["last_completion_tokens"] = 0


# =============================================================================
# 5. /clear 的服务端会话清理（尽力而为，不阻塞）
# =============================================================================
def collect_and_spawn_conversation_deletions(chat_id: int) -> None:
    """快照当前全部厂商会话 id 并派发后台删除（/clear 时调用）。

    必须在 ``reset_conversation_state`` **之前**调用（reset 会清空映射）。
    删除失败（网关不支持 / 会话不存在 / 网络故障）一律静默——服务端
    遗留会话不影响本地状态机正确性。
    """
    if not DELETE_CONVERSATION_ON_CLEAR:
        return
    state = _conv_state.get_conversation_state_sync(chat_id)
    snapshots = [
        (ref.conversation_id, ref.model)
        for ref in state.vendor_conversations.values()
        if ref.conversation_id
    ]
    if not snapshots:
        return

    async def _delete_all() -> None:
        for conversation_id, model_name in snapshots:
            client = _client_for_model_name(model_name)
            if client is None:
                continue
            try:
                await asyncio.wait_for(
                    client.conversations.delete(conversation_id),
                    timeout=SYNC_ITEM_TIMEOUT,
                )
                logger.info(
                    "[server_compaction] chat=%s 已删除服务端会话 %s",
                    chat_id, conversation_id,
                )
            except Exception as exc:
                logger.debug(
                    "[server_compaction] chat=%s 删除服务端会话 %s 失败（忽略）: %r",
                    chat_id, conversation_id, exc,
                )

    try:
        asyncio.get_running_loop().create_task(_delete_all())
    except RuntimeError:
        return


__all__ = [
    "PullOverflowError",
    "detect_compaction_signal",
    "detect_compaction_metadata",
    "pull_conversation_items",
    "adapt_items_to_messages",
    "maybe_spawn_server_sync",
    "run_pending_server_sync",
    "collect_and_spawn_conversation_deletions",
]

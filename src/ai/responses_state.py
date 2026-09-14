# -*- coding: utf-8 -*-
"""OpenAI Responses API stateful 增量会话状态（Phase 1：stateful + fallback）。

双层模型（务必先读）：
====================================================
本地 conversation_history 是**唯一的业务真相**（完整历史 / 恢复 / 模型
切换 / 审计都靠它）；Responses server state（response_id 链）只是
"上游上下文指针"的一层缓存：

    ┌─────────────────────────────────────┐
    │ Local Conversation                  │
    │ 完整历史 / 恢复 / 模型切换 / 审计    │
    └──────────────────┬──────────────────┘
                       │ bootstrap / recovery only
                       ▼
    ┌─────────────────────────────────────┐
    │ Responses Conversation State        │  ← 本模块
    │ previous_response_id + 水位/指纹    │
    └──────────────────┬──────────────────┘
                       │ incremental input
                       ▼
                  POST /v1/responses

因此本模块的所有状态都必须可以被无条件丢弃：server state 失效
（previous_response_id 不可达 / 网关不支持 / 保留期过期 / 指纹不齐）
时，调用方丢掉指针、从本地历史重新 bootstrap 即可，不丢任何业务数据。

会话键设计：
====================================================
键为 ``(session_key, provider, endpoint, model)`` 四元组：

  - session_key = ``state.get_llm_session_key(chat_id)``，即
    ``tg-chat-{chat_id}-{纪元 token}``。纪元 token 在 /clear（清空对话）
    时轮换，因此清空对话后旧 server state 天然不可达（本地历史已清空，
    绝不允许再增量续上旧 server 链）；
  - provider + endpoint + model：模型/端点切换天然隔离——切到新模型时
    没有该模型的 response_id，自动从本地历史 bootstrap 新链；切回旧模型
    时旧链的指针还在，指纹对齐即可继续增量，无需复制任何历史。

水位与指纹（为什么需要）：
====================================================
只有 response_id 还不够：增量发送的前提是"server 链已经反映了本地历史
的哪个前缀"。本模块为每个会话记录：

  - synced_message_count：server 链已反映的本地**非 system** 消息条数
    （system 消息走 instructions 逐请求重发，不属于 server 链内容，也
    每轮都在变——时间戳/技能目录/静默模式提示——因此不参与计数与指纹）；
  - synced_fingerprint：上述前缀的归一化指纹。下一轮请求前重新计算并与
    存储值比对，不一致（历史被压缩/改写/裁剪、静默工具可见性切换、
    TIMER 唤醒消息未入历史导致前缀错位等）一律放弃增量、回落 bootstrap。
    这是"server state 是缓存不是业务数据"的机械保证。

归一化（指纹为什么能跨轮稳定）：
====================================================
出站消息每轮由 attachment_content._append_history_async 按当前模型能力
重建（user 消息重新解析附件、assistant 文本在入历史时被 strip）。指纹
对文本做 strip、对 http(s) URL 剥掉 query（R2 预签名 URL 每次重签都会
变化）、对 data: 内联内容只哈希不比对原文——这些变化不影响"server 链
内容与本地语义一致"这一判断；而压缩/改写等实质变化仍然会改变指纹。

存储形态：
====================================================
进程内存 LRU（与 conversation_history 的内存级生命周期一致：进程重启
两者同时清零，语义同为新会话），容量上限见 _RESPONSES_SESSIONS_MAX。
不持久化：response_id 指向的上游 state 本身有保留期（官方约 30 天），
持久化指针反而制造"重启后拿着过期指针撞墙"的额外路径。
"""
import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, List, Optional

from core.logging_setup import get_logger
from core.messages import (
    AudioBlock, DocumentBlock, ImageBlock, Message, ReasoningBlock,
    TextBlock, ToolCallBlock, ToolResultBlock, VideoBlock,
)
from state import get_llm_session_key

logger = get_logger(__name__)

__all__ = [
    "ResponsesSession",
    "get_responses_session",
    "save_responses_session",
    "drop_responses_session",
    "drop_responses_sessions_for_chat",
    "reset_responses_session_for_model",
    "count_input_visible_messages",
    "fingerprint_synced_prefix",
    "resolve_synced_prefix",
]


# =============================================================================
# 会话数据类
# =============================================================================
@dataclass
class ResponsesSession:
    """一个 (chat 会话, 厂商, 端点, 模型) 组合的 Responses server state 指针。

    除 response_id 外必须携带足够的元数据（ created_at / last_used_at /
    status），否则排查"state 为什么断了 / 模型切换后为什么重建了"时
    没有观测点。水位与指纹字段的语义见模块 docstring。
    """

    chat_id: Any
    session_key: str                    # tg-chat-{chat_id}-{纪元 token}（/clear 即轮换）
    provider: str
    endpoint: str
    model: str
    # 上游链的最后一个 response（response.completed / response.incomplete
    # 事件携带的 response.id）；None 表示当前无可用指针（下轮 bootstrap）。
    response_id: Optional[str] = None
    # server 链已反映的本地非 system 消息条数（水位）。
    synced_message_count: int = 0
    # 上述前缀的归一化指纹（fingerprint_synced_prefix 的返回值）。
    synced_fingerprint: str = ""
    created_at: float = 0.0
    last_used_at: float = 0.0
    # 最近一次推进时的响应状态（completed / incomplete 原因等），仅观测用。
    status: Optional[str] = None


# =============================================================================
# 进程内存 LRU 注册表
# =============================================================================
_RESPONSES_SESSIONS_MAX = 512
_responses_sessions: "OrderedDict[tuple, ResponsesSession]" = OrderedDict()


def _session_key_tuple(chat_id: Any, provider: str, endpoint: str, model: str) -> Optional[tuple]:
    """构造注册表键；无法定位会话（chat_id 为空 / session key 为空）时返回 None。"""
    if chat_id is None:
        return None
    session_key = get_llm_session_key(chat_id)
    if not session_key:
        return None
    return (session_key, str(provider or ""), str(endpoint or ""), str(model or ""))


async def get_responses_session(
        chat_id: Any, provider: str, endpoint: str, model: str) -> Optional[ResponsesSession]:
    """读取（并 LRU 触碰）该组合的 Responses 会话指针。

    无记录 / response_id 为空 / 无法定位会话时返回 None——调用方应视为
    "没有可用 server state"，走 bootstrap 全量发送。
    """
    key = _session_key_tuple(chat_id, provider, endpoint, model)
    if key is None:
        return None
    session = _responses_sessions.get(key)
    if session is None or not session.response_id:
        return None
    _responses_sessions.move_to_end(key)
    return session


async def save_responses_session(
        chat_id: Any,
        provider: str,
        endpoint: str,
        model: str,
        response_id: str,
        *,
        synced_message_count: int,
        synced_fingerprint: str,
        status: Optional[str] = None,
) -> Optional[ResponsesSession]:
    """写入/推进该组合的 server state 指针（每轮响应成功后调用）。

    首次写入时记录 created_at；推进时刷新 response_id / 水位 / 指纹 /
    last_used_at。水位语义：本次响应已完成并落入本地历史之后，本地
    非 system 消息总数（server 链此时恰好反映了全部这些内容）。
    无法定位会话（chat_id 为空）时返回 None（调用方按无 state 继续）。
    """
    key = _session_key_tuple(chat_id, provider, endpoint, model)
    if key is None:
        return None
    now = time.time()
    session = _responses_sessions.get(key)
    if session is None:
        session = ResponsesSession(
            chat_id=chat_id,
            session_key=key[0],
            provider=str(provider or ""),
            endpoint=str(endpoint or ""),
            model=str(model or ""),
            created_at=now,
        )
    session.response_id = response_id
    session.synced_message_count = max(0, int(synced_message_count))
    session.synced_fingerprint = synced_fingerprint
    session.status = status
    session.last_used_at = now
    _responses_sessions[key] = session
    _responses_sessions.move_to_end(key)
    while len(_responses_sessions) > _RESPONSES_SESSIONS_MAX:
        _responses_sessions.popitem(last=False)
    return session


async def drop_responses_session(chat_id: Any, provider: str, endpoint: str, model: str) -> None:
    """丢弃该组合的 server state 指针（server state 失效 / fallback 重建前调用）。

    只丢指针不丢业务数据——下一轮自动从本地历史 bootstrap 新链。
    """
    key = _session_key_tuple(chat_id, provider, endpoint, model)
    if key is None:
        return
    _responses_sessions.pop(key, None)


def drop_responses_sessions_for_chat(chat_id: Any) -> int:
    """清空对话时显式清扫该 chat 的全部 Responses 指针（/clear 联动）。

    注册表键里的会话纪元 token 轮换后旧条目已不可达，这里是双保险：
    按存储的 chat_id 扫描删除（int/str 归一比较），返回删除条数。
    """
    if chat_id is None:
        return 0
    chat_str = str(chat_id)
    stale = [k for k, s in _responses_sessions.items() if str(s.chat_id) == chat_str]
    for k in stale:
        _responses_sessions.pop(k, None)
    return len(stale)


async def reset_responses_session_for_model(chat_id: Any, model_info: Any) -> bool:
    """本地历史压缩事件后，显式丢弃该模型组合的 Responses 指针（Phase 3）。

    Phase 3 compaction 的核心协调点："本地历史压缩 + Responses server
    state reset 两个系统一起管理"。pre_flight_context_check 的压缩事件
    （L1 工具载荷归档 / L2 结构性淘汰）会改写本地历史——server 链与本地
    语义从此分叉。水位指纹机制本来就能在下轮请求时兜底检测（对不齐 →
    bootstrap），这里是显式提前 reset + 观测日志，避免浪费一次注定失败
    的增量请求，也让压缩事件在日志里可见。

    返回是否实际丢弃了指针（仅用于日志/测试断言）。
    """
    if model_info is None or chat_id is None:
        return False
    if not getattr(model_info, "responses_stateful", False):
        return False
    from config import get_effective_endpoint

    try:
        endpoint = get_effective_endpoint(model_info).endpoint or ""
    except Exception:
        endpoint = ""
    provider = str(getattr(model_info, "provider", "") or "")
    model = str(getattr(model_info, "model_id", "") or "")
    key = _session_key_tuple(chat_id, provider, endpoint, model)
    if key is None:
        return False
    existed = key in _responses_sessions
    _responses_sessions.pop(key, None)
    if existed:
        logger.info(
            "Responses state reset（本地历史压缩事件）: chat_id=%s model=%s "
            "—— server 链指针已丢弃，下一轮将从压缩后的本地历史 bootstrap",
            chat_id, model,
        )
    return existed


# =============================================================================
# 水位对齐：计数 / 指纹 / 增量起点
# =============================================================================
def _message_role(msg: Any) -> Optional[str]:
    """读取 Message（或 OpenAI 形状 dict）的 role。"""
    if isinstance(msg, Message):
        return msg.role
    if isinstance(msg, dict):
        role = msg.get("role")
        return str(role) if role is not None else None
    return getattr(msg, "role", None)


def count_input_visible_messages(messages: List[Any]) -> int:
    """统计会作为 input item 进入 server 链的消息条数（即非 system 消息）。

    system 消息走 instructions 逐请求重发，不进入 server 链，也不参与
    水位计数（见模块 docstring）。个别转换器会跳过的边缘消息（无
    tool_result_block 的 tool 消息、全空 user 消息等）在 bootstrap 与
    增量两种模式下同样不可见，计入水位无副作用。
    """
    return sum(1 for msg in messages if _message_role(msg) != "system")


# 归一化：http(s) URL 剥 query（R2 预签名 URL 每轮重签，query 全变）。
_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>\\)\]]+?)\?[^\s\"'<>\\)\]]*")


def _normalize_text(value: str) -> str:
    """指纹用文本归一化：URL 剥 query；data: 内联内容只留哈希。

    预签名 URL 的语义资源不变；data URL（base64 附件）字节数巨大且
    内容稳定，哈希即足够比对。
    """
    if value.startswith("data:"):
        return "data:sha256:" + hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:24]
    return _URL_QUERY_RE.sub(r"\1", value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _message_canonical(msg: Any) -> Optional[List[Any]]:
    """把一条消息归一化为指纹用结构（与 meta / 出站协议形状无关）。

    只读 blocks 与 role：Telegram 附件元数据（Message.meta）从不进出站
    请求体，也不参与指纹；消息按当前模型能力重建后 blocks 语义不变，
    配合 _normalize_text 保证指纹跨轮稳定。
    """
    if isinstance(msg, Message):
        m = msg
    elif isinstance(msg, dict):
        try:
            m = Message.from_openai_dict(msg)
        except Exception:
            return ["dict", _normalize_text(_canonical_json(msg))]
    else:
        return ["unknown", _normalize_text(_canonical_json(getattr(msg, "__dict__", msg)))]

    parts: List[Any] = [m.role]
    block_parts: List[Any] = []
    for b in m.blocks:
        if isinstance(b, TextBlock):
            # assistant 文本入历史时会被 strip（update_conversation_and_ledger），
            # 这里同样 strip，保证入历史前后指纹一致。
            block_parts.append(["text", _normalize_text((b.text or "").strip())])
        elif isinstance(b, ReasoningBlock):
            block_parts.append(["reasoning", _normalize_text(b.text or "")])
        elif isinstance(b, ToolCallBlock):
            block_parts.append(["call", b.id or "", b.name or "",
                                _normalize_text(_canonical_json(b.arguments or {}))])
        elif isinstance(b, ToolResultBlock):
            block_parts.append(["tool_output", b.tool_call_id or "",
                                _normalize_text(str(b.content or ""))])
        elif isinstance(b, ImageBlock):
            block_parts.append(["image", _normalize_text(b.url or ""), b.detail or ""])
        elif isinstance(b, DocumentBlock):
            block_parts.append(["document", b.filename or "",
                                _normalize_text(b.data_url or ""), _normalize_text(b.url or "")])
        elif isinstance(b, (AudioBlock, VideoBlock)):
            block_parts.append([type(b).__name__.lower(),
                                _normalize_text(getattr(b, "url", "") or "")])
        else:
            block_parts.append([type(b).__name__])
    if m.role == "assistant":
        # assistant 的 strip 入历史路径（set_text）会把 TextBlock 重排到
        # blocks 首位；reasoning/text/tool_calls 在出站渲染时本就按类型
        # 独立消费，块内顺序无语义——指纹按稳定键排序，跨重排稳定。
        block_parts.sort(key=_canonical_json)
    parts.extend(block_parts)
    return parts


def fingerprint_synced_prefix(messages: List[Any], synced_count: int) -> str:
    """计算本地消息列表前 ``synced_count`` 条非 system 消息的归一化指纹。"""
    canonical: List[Any] = []
    for msg in messages:
        if len(canonical) >= synced_count:
            break
        if _message_role(msg) == "system":
            continue
        parts = _message_canonical(msg)
        if parts is not None:
            canonical.append(parts)
    digest = hashlib.sha256()
    digest.update(f"n={synced_count};".encode("utf-8"))
    digest.update(_canonical_json(canonical).encode("utf-8"))
    return digest.hexdigest()[:32]


def resolve_synced_prefix(
        messages: List[Any], synced_count: int, synced_fingerprint: str) -> Optional[int]:
    """校验 server 链与本地历史前缀是否对齐；对齐时返回增量区间起始下标。

    返回值语义：
      - int  ：``messages[返回值:]`` 就是尚未被 server 链反映的增量区间
               （调用方转成 input item、带 previous_response_id 发送）；
      - None ：无法对齐（历史被压缩/改写/裁剪、消息变少、指纹不齐、
               水位为 0 等），调用方必须放弃增量、bootstrap 全量发送。

    指纹不一致是最重要的保护：它意味着本地历史与 server 链在语义上已经
    分叉（压缩/改写/可见性切换/唤醒消息未入历史的前缀错位），此时继续
    增量会让模型看到"自己没说过的历史"，必须回落 bootstrap 重立新链。
    """
    if synced_count <= 0:
        return None
    if count_input_visible_messages(messages) < synced_count:
        return None
    if fingerprint_synced_prefix(messages, synced_count) != synced_fingerprint:
        return None

    seen = 0
    for idx, msg in enumerate(messages):
        if _message_role(msg) == "system":
            continue
        seen += 1
        if seen == synced_count:
            # 增量从水位之后的第一个非 system 消息开始；水位与它之间
            # 若夹着 system 消息（静默模式尾部提示等），一并划入增量
            # 区间——它们不产生 input item，只会并入 instructions。
            for j in range(idx + 1, len(messages)):
                if _message_role(messages[j]) != "system":
                    return j
            # 水位之后已无非 system 消息：无增量可发，由调用方兜底。
            return len(messages)
    return None

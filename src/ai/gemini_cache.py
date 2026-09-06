"""Gemini 显式缓存（cachedContent API）管理器。

背景（2026-09-05 线上观测）：
====================================================================
Gemini 隐式缓存（implicit caching，usageMetadata.cachedContentTokenCount）
有三个固有短板，导致活跃 agentic 会话命中率长期偏低：

1. 缓存建立是**异步**的：一个前缀要 ~1-2 分钟才变成可命中——同一回合
   的工具循环（第 1 轮 prompt + functionCall/functionResponse = 第 2 轮
   prompt，间隔仅数秒）永远赶不上；
2. 缓存锚点**阶梯式**推进（实测 16.2K → 32.4K → 44.6K，每级间隔 3+
   分钟），活跃对话的未缓存尾巴持续增长，阶梯内命中率单调下滑；
3. TTL 仅 ~5 分钟，空闲即全灭（实测空闲 5m43s 后 Cached 归 0）。

方案：用**显式缓存**把"system prompt + 稳定历史前缀"主动建成
cachedContents 对象（TTL 10-30 分钟、命中即续期），请求体引用
cachedContent 并只发送当前回合的后缀。cached input 计价为普通 input
的 1/4，storage 按小时计费——对 11K-49K 输入的高频轮次显著划算。

设计约束（务必遵守）：
====================================================================
1. **纯增量能力**：只服务 Gemini 原生循环（gemini_bridge），不触碰
   OpenAI / Anthropic 的任何缓存策略；manager 不 import gemini_bridge
   （convert_fn / base_url / headers 由调用方注入），避免循环导入。
2. **绝不影响主流程**：acquire 的任何异常都吞掉并返回 None（回退全量
   请求）；请求被 Gemini 拒绝（400/404）时由调用方回退重试一次并
   invalidate 本条目（自我愈合，覆盖 TTL 竞态 / 工具面变化等一切
   "缓存与请求不一致"的场景）。
3. **前缀切分安全性**：缓存内容 = system 消息 + 最后一条 user 消息
   之前的全部历史；前缀收尾必须是合法悬挂点——仅允许 assistant 消息
   且不带 tool_calls（否则跨缓存边界会出现连续 user / 缺
   functionResponse 的非法序列）。转换函数对 prefix / suffix 分别调用
   与全量转换结果逐字节一致（合并逻辑只发生在相邻同角色之间，悬挂点
   已排除跨界合并）。
4. **键控**：key = sha1(model + tools声明 + prefix 消息逐条相等比较)。
   历史是 append-only 的（合并/改写会改变既有消息 → 相等比较失配 →
   自然换新 key 重建），因此命中即保证逐字节一致。

环境变量：
  GEMINI_EXPLICIT_CACHE=0            总开关（默认开）
  GEMINI_EXPLICIT_CACHE_TTL_S        缓存 TTL，默认 900（15 分钟）
  GEMINI_EXPLICIT_CACHE_MIN_TOKENS   创建阈值（前缀估算 token），默认 4096
  GEMINI_EXPLICIT_CACHE_MAX_ENTRIES  常驻条目上限（LRU 淘汰），默认 32
  GEMINI_EXPLICIT_CACHE_AWAIT_CREATE_S  acquire 等待在建缓存的时长上限，默认 3
"""
import asyncio
import copy
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import aiohttp

from utils import get_logger

logger = get_logger(__name__)


# =============================================================================
# 可调参数（环境变量）
# =============================================================================
def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _env_float(name: str, default: float, min_value: float, max_value: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


_ENABLED = _env_bool("GEMINI_EXPLICIT_CACHE", True)
_TTL_S = _env_int("GEMINI_EXPLICIT_CACHE_TTL_S", 900, 60, 86400)
_MIN_PREFIX_TOKENS = _env_int("GEMINI_EXPLICIT_CACHE_MIN_TOKENS", 4096, 0, 10 ** 9)
_MAX_ENTRIES = _env_int("GEMINI_EXPLICIT_CACHE_MAX_ENTRIES", 32, 1, 1000)
_AWAIT_CREATE_S = _env_float("GEMINI_EXPLICIT_CACHE_AWAIT_CREATE_S", 3.0, 0.0, 30.0)


# =============================================================================
# HTTP 薄封装（测试可 monkeypatch）
# =============================================================================
async def _http_json(method: str, url: str, headers: dict,
                     payload: Optional[dict], timeout_s: float) -> tuple[int, dict]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers,
                                   json=payload) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}
            return resp.status, data


def _err_text(data: dict) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or "")
    return str(data or "")


# =============================================================================
# 前缀切分（纯函数）
# =============================================================================
def _last_turn_boundary(messages: list) -> Optional[int]:
    """当前回合起始下标 = 最后一条 user 消息的位置；无 user 返回 None。"""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            return i
    return None


def _prefix_cache_safe(prefix: list) -> bool:
    """前缀收尾必须是 Gemini 内容序列的合法悬挂点。

    - 允许"纯 system 前缀"（无任何非 system 消息）；
    - 否则最后一条非 system 消息必须是 assistant 且不带 tool_calls：
      * tool 结尾 -> 跨界后出现连续 user（Gemini 拒绝）；
      * assistant+tool_calls 结尾 -> functionCall 之后必须紧跟
        functionResponse，不能从缓存续传。
    """
    non_system = [m for m in prefix
                  if isinstance(m, dict) and m.get("role") != "system"]
    if not non_system:
        return True
    last = non_system[-1]
    if last.get("role") != "assistant":
        return False
    if last.get("tool_calls"):
        return False
    return True


def split_for_cache(messages: list) -> Optional[tuple[list, list]]:
    """把 OpenAI 形状 loop_messages 切成 (可缓存前缀, 当前回合后缀)。

    不可切（无 user / 前缀为空 / 悬挂点不安全）返回 None。
    后缀永远以 user 消息开头（回合起点），与转换器的角色合并规则兼容。
    """
    if not messages:
        return None
    boundary = _last_turn_boundary(messages)
    if boundary is None or boundary == 0:
        return None
    prefix, suffix = messages[:boundary], messages[boundary:]
    if not suffix or not _prefix_cache_safe(prefix):
        return None
    return prefix, suffix


def _is_strict_prefix(short: list, messages: list) -> bool:
    if len(short) >= len(messages):
        return False
    try:
        return messages[:len(short)] == short
    except Exception:
        return False


def _estimate_tokens(messages: list) -> int:
    """前缀 token 估算（创建阈值判断用，允许粗略）。"""
    try:
        from token_budget import count_tokens
        chunks: list[str] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, str):
                chunks.append(content)
            elif content is not None:
                chunks.append(json.dumps(content, ensure_ascii=False))
            if m.get("tool_calls"):
                chunks.append(json.dumps(m["tool_calls"], ensure_ascii=False))
        return count_tokens("\n".join(chunks))
    except Exception:
        total = 0
        for m in messages:
            if isinstance(m, dict):
                total += len(str(m.get("content") or ""))
        return total // 4


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_expire_to_monotonic(expire_time: str) -> Optional[float]:
    """RFC3339 expireTime -> monotonic 时间戳；解析失败返回 None。"""
    if not expire_time or not isinstance(expire_time, str):
        return None
    try:
        dt = datetime.fromisoformat(expire_time.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return time.monotonic() + delta
    except Exception:
        return None


# =============================================================================
# 管理器
# =============================================================================
class GeminiCacheHandle:
    """一次请求对缓存的成功引用（不可变值对象）。"""

    __slots__ = ("name", "key", "prefix_len", "suffix_messages",
                 "chat_id", "model")

    def __init__(self, name: str, key: str, prefix_len: int,
                 suffix_messages: list, chat_id, model: str):
        self.name = name
        self.key = key
        self.prefix_len = prefix_len
        self.suffix_messages = suffix_messages
        self.chat_id = chat_id
        self.model = model


class _Entry:
    __slots__ = ("key", "name", "chat_id", "model", "tools_hash", "prefix",
                 "expire_at", "created_at", "last_used", "base_url", "headers")

    def __init__(self, key, name, chat_id, model, tools_hash, prefix,
                 expire_at, base_url, headers):
        self.key = key
        self.name = name
        self.chat_id = chat_id
        self.model = model
        self.tools_hash = tools_hash
        self.prefix = prefix
        self.expire_at = expire_at
        self.created_at = time.monotonic()
        self.last_used = self.created_at
        self.base_url = base_url
        self.headers = headers


class GeminiExplicitCacheManager:
    """cachedContents 生命周期管理（创建 / 复用 / 续期 / 失效 / 淘汰）。

    所有后台任务的异常都被吞掉（仅日志），绝不影响主流程。
    """

    def __init__(self, enabled: bool = None):
        self._enabled = _ENABLED if enabled is None else enabled
        self._entries: dict = {}                       # key -> _Entry
        self._inflight: dict = {}                      # chat_id -> {key: Task}
        self._background: set = set()                  # 防 GC 强引用
        self._chat_fail_until: dict = {}               # chat_id -> monotonic
        self._chat_fail_streak: dict = {}              # chat_id -> int
        self._ineligible_models: set = set()

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    async def acquire(self, chat_id, model: str, messages: list,
                      gemini_tools: Optional[list],
                      convert_fn: Callable, base_url: str, headers: dict,
                      ) -> Optional[GeminiCacheHandle]:
        """返回可引用的缓存句柄；同时尽力安排"理想前缀"的后台创建。

        - 可用缓存 = 与当前 messages 逐条相等的**最长严格前缀**条目；
        - 命中中若"理想前缀"（最后一条 user 之前的全部消息）有在建缓存，
          短暂等待（≤ AWAIT_CREATE_S），建好后升级为更长的命中；
        - 任何异常返回 None（调用方回退全量请求）。
        """
        if not self._enabled or not messages or model in self._ineligible_models:
            return None
        try:
            return await self._acquire_inner(
                chat_id, model, messages, gemini_tools,
                convert_fn, base_url, headers)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("gemini 显式缓存 acquire 失败（回退全量请求）",
                         exc_info=True)
            return None

    async def _acquire_inner(self, chat_id, model, messages, gemini_tools,
                             convert_fn, base_url, headers):
        tools_hash = _canonical(gemini_tools) if gemini_tools else "none"
        handle = self._find_handle(chat_id, model, tools_hash, messages)

        # 理想前缀（最后一条 user 之前）：安排创建 / 等待在建。
        ideal = split_for_cache(messages)
        if ideal is not None:
            prefix, _ = ideal
            ideal_key = self._key_for(model, tools_hash, prefix)
            entry = self._entries.get(ideal_key)
            now = time.monotonic()
            has_fresh = entry is not None and entry.expire_at > now
            inflight_task = self._inflight.get(chat_id or 0, {}).get(ideal_key)
            if not has_fresh and inflight_task is None and self._creation_allowed(chat_id):
                est = _estimate_tokens(prefix)
                if est >= _MIN_PREFIX_TOKENS:
                    self._start_creation(chat_id, model, tools_hash, prefix,
                                         ideal_key, est, convert_fn,
                                         base_url, headers, gemini_tools)
            elif inflight_task is not None and _AWAIT_CREATE_S > 0:
                # 在建：短暂等待（工具循环下一轮常在数秒后，等一下即可命中）。
                done, _pending = await asyncio.wait({inflight_task},
                                                    timeout=_AWAIT_CREATE_S)
                if done:
                    handle = (self._find_handle(chat_id, model, tools_hash,
                                                messages) or handle)

        if handle is not None:
            entry = self._entries.get(handle.key)
            if entry is not None:
                entry.last_used = time.monotonic()
                self._maybe_refresh(entry)
        return handle

    # ------------------------------------------------------------------
    # 失效（请求方收到 400/404 时调用；同步摘除 + 后台删除）
    # ------------------------------------------------------------------
    def invalidate(self, handle: GeminiCacheHandle, reason: str = "") -> None:
        if handle is None:
            return
        entry = self._entries.pop(handle.key, None)
        logger.info("[gemini-cache] invalidate %s (chat=%s, reason=%s)",
                    handle.name, handle.chat_id, reason or "unknown")
        if entry is not None:
            self._spawn(self._delete_entry(entry))

    # ------------------------------------------------------------------
    # 内部：查找 / 创建 / 续期 / 删除 / 淘汰
    # ------------------------------------------------------------------
    def _key_for(self, model: str, tools_hash: str, prefix: list) -> str:
        return hashlib.sha1(
            _canonical([model, tools_hash, prefix]).encode("utf-8")).hexdigest()

    def _find_handle(self, chat_id, model, tools_hash, messages):
        now = time.monotonic()
        best: Optional[_Entry] = None
        for entry in list(self._entries.values()):
            if (entry.chat_id != chat_id or entry.model != model
                    or entry.tools_hash != tools_hash):
                continue
            if entry.expire_at <= now:
                self._entries.pop(entry.key, None)
                continue
            if not _is_strict_prefix(entry.prefix, messages):
                continue
            if best is None or len(entry.prefix) > len(best.prefix):
                best = entry
        if best is None:
            return None
        return GeminiCacheHandle(
            name=best.name, key=best.key, prefix_len=len(best.prefix),
            suffix_messages=messages[len(best.prefix):],
            chat_id=chat_id, model=model)

    def _creation_allowed(self, chat_id) -> bool:
        return time.monotonic() >= self._chat_fail_until.get(chat_id or 0, 0.0)

    def _start_creation(self, chat_id, model, tools_hash, prefix, key,
                        est_tokens, convert_fn, base_url, headers,
                        gemini_tools) -> None:
        task = asyncio.ensure_future(self._create_entry(
            chat_id, model, tools_hash, prefix, key, est_tokens,
            convert_fn, base_url, headers, gemini_tools))
        self._inflight.setdefault(chat_id or 0, {})[key] = task
        self._background.add(task)

        def _done(t, chat_id=chat_id, key=key):
            self._background.discard(t)
            bucket = self._inflight.get(chat_id)
            if bucket is not None:
                bucket.pop(key, None)

        task.add_done_callback(_done)

    async def _create_entry(self, chat_id, model, tools_hash, prefix, key,
                            est_tokens, convert_fn, base_url, headers,
                            gemini_tools) -> None:
        display_name = f"atc-{chat_id or 0}-{key[:10]}"
        try:
            has_non_system = any(
                isinstance(m, dict) and m.get("role") != "system"
                for m in prefix)
            sys_text, contents = convert_fn(copy.deepcopy(prefix))
            if not has_non_system:
                # 纯 system 前缀：转换器会给空 contents 加 "(empty)" 占位
                # user turn（全量请求路径的防御兜底）。缓存体绝不能带它
                # ——会造成跨界连续 user + 上下文语义漂移；显式缓存支持
                # 仅 systemInstruction（+ tools）的缓存对象。
                contents = []
            body: dict = {"model": f"models/{model}", "ttl": f"{_TTL_S}s"}
            if sys_text:
                body["systemInstruction"] = {"parts": [{"text": sys_text}]}
            if contents:
                body["contents"] = contents
            if gemini_tools:
                body["tools"] = copy.deepcopy(gemini_tools)
            body["displayName"] = display_name
            status, data = await _http_json(
                "POST", f"{base_url}/cachedContents", headers, body, 90.0)
            if status in (200, 201) and isinstance(data, dict) and data.get("name"):
                name = data["name"]
                expire_at = (_parse_expire_to_monotonic(data.get("expireTime"))
                             or (time.monotonic() + _TTL_S))
                self._install_entry(_Entry(
                    key=key, name=name, chat_id=chat_id, model=model,
                    tools_hash=tools_hash, prefix=copy.deepcopy(prefix),
                    expire_at=expire_at, base_url=base_url,
                    headers=dict(headers)))
                self._chat_fail_streak.pop(chat_id or 0, None)
                logger.info(
                    "[gemini-cache] created %s (chat=%s, prefix_msgs=%s, "
                    "est_tokens=%s, ttl=%ss)", name, chat_id, len(prefix),
                    est_tokens, _TTL_S)
                self._evict_for(chat_id, keep_key=key)
                return
            err = _err_text(data) or f"HTTP {status}"
            lowered = err.lower()
            if status == 400 and ("not supported" in lowered
                                  or "does not support" in lowered):
                self._ineligible_models.add(model)
                logger.warning(
                    "[gemini-cache] 模型 %s 不支持显式缓存，已停用（%s）",
                    model, err[:200])
            self._register_failure(chat_id, status, err)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._register_failure(chat_id, 0, "exception")
            logger.debug("gemini 显式缓存创建异常", exc_info=True)

    def _register_failure(self, chat_id, status: int, err: str) -> None:
        streak = self._chat_fail_streak.get(chat_id or 0, 0) + 1
        self._chat_fail_streak[chat_id or 0] = streak
        backoff = min(3600.0, 300.0 * (2 ** (streak - 1)))
        self._chat_fail_until[chat_id or 0] = time.monotonic() + backoff
        logger.warning(
            "[gemini-cache] create failed (chat=%s, status=%s, streak=%s, "
            "backoff=%.0fs): %s", chat_id, status, streak, backoff, err[:300])

    def _install_entry(self, entry: _Entry) -> None:
        now = time.monotonic()
        for k in [k for k, e in self._entries.items() if e.expire_at <= now]:
            self._entries.pop(k, None)
        self._entries[entry.key] = entry
        while len(self._entries) > _MAX_ENTRIES:
            oldest_key = min(self._entries,
                             key=lambda k: self._entries[k].last_used)
            victim = self._entries.pop(oldest_key)
            self._spawn(self._delete_entry(victim))

    def _evict_for(self, chat_id, keep_key: str) -> None:
        """同一 chat 只保留最新前缀的条目（旧前缀必然不再命中）。"""
        for k, e in list(self._entries.items()):
            if e.chat_id == chat_id and k != keep_key:
                self._entries.pop(k, None)
                self._spawn(self._delete_entry(e))

    def _maybe_refresh(self, entry: _Entry) -> None:
        if entry.expire_at - time.monotonic() > _TTL_S / 2:
            return
        entry.expire_at = time.monotonic() + _TTL_S  # 乐观续期防抖
        self._spawn(self._refresh_entry(entry))

    async def _refresh_entry(self, entry: _Entry) -> None:
        try:
            url = f"{entry.base_url}/{entry.name}?updateMask=ttl"
            status, _data = await _http_json(
                "PATCH", url, entry.headers, {"ttl": f"{_TTL_S}s"}, 30.0)
            if status in (200, 201):
                entry.expire_at = time.monotonic() + _TTL_S
                logger.info("[gemini-cache] refresh %s -> ttl=%ss",
                            entry.name, _TTL_S)
            elif status in (400, 404):
                self._entries.pop(entry.key, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("gemini 显式缓存续期失败", exc_info=True)

    async def _delete_entry(self, entry: _Entry) -> None:
        try:
            await _http_json("DELETE", f"{entry.base_url}/{entry.name}",
                             entry.headers, None, 30.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("gemini 显式缓存删除失败（TTL 兜底）", exc_info=True)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(lambda t: self._background.discard(t))


#: 模块级单例（gemini_bridge 引用）
manager = GeminiExplicitCacheManager()

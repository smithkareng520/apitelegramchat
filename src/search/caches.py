"""web_search / fetch_url 结果的双 TTL 缓存（自 search_engine.py 拆出）。"""

import json
from urllib.parse import urlsplit, urlunsplit

from cachetools import TTLCache

from config import FETCH_CACHE_TTL, SEARCH_CACHE_TTL

import logging

logger = logging.getLogger(__name__)


# ---------- 缓存 ----------
_fetch_cache = TTLCache(maxsize=200, ttl=FETCH_CACHE_TTL)

# web_search 结果缓存：agent 循环里模型重复/改写同一查询报常见，命中后
# 直接返回上次的格式化结果，省 Serper 配额与延迟；TTL 由 SEARCH_CACHE_TTL
# 控制（默认 300s，与 fetch 缓存同一套环境变量风格）。
_search_cache = TTLCache(maxsize=200, ttl=SEARCH_CACHE_TTL)


def _search_cache_key(
    modes: list[str],
    query: str,
    requested: int,
    page: int | None,
    gl: str | None,
    hl: str | None,
    tbs: str | None,
    image_url: str | None,
) -> str:
    """把归一化后的搜索参数序列化成稳定的缓存键。"""
    return json.dumps(
        {
            "m": list(modes),
            "q": query,
            "n": requested,
            "p": page,
            "gl": gl or "",
            "hl": hl or "",
            "tbs": tbs or "",
            "iu": (image_url or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_cacheable_search_result(value: object) -> bool:
    """只缓存成功结果与确定性空结果；服务错误/异常不缓存，保证可重试。"""
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("❌ 未找到"):
        return True  # 确定性空结果，短期内复用可省配额
    return not value.startswith("❌")


def _normalize_fetch_cache_key(url: str) -> str:
    """Drop fragment so the same page maps to one cache entry."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except Exception:
        logger.debug("_normalize_fetch_cache_key 内部忽略的异常", exc_info=True)
        return url

# ========== 缓存函数 ==========
def get_fetch_cache(url: str) -> str | None:
    return _fetch_cache.get(_normalize_fetch_cache_key(url))


def set_fetch_cache(url: str, content: str) -> None:
    """写入 fetch 缓存。

    重要安全修复：此前所有失败结果（以 ``失败：`` 开头的字符串）也被写
    入缓存。这意味着任何一次网络抖动导致的失败都会让该 URL 在
    ``FETCH_CACHE_TTL``（默认 1 小时）内对所有后续调用直接返回缓存的
    失败字符串，即使网络已恢复也不会重试。现在改为只缓存成功结果，
    失败结果仍然返回给调用方但不写入缓存，让下一次调用有机会重试。
    """
    if isinstance(content, str) and content.startswith("失败："):
        # 失败结果不缓存，避免短暂网络抖动把 URL "中毒" 一整个 TTL 周期。
        return
    _fetch_cache[_normalize_fetch_cache_key(url)] = content

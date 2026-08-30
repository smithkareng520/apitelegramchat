"""fetch_url 的受控根路径首页回退规则。"""

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from apitelegramchat.web_search_settings import (
    FETCH_URL_ROOT_FALLBACK_ENABLED,
    FETCH_URL_ROOT_FALLBACK_PATHS,
)


def _configured_paths(paths: Any) -> tuple[str, ...]:
    """返回格式合法、去重后的同站点回退路径。"""
    if isinstance(paths, str):
        paths = (paths,)
    if not isinstance(paths, (tuple, list, set, frozenset)):
        return ()

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        if not isinstance(raw_path, str):
            continue
        candidate = raw_path.strip()
        if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
            continue
        try:
            parts = urlsplit(candidate)
        except ValueError:
            continue
        # 回退配置只能是站内绝对路径，不能带外部 host、查询参数或片段。
        if parts.scheme or parts.netloc or parts.query or parts.fragment:
            continue
        path = parts.path
        if path in {"", "/"} or path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return tuple(normalized)


ROOT_FALLBACK_PATHS = _configured_paths(FETCH_URL_ROOT_FALLBACK_PATHS)


def root_fallback_urls(url: str) -> tuple[str, ...]:
    """为合格的根路径 URL 生成同 origin 的首页回退候选地址。

    仅允许 HTTP(S) 根路径（空路径或 `/`）、且不带查询参数及片段的 URL 触发。
    这样不会改变用户给出的深层页面、带参数页面或外站目标。
    """
    if not FETCH_URL_ROOT_FALLBACK_ENABLED or not ROOT_FALLBACK_PATHS:
        return ()
    if not isinstance(url, str) or not url:
        return ()
    try:
        parts = urlsplit(url)
    except ValueError:
        return ()
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.netloc
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        return ()

    return tuple(
        urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        for path in ROOT_FALLBACK_PATHS
    )

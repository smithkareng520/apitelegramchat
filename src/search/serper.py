"""Serper 搜索客户端：web_search 工具与四类 mode 的解析/格式化（自 search_engine.py 拆出）。"""

import asyncio
from typing import Any


from web_search_settings import WEB_SEARCH_LANGUAGE, WEB_SEARCH_REGION
from web_search_filter import (
    BLACKLISTED_SEARCH_DOMAINS as _BLACKLISTED_SEARCH_DOMAINS,
    SEARCH_DEFAULT_RESULTS as _SEARCH_DEFAULT_RESULTS,
    SEARCH_MAX_CANDIDATES as _SEARCH_MAX_CANDIDATES,
    SEARCH_MAX_RESULTS as _SEARCH_MAX_RESULTS,
    candidate_result_count,
    filter_blacklisted_search_results as _filter_blacklisted_search_results,
)
from serper_api import (
    SerperError,
    SerperUnavailableError,
    SERPER_DEFAULT_TIMEOUT as _SERPER_DEFAULT_TIMEOUT,
)
from search.caches import _search_cache, _search_cache_key, _is_cacheable_search_result

import logging

logger = logging.getLogger(__name__)


# images/videos/lens 单请求的 num_results 上限（serper 文档口径）。
_SEARCH_MEDIA_MAX_RESULTS = 100
class SerperSearchTransientError(Exception):
    """Serper 上游临时未返回结果（例如 organic 为空），可重试。"""


SERPER_PAGE_SIZE = 10  # search 端点单页固定 10 条


def _serper_api_timeout() -> float:
    """读取 SERPER_API_TIMEOUT 配置（默认 12s）。"""
    try:
        from config import SERPER_API_TIMEOUT
        if isinstance(SERPER_API_TIMEOUT, (int, float)) and SERPER_API_TIMEOUT >= 1.0:
            return float(SERPER_API_TIMEOUT)
    except Exception:
        logger.debug("_serper_api_timeout 内部忽略的异常", exc_info=True)
        pass
    return _SERPER_DEFAULT_TIMEOUT


def _parse_serper_search_result(data: dict[str, Any] | None) -> list[dict]:
    """从 serper.dev /search 响应中提取 organic 列表为统一字段。

    Serper /search 的 organic item 字段（实测）:
      title, link, snippet, date(可选), rating(可选), ratingCount(可选), position
    本函数保留前 5 个有用字段；position 已被列表顺序编码，无需重复存储。
    rating 为浮点（如 4.3），ratingCount 为整数（如 30740），两者通常同时出现
    但偶有单独出现的情况，统一存为字符串便于下游条件性渲染。
    """
    if not isinstance(data, dict):
        return []
    organic = data.get("organic")
    if not isinstance(organic, list):
        return []
    items: list[dict] = []
    for result in organic:
        if not isinstance(result, dict):
            continue
        title = str(result.get("title") or "").strip() or "无标题"
        link = str(result.get("link") or "").strip()
        snippet = str(result.get("snippet") or "").strip()
        if not link:
            continue
        # rating 可能是 float 或字符串；统一规范化成可读字符串
        rating_raw = result.get("rating")
        rating = ""
        if isinstance(rating_raw, (int, float)) and rating_raw > 0:
            rating = f"{float(rating_raw):.1f}"
        elif isinstance(rating_raw, str) and rating_raw.strip():
            rating = rating_raw.strip()
        rating_count_raw = result.get("ratingCount")
        rating_count = ""
        if isinstance(rating_count_raw, int) and rating_count_raw > 0:
            rating_count = str(rating_count_raw)
        items.append({
            "title": title,
            "link": link,
            "snippet": snippet,
            "date": str(result.get("date") or "").strip(),
            "rating": rating,
            "rating_count": rating_count,
        })
    return items


def _parse_serper_images_result(data: dict[str, Any] | None) -> list[dict]:
    """从 serper.dev /images 响应中提取图片列表为统一字段。"""
    if not isinstance(data, dict):
        return []
    images = data.get("images")
    if not isinstance(images, list):
        return []
    items: list[dict] = []
    for result in images:
        if not isinstance(result, dict):
            continue
        title = str(result.get("title") or "").strip() or "无标题"
        image_url = str(result.get("imageUrl") or "").strip()
        link = str(result.get("link") or "").strip() or image_url
        if not image_url:
            continue
        items.append({
            "title": title,
            "image_url": image_url,
            "link": link,
            "thumbnail_url": str(result.get("thumbnailUrl") or "").strip(),
            "source": str(result.get("source") or result.get("domain") or "").strip(),
            "width": result.get("imageWidth"),
            "height": result.get("imageHeight"),
        })
    return items


def _parse_serper_videos_result(data: dict[str, Any] | None) -> list[dict]:
    """从 serper.dev /videos 响应中提取视频列表为统一字段。

    Serper /videos 的 item 字段（实测）:
      title, link(观看页 URL), snippet, imageUrl(封面图直链),
      videoUrl(可选，视频媒体直链), duration(可选), source, channel(可选), date, position

    ⚠️ 区分两类 URL:
      - link   = YouTube / Bilibili / Facebook 等观看页 HTML URL —— 给 <a href> 用，
                 不能塞进 <video src>，否则 Telegram 会报 RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND。
      - videoUrl = Google CDN 上的视频媒体直链 (encrypted-vtbn0.gstatic.com/video?q=...)，
                 是真正能塞进 <video src> 的 URL；不是每个 item 都有，缺失时留空。
    本函数把两者分别保存为 link / video_url，避免下游 AI 把它们搞混。
    duration 形如 "20:40" 或 "0:54"；channel 是发布者名（YouTube 频道、FB 主页等）。
    """
    if not isinstance(data, dict):
        return []
    videos = data.get("videos")
    if not isinstance(videos, list):
        return []
    items: list[dict] = []
    for result in videos:
        if not isinstance(result, dict):
            continue
        title = str(result.get("title") or "").strip() or "无标题"
        link = str(result.get("link") or "").strip()
        if not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "snippet": str(result.get("snippet") or "").strip(),
            "image_url": str(result.get("imageUrl") or "").strip(),
            "video_url": str(result.get("videoUrl") or "").strip(),
            "duration": str(result.get("duration") or "").strip(),
            "source": str(result.get("source") or "").strip(),
            "channel": str(result.get("channel") or "").strip(),
            "date": str(result.get("date") or "").strip(),
        })
    return items


def _parse_serper_lens_result(data: dict[str, Any] | None) -> list[dict]:
    """从 serper.dev /lens 响应中提取 organic 列表为统一字段。"""
    if not isinstance(data, dict):
        return []
    organic = data.get("organic")
    if not isinstance(organic, list):
        return []
    items: list[dict] = []
    for result in organic:
        if not isinstance(result, dict):
            continue
        title = str(result.get("title") or "").strip() or "无标题"
        link = str(result.get("link") or "").strip()
        image_url = str(result.get("imageUrl") or "").strip()
        if not link and not image_url:
            continue
        items.append({
            "title": title,
            "link": link,
            "image_url": image_url,
            "thumbnail_url": str(result.get("thumbnailUrl") or "").strip(),
            "source": str(result.get("source") or "").strip(),
        })
    return items


async def _serper_search_one_mode(
    mode: str,
    *,
    query: str | None,
    image_url: str | None,
    num: int | None,
    page: int | None,
    gl: str | None,
    hl: str | None,
    tbs: str | None,
) -> list[dict]:
    """执行单个 mode 的搜索，返回统一字段的结果列表。

    对于 search mode，单页固定 10 条；当 num > 10 时按页并发取回再合并，
    保持原有 offset 语义。其他 mode 直接用 serper 返回的列表。
    """
    timeout = _serper_api_timeout()

    if mode == "search":
        if not query:
            raise SerperSearchTransientError("search mode requires query")
        # search 端点 num 实际是 page 数：每页固定 10 条。把 num_results 折算成页。
        requested_num = num if isinstance(num, int) and num > 0 else _SEARCH_DEFAULT_RESULTS
        requested_num = min(max(requested_num, 1), _SEARCH_MAX_CANDIDATES)
        # 处理 offset（向后翻页）
        offset = max(int(page or 1) - 1, 0) * SERPER_PAGE_SIZE if page else 0
        # page 数从 1 开始
        first_page = offset // SERPER_PAGE_SIZE + 1
        last_idx = offset + requested_num - 1
        last_page = last_idx // SERPER_PAGE_SIZE + 1
        pages = range(first_page, last_page + 1)

        from serper_api import search as serper_search_api
        async def _fetch_page(p: int) -> list[dict]:
            data = await serper_search_api(
                query, gl=gl, hl=hl, tbs=tbs, page=p, timeout=timeout,
            )
            return _parse_serper_search_result(data)

        outcomes = await asyncio.gather(
            *(_fetch_page(p) for p in pages), return_exceptions=True,
        )
        items: list[dict] = []
        first_error: BaseException | None = None
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if first_error is None:
                    first_error = outcome
                continue
            items.extend(outcome)

        if not items and first_error is not None:
            if isinstance(first_error, SerperError):
                raise first_error
            raise SerperSearchTransientError(
                f"Serper search returned no results; first error: {first_error}"
            )
        if items and first_error is not None:
            # 部分页失败：拼出来的结果数量会少于请求量。不静默吞掉，
            # 留下排障线索（结果仍然返回，让调用方拿到可用部分）。
            logger.warning(
                "Serper search 部分页失败 modes=search pages=%s first_error=%s",
                list(pages), first_error,
            )
        # offset 只能是页对齐值（上面按页折算），无需子页偏移。
        return items[:requested_num]

    if mode == "images":
        if not query:
            raise SerperSearchTransientError("images mode requires query")
        from serper_api import images as serper_images_api
        data = await serper_images_api(
            query, num=num, page=page, gl=gl, hl=hl, tbs=tbs, timeout=timeout,
        )
        return _parse_serper_images_result(data)

    if mode == "videos":
        if not query:
            raise SerperSearchTransientError("videos mode requires query")
        from serper_api import videos as serper_videos_api
        data = await serper_videos_api(
            query, num=num, page=page, gl=gl, hl=hl, tbs=tbs, timeout=timeout,
        )
        return _parse_serper_videos_result(data)

    if mode == "lens":
        if not image_url:
            raise SerperSearchTransientError("lens mode requires image_url")
        from serper_api import lens as serper_lens_api
        data = await serper_lens_api(
            image_url, query=query, num=num, page=page, gl=gl, hl=hl, tbs=tbs, timeout=timeout,
        )
        return _parse_serper_lens_result(data)

    raise SerperSearchTransientError(f"unknown serper mode: {mode}")


def _format_search_results(items: list, query: str, engine: str, requested: int | None = None) -> str:
    """渲染 search 模式的 envelope section。

    字段顺序固定为：标题 → 摘要 → 时间(可选) → 链接 → 评分(可选)。
    时间/评分行仅在对应字段非空时才出现，避免给 AI 灌空行。
    评分行格式：`评分：4.3 ⭐ (30740 评价)`，无评价数则只输出星标。
    """
    success_count = len(items)
    requested_count = requested if isinstance(requested, int) and requested > 0 else success_count
    lines = [f"🔍 [成功: {engine}] 搜索「{query}」的结果（{success_count}/{requested_count}）：\n"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        snippet = item.get("snippet", "")
        date = item.get("date", "")
        link = item.get("link", "")
        rating = item.get("rating", "")
        rating_count = item.get("rating_count", "")
        block = f"{i}. 标题：{title}\n   摘要：{snippet}\n"
        if date:
            block += f"   时间：{date}\n"
        block += f"   链接：{link}\n"
        if rating:
            rating_line = f"   评分：{rating} ⭐"
            if rating_count:
                rating_line += f" ({rating_count} 评价)"
            rating_line += "\n"
            block += rating_line
        lines.append(block)
    return "\n".join(lines)


def _format_image_results(items: list, query: str, requested: int | None = None) -> str:
    success_count = len(items)
    requested_count = requested if isinstance(requested, int) and requested > 0 else success_count
    lines = [f"🖼️ [成功: Serper Images] 搜图「{query}」的结果（{success_count}/{requested_count}）：\n"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        image_url = item.get("image_url", "")
        link = item.get("link", "")
        source = item.get("source", "")
        lines.append(
            f"{i}. 标题：{title}\n"
            f"   图片：{image_url}\n"
            f"   来源：{source}\n"
            f"   页面：{link}\n"
        )
    return "\n".join(lines)


def _format_video_results(items: list, query: str, requested: int | None = None) -> str:
    """渲染 videos 模式的 envelope section。

    字段命名上做了关键区分，避免 AI 把观看页 URL 误当视频媒体 URL：
      页面 = link 字段，YouTube/Bilibili 等观看页 HTML URL —— 给 <a href> 用
      封面 = image_url 字段，封面图直链 —— 给 <img src> 用
      视频 = video_url 字段，Google CDN 视频媒体直链 —— 给 <video src> 用
    视频行只在 video_url 非空时才出现（不是每个 item 都有 videoUrl）。
    时长/频道/时间 同样条件性输出。
    """
    success_count = len(items)
    requested_count = requested if isinstance(requested, int) and requested > 0 else success_count
    lines = [f"🎬 [成功: Serper Videos] 搜视频「{query}」的结果（{success_count}/{requested_count}）：\n"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        snippet = item.get("snippet", "")
        duration = item.get("duration", "")
        source = item.get("source", "")
        channel = item.get("channel", "")
        date = item.get("date", "")
        link = item.get("link", "")
        image_url = item.get("image_url", "")
        video_url = item.get("video_url", "")
        block = f"{i}. 标题：{title}\n   摘要：{snippet}\n"
        if duration:
            block += f"   时长：{duration}\n"
        if source:
            block += f"   来源：{source}\n"
        if channel:
            block += f"   频道：{channel}\n"
        if date:
            block += f"   时间：{date}\n"
        block += f"   页面：{link}\n"
        if image_url:
            block += f"   封面：{image_url}\n"
        if video_url:
            block += f"   视频：{video_url}\n"
        lines.append(block)
    return "\n".join(lines)


def _format_lens_results(items: list, image_url: str, requested: int | None = None) -> str:
    success_count = len(items)
    requested_count = requested if isinstance(requested, int) and requested > 0 else success_count
    lines = [f"🔎 [成功: Serper Lens] 以图搜图「{image_url}」的结果（{success_count}/{requested_count}）：\n"]
    for i, item in enumerate(items, 1):
        title = item.get("title", "无标题")
        link = item.get("link", "")
        image_url_item = item.get("image_url", "")
        source = item.get("source", "")
        lines.append(
            f"{i}. 标题：{title}\n"
            f"   来源：{source}\n"
            f"   页面：{link}\n"
            f"   图片：{image_url_item}\n"
        )
    return "\n".join(lines)


def _normalize_modes(mode: str | list[str] | None) -> list[str]:
    """把 mode 参数规范化为去重后的有序 list。默认 ["search"]。"""
    if mode is None:
        return ["search"]
    if isinstance(mode, str):
        m = mode.strip().lower()
        if not m:
            return ["search"]
        return [m]
    if isinstance(mode, list):
        out: list[str] = []
        seen: set[str] = set()
        for m in mode:
            if not isinstance(m, str):
                continue
            normalized = m.strip().lower()
            if not normalized or normalized in seen:
                continue
            if normalized not in {"search", "images", "videos", "lens"}:
                continue
            seen.add(normalized)
            out.append(normalized)
        return out or ["search"]
    return ["search"]


async def execute_web_search(
    query: str | None = None,
    num_results: int | None = None,
    offset: int | None = None,
    *,
    mode: str | list[str] | None = "search",
    image_url: str | None = None,
    gl: str | None = None,
    hl: str | None = None,
    tbs: str | None = None,
) -> str:
    """通过 Serper 直连 API 搜索，支持 search / images / videos / lens 四种 mode。

    带结果缓存：归一化参数相同的重复查询在 SEARCH_CACHE_TTL（默认 300s）
    内直接返回上次的格式化结果。缓存覆盖主 agent、子 agent 与重试路径，
    agent 循环里模型重复同一查询时不再消耗 Serper 配额。

    单次调用可同时执行多个 mode（mode 为 list 时并发执行）。各 mode 的失败
    互不影响：成功 mode 的结果正常返回，失败 mode 在结果末尾以错误说明列出。

    参数：
      query:       搜索关键词。search / images / videos 必填；lens 可选。
      num_results: 单 mode 的结果数上限。search: 1-50（多页聚合）；
                   images / videos / lens: 1-100。
      offset:      search mode 的偏移量（向后翻页），其他 mode 忽略；
                   为兼容老调用方，等价于 page = offset // 10 + 1。
      mode:        "search"（默认） / "images" / "videos" / "lens"，或它们的 list。
      image_url:   lens mode 必填；其他 mode 忽略。
      gl:          地区码（如 us / cn），默认取 WEB_SEARCH_REGION。
      hl:          界面语言（如 en / zh-cn），默认取 WEB_SEARCH_LANGUAGE。
      tbs:         时间筛选（如 qdr:d 当天 / qdr:w 一周 / qdr:m 一月 / qdr:y 一年）。
    """
    # ---- 参数归一化（唯一一份；缓存 key 与执行共用同一结果，
    # 避免两份归一化逻辑各自漂移、缓存 key 碎片化） ----
    modes = _normalize_modes(mode)
    requested = _normalize_requested_results(num_results)
    # offset 只对 search mode 生效（schema/docstring 均如此声明）。
    # <10 等价于第 1 页，与不带 offset 归一到同一缓存 key，避免 p:1/p:null 碎片。
    page: int | None = None
    if offset is not None and "search" in modes:
        page = max(int(offset), 0) // SERPER_PAGE_SIZE + 1
        if page <= 1:
            page = None
    query_str = (query or "").strip()
    image_url_str = (image_url or "").strip()
    # 应用部署默认地区/语言，使 schema 中“默认 cn / zh-cn”的说明与实际行为一致。
    gl = gl or WEB_SEARCH_REGION
    hl = hl or WEB_SEARCH_LANGUAGE

    cache_key = _search_cache_key(
        modes, query_str, requested, page, gl, hl, tbs, image_url_str,
    )
    cached = _search_cache.get(cache_key)
    if cached is not None:
        logger.debug("Search cache hit: %s", cache_key[:160])
        return cached

    result = await _execute_web_search_uncached(
        modes=modes,
        query_str=query_str,
        requested=requested,
        page=page,
        image_url=image_url_str or None,
        gl=gl,
        hl=hl,
        tbs=tbs,
    )
    if _is_cacheable_search_result(result):
        _search_cache[cache_key] = result
    return result


def _normalize_requested_results(num_results: int | None) -> int:
    """归一化 num_results：非法/缺省回退默认值；全局上限 100（schema 口径）。

    各 mode 的差异化上限（search 50 / 其他 100）在执行时按 mode 再钳制。
    """
    if num_results is None:
        return _SEARCH_DEFAULT_RESULTS
    try:
        return max(1, min(int(num_results), _SEARCH_MEDIA_MAX_RESULTS))
    except (TypeError, ValueError):
        return _SEARCH_DEFAULT_RESULTS


async def _execute_web_search_uncached(
    modes: list[str],
    query_str: str,
    requested: int,
    page: int | None,
    image_url: str | None,
    gl: str | None,
    hl: str | None,
    tbs: str | None,
) -> str:
    """execute_web_search 的无缓存实现（入参已由调用方归一化）。"""
    needs_query = any(m in {"search", "images", "videos"} for m in modes)
    if needs_query and not query_str:
        return "❌ 搜索关键词为空。"
    if "lens" in modes and not (image_url or "").strip():
        return "❌ 以图搜图（lens）模式需要 image_url 参数。"

    # 各 mode 的结果数上限不同：search 多页聚合上限 50；images/videos/lens
    # 单请求上限 100（与 schema 声明及 serper 文档一致）。
    def _num_for(m: str) -> int:
        return min(requested, _SEARCH_MAX_RESULTS if m == "search" else _SEARCH_MEDIA_MAX_RESULTS)

    # search mode 为弥补黑名单过滤造成的缺口，按配置倍率向上游多取候选，
    # 过滤后再截断到请求数。
    def _upstream_num_for(m: str) -> int:
        n = _num_for(m)
        return candidate_result_count(n) if m == "search" else n

    # 单 mode 时走轻量路径；多 mode 时并发执行。
    if len(modes) == 1:
        single_mode = modes[0]
        try:
            items = await _serper_search_one_mode(
                single_mode,
                query=query_str or None,
                image_url=(image_url or "").strip() or None,
                num=_upstream_num_for(single_mode),
                page=page if single_mode == "search" else None,
                gl=gl,
                hl=hl,
                tbs=tbs,
            )
        except SerperUnavailableError as exc:
            logger.warning("Serper API 未配置: %s", exc)
            return exc.user_message("网页搜索服务")
        except SerperError as exc:
            logger.warning(
                "Serper API 调用失败 mode=%s category=%s status=%s retryable=%s: %s",
                single_mode, exc.category,
                exc.status_code if exc.status_code is not None else "unknown",
                exc.retryable, exc,
            )
            return exc.user_message("网页搜索服务")
        except SerperSearchTransientError as exc:
            logger.warning("Serper 未返回有效结果 mode=%s: %s", single_mode, exc)
            return "❌ 网页搜索服务暂未返回有效结果；请稍后重试。"
        except Exception:
            logger.exception("Serper 搜索发生未分类异常 mode=%s", single_mode)
            return "❌ 网页搜索服务发生未分类异常；请稍后重试。"

        # 仅 search mode 应用本地黑名单过滤；其他 mode 不涉及域名黑名单语义。
        if single_mode == "search" and items:
            items, filtered_count = _filter_blacklisted_search_results(items)
            items = items[:_num_for(single_mode)]
            if filtered_count:
                logger.info(
                    "web_search 已过滤 %s 条黑名单域名结果，domains=%s",
                    filtered_count, ", ".join(_BLACKLISTED_SEARCH_DOMAINS),
                )
            if items:
                return _format_search_results(items, query_str, "Serper / Google", requested=_num_for(single_mode))
            return f"❌ 未找到与「{query_str}」相关的结果。"
        if single_mode == "images" and items:
            return _format_image_results(items[:_num_for(single_mode)], query_str, requested=_num_for(single_mode))
        if single_mode == "videos" and items:
            return _format_video_results(items[:_num_for(single_mode)], query_str, requested=_num_for(single_mode))
        if single_mode == "lens" and items:
            return _format_lens_results(items[:_num_for(single_mode)], (image_url or "").strip(), requested=_num_for(single_mode))
        # 无结果
        if single_mode == "lens":
            return f"❌ 未找到与图片「{(image_url or '').strip()}」相关的结果。"
        return f"❌ 未找到与「{query_str}」相关的结果。"

    # 多 mode：并发执行，逐 mode 拼接结果
    async def _run_one(m: str) -> tuple[str, str | None, Exception | None]:
        try:
            items = await _serper_search_one_mode(
                m,
                query=query_str or None,
                image_url=(image_url or "").strip() or None,
                num=_upstream_num_for(m),
                page=page if m == "search" else None,
                gl=gl,
                hl=hl,
                tbs=tbs,
            )
            if m == "search" and items:
                items, filtered_count = _filter_blacklisted_search_results(items)
                if filtered_count:
                    logger.info(
                        "web_search 已过滤 %s 条黑名单域名结果，domains=%s",
                        filtered_count, ", ".join(_BLACKLISTED_SEARCH_DOMAINS),
                    )
            items = items[:_num_for(m)]
            if m == "search":
                text = _format_search_results(items, query_str, "Serper / Google", requested=_num_for(m)) if items else None
            elif m == "images":
                text = _format_image_results(items, query_str, requested=_num_for(m)) if items else None
            elif m == "videos":
                text = _format_video_results(items, query_str, requested=_num_for(m)) if items else None
            elif m == "lens":
                text = _format_lens_results(items, (image_url or "").strip(), requested=_num_for(m)) if items else None
            else:
                text = None
            return m, text, None
        except Exception as exc:
            logger.debug("_run_one 内部忽略的异常", exc_info=True)
            return m, None, exc

    outcomes = await asyncio.gather(*(_run_one(m) for m in modes))
    sections: list[str] = []
    errors: list[tuple[str, str]] = []
    for m, text, run_exc in outcomes:
        if run_exc is not None:
            if isinstance(run_exc, SerperError):
                errors.append((m, run_exc.user_message(f"{m} 搜索")))
            else:
                errors.append((m, f"❌ {m} 搜索失败：{run_exc}"))
            continue
        if text:
            sections.append(text)
        else:
            label = {
                "search": f"「{query_str}」",
                "images": f"「{query_str}」",
                "videos": f"「{query_str}」",
                "lens": f"「{(image_url or '').strip()}」",
            }.get(m, "")
            errors.append((m, f"❌ {m} 未找到与{label}相关的结果。"))

    if not sections and errors:
        # 全失败：返回第一个错误（上层不做自动重试，直接把失败原因透出给模型）
        _, first_msg = errors[0]
        logger.warning("web_search 全部 mode 失败 modes=%s first=%s", modes, first_msg)
        return first_msg

    body = "\n\n".join(sections)
    if errors:
        body += "\n\n" + "\n".join(msg for _, msg in errors)
    return body

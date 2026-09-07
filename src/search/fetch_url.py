"""fetch_url 工具：网页抓取、编码检测、SSRF 防护与重定向追踪（自 search_engine.py 拆出）。"""

import asyncio
import re
import time
import ipaddress
import socket
from typing import cast
from urllib.parse import urljoin, urlsplit

try:
    import trafilatura
    from trafilatura.settings import use_config
except Exception:  # pragma: no cover - optional dependency fallback
    trafilatura = None  # type: ignore[assignment]
    def use_config() -> None:  # type: ignore
        return None
try:
    from curl_cffi.requests import AsyncSession
except Exception:  # pragma: no cover - optional dependency fallback
    AsyncSession = None  # type: ignore
try:
    from lxml import html as lxml_html
except Exception:  # pragma: no cover - optional dependency fallback
    lxml_html = None

from token_budget import truncate_to_token_budget
from fetch_url_fallback import root_fallback_urls
from search.caches import (
    get_fetch_cache,
    set_fetch_cache,
)

import logging

logger = logging.getLogger(__name__)


FETCH_CONTENT_TOKEN_BUDGET = 20_000
FETCH_TITLE_TOKEN_BUDGET = 64
TRAFILATURA_TIMEOUT = 10
HTTP_TIMEOUT_SHORT = 10
CURL_TIMEOUT = 20

_TRAFILATURA_CONFIG = use_config()
if _TRAFILATURA_CONFIG is not None:
    try:
        _TRAFILATURA_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(TRAFILATURA_TIMEOUT))
    except Exception:
        logger.debug("module 内部忽略的异常", exc_info=True)
        pass
# ---------- 工具函数 ----------
def _truncate(text: str, token_budget: int = FETCH_CONTENT_TOKEN_BUDGET, suffix: str = "…（内容已按 token 预算截断）") -> str:
    return truncate_to_token_budget(text, token_budget, suffix=suffix)


def _get_title_from_html(html_content: str) -> str:
    if not html_content:
        return "无标题"
    try:
        tree = lxml_html.fromstring(html_content)
        title_elem = tree.find('.//title')
        if title_elem is not None and title_elem.text:
            title = title_elem.text.strip()
            return truncate_to_token_budget(title, FETCH_TITLE_TOKEN_BUDGET, suffix="…") if title else "无标题"
    except Exception:
        logger.debug("_get_title_from_html 内部忽略的异常", exc_info=True)
        pass
    return "无标题"
# --------------------- fetch_url (Telegram Rich HTML 输出) ---------------------

# 字符编码检测：优先级与 WHATWG / HTML5 规范对齐。
#   1. BOM (UTF-8-SIG / UTF-16-LE / UTF-16-BE)
#   2. HTTP Content-Type 头里的 charset
#   3. HTML <meta charset="..."> / <meta http-equiv="Content-Type" content="...; charset=...">
#   4. chardet/charset_normalizer（若已安装）作为兜底
#   5. UTF-8 with errors='replace'（最后防线）
#
# 这条路径之前直接用 response.text（curl_cffi 仅按 HTTP 头 charset 解码），
# 对于在 meta 标签里写 charset=gb2312 但 HTTP 头里没声明 charset 的网站
# （如 jxrb.jxwmw.cn），全部会得到 UTF-8 误码后的"馊字"，导致标题和正文
# 提取都失败。改成从 raw bytes 开始按上面优先级解码后，标题/正文恢复正确。
_BOM_TABLE = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
# 在头部 4KB 内扫这两条 meta 形式足够覆盖大多数中文站点。
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:]+)""",
    re.IGNORECASE,
)


def _detect_html_encoding(raw: bytes, http_encoding: str | None) -> str:
    """按 HTML5 规范的优先级返回最可能的字符集名称。

    raw 是 HTTP 响应体（未经 .text 转换的原始字节）。http_encoding 是
    curl_cffi 从 Content-Type 头解析出来的字符集（可能为 None / 空 / "None"）。
    """
    if not raw:
        return "utf-8"
    # 1) BOM
    for bom, enc in _BOM_TABLE:
        if raw.startswith(bom):
            return enc
    head = raw[:4096]
    # 2) HTTP 头里给的 charset（curl_cffi 会自动把 .encoding 设成这个）
    if http_encoding:
        enc = http_encoding.strip().lower()
        # 显式 ISO-8859-1 通常只是 curl_cffi 的兜底，不应优先于 meta
        if enc and enc not in {"iso-8859-1", "latin-1", "ascii"}:
            return _normalize_encoding_name(enc)
    # 3) HTML meta charset
    m = _META_CHARSET_RE.search(head)
    if m:
        enc = m.group(1).decode("ascii", errors="ignore").strip().lower()
        if enc:
            return _normalize_encoding_name(enc)
    # 4) chardet / charset_normalizer 兜底
    try:
        import chardet
        guess = chardet.detect(raw[:32768])
        if isinstance(guess, dict):
            enc = (guess.get("encoding") or "").strip().lower()
            conf = float(guess.get("confidence") or 0.0)
            if enc and conf >= 0.7:
                return _normalize_encoding_name(enc)
    except Exception:
        logger.debug("_detect_html_encoding 内部忽略的异常", exc_info=True)
        pass
    try:
        # charset_normalizer 是 requests / chardet 的常见替代品
        from charset_normalizer import from_bytes
        best = from_bytes(raw[:32768]).best()
        if best is not None:
            enc = (best.encoding or "").strip().lower()
            if enc:
                return _normalize_encoding_name(enc)
    except Exception:
        logger.debug("_detect_html_encoding 内部忽略的异常", exc_info=True)
        pass
    # 5) 最后防线：UTF-8 with errors='replace'
    return "utf-8"


def _normalize_encoding_name(name: str) -> str:
    """把 'gb2312' / 'gbk' / 'utf8' 等常见别名规范化为 Python codecs 认得的形式。"""
    if not name:
        return "utf-8"
    n = name.strip().lower().replace("_", "-")
    # gb_2312-80 / gb2312-80 / gb2312 → gbk（GBK 是 GB2312 的超集，更稳）
    if n in {"gb2312", "gb-2312", "gb_2312", "gb2312-80", "gb_2312-80", "chinese", "csiso58gb231280", "csgb2312"}:
        return "gbk"
    if n in {"utf8", "utf-8-8", "utf8-8"}:
        return "utf-8"
    if n == "utf-8-sig":
        return "utf-8-sig"
    if n in {"utf-16le", "utf_16_le"}:
        return "utf-16-le"
    if n in {"utf-16be", "utf_16_be"}:
        return "utf-16-be"
    return n


def _decode_html_bytes(raw: bytes | None, http_encoding: str | None) -> str | None:
    """把原始字节按检测出的编码安全解码为 str。"""
    if not raw:
        return None
    enc = _detect_html_encoding(raw, http_encoding)
    try:
        return raw.decode(enc, errors="replace")
    except (LookupError, TypeError):
        # 未知编码名 → 退回 UTF-8
        return raw.decode("utf-8", errors="replace")


async def _fetch_html_with_curl(url: str) -> str | None:
    try:
        async with AsyncSession() as session:
            response = await session.get(url, timeout=CURL_TIMEOUT, impersonate="chrome120",
                                         headers={"Accept": "text/html,application/xhtml+xml,*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            if response.status_code != 200:
                return None
            # 优先按 HTTP 头 + meta + chardet 检测的编码解码，避免 GBK 站点被
            # 错误地按 UTF-8 解析产生馊字标题。
            raw = response.content
            http_enc = getattr(response, "encoding", None)
            decoded = _decode_html_bytes(raw, http_enc)
            if decoded is not None:
                return decoded
            # 兜底：让 curl_cffi 自己用 .text（HTTP 头声明的编码）解码。
            return response.text
    except Exception as e:
        logger.error(f"curl_cffi 请求异常: {e}, URL: {url}")
        return None


async def _download_html_with_trafilatura(url: str) -> str | None:
    """curl_cffi 失败时用 trafilatura 自带下载器兜底获取原始 HTML。

    trafilatura.fetch_url 内部会按 HTML5 规范做编码检测（含 BOM / meta /
    chardet 兜底），因此对 GBK 站点不会出现馊字。返回值是已解码的 str。
    """
    if trafilatura is None:
        return None
    try:
        downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
        return downloaded or None
    except Exception as e:
        logger.debug(f"trafilatura 下载失败: {url}: {e}")
        return None


def _build_rich_fetch_payload(url: str, html: str) -> str | None:
    """把原始 HTML 转换为【返回给模型】的 Telegram Rich HTML（同步、CPU 密集）。

    提取链路（结果忠实于原页面文档顺序，媒体原位呈现，无聚合媒体区）：
      1. trafilatura XML（保留链接/图片/格式/表格及其顺序）→ Telegram HTML 块；
      2. DOM 文档序收集内嵌视频/iframe 播放器/音频/懒加载图片（带位置）；
      3. 锚定 + 原位插回正文流；轮播图 → <tg-slideshow>；预算内整块截断。
    注意：本函数的返回值只进入模型上下文；Telegram 工具 UI 的展示由
    tool_executors.format_tool_result 单独负责（保持历史简单样式）。
    返回 None 表示完全提不出内容（调用方继续走重定向检测/失败路径）。
    """
    if not html:
        return None
    try:
        from fetch_rich_content import (
            build_model_facing_html,
            build_fallback_text_from_html,
            extract_body_blocks,
            extract_title_from_html,
        )
    except Exception as e:
        logger.error(f"[fetch_url] fetch_rich_content 导入失败: {e}")
        return None

    title = extract_title_from_html(html)

    # 正文提取：trafilatura XML → Telegram HTML 块（含中文页面退化检测与
    # favor_precision/favor_recall 回退），链接/图片/格式/表格全保留。
    body_blocks = extract_body_blocks(html, url)
    body_len = sum(len(b) for b in body_blocks)

    fallback_text = ""
    if body_len < 200:
        # 结构化提取失败：纯文本兜底（meta 描述 + 段落），媒体仍会原位插入。
        fallback_text = build_fallback_text_from_html(html)
        if not fallback_text.strip():
            # 连兜底文本都没有：若 DOM 也完全没有媒体则直接失败；
            # 有媒体时仍交给 build_model_facing_html 产出媒体型结果。
            probe = build_model_facing_html(url, html, body_blocks=[], title=title)
            if not probe:
                return None

    result = build_model_facing_html(
        url, html, body_blocks=body_blocks, title=title, fallback_text=fallback_text,
    )
    if not result:
        return None
    # 最终防御：结果可见文本过短且无任何媒体时视为提取失败。
    visible = re.sub(r'<[^>]+>', '', result)
    has_media = bool(re.search(r'<(img|video|audio|tg-slideshow)\b', result))
    if len(re.sub(r'\s+', '', visible)) < 60 and not has_media:
        return None
    return result


# --------------------- SSRF 防护 ---------------------
_ALLOWED_FETCH_SCHEMES = {"http", "https"}


def _is_safe_url_to_fetch_sync(url: str) -> tuple[bool, str]:
    """
    SSRF 防护（同步部分）：URL 协议/格式校验 + IP 字面量校验。
    不做 DNS 解析。需要 DNS 解析的部分由 `_is_safe_url_to_fetch` 的 async 包装完成。
    """
    if not url or not isinstance(url, str):
        return False, "URL 为空"
    try:
        parts = urlsplit(url)
    except Exception as e:
        logger.debug("_is_safe_url_to_fetch_sync 内部忽略的异常", exc_info=True)
        return False, f"URL 解析失败: {e}"
    if parts.scheme.lower() not in _ALLOWED_FETCH_SCHEMES:
        return False, f"不支持的协议: {parts.scheme}"
    host = parts.hostname or ""
    if not host:
        return False, "URL 缺少主机名"
    # 如果 host 本身就是 IP 字面量，直接校验，无需 DNS 解析
    # （IP 禁用范围判定统一走 _check_ip_safe，避免谓词两处维护）
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # 不是 IP 字面量，是域名，DNS 解析交由 async 部分处理
        return True, ""
    return _check_ip_safe(host)


def _check_ip_safe(ip_str: str) -> tuple[bool, str]:
    """单个 IP 字符串的 SSRF 校验。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True, ""  # 不是 IP，跳过
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        return False, f"目标地址 {ip} 属于禁止访问的范围（私网/回环/链路本地等）"
    return True, ""


async def _is_safe_url_to_fetch(url: str) -> tuple[bool, str]:
    """
    SSRF 防护：先做同步部分（URL 协议/IP 字面量校验），再做异步 DNS 解析。
    注意：DNS 解析必须用 asyncio 的非阻塞版本，不能用同步 socket.getaddrinfo，
    否则恶意 LLM 高频调用 fetch_url 即可拖垮整个事件循环。
    """
    ok, reason = _is_safe_url_to_fetch_sync(url)
    if not ok:
        return False, reason
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host:
        return False, "URL 缺少主机名"
    # 如果是 IP 字面量，同步部分已校验，无需 DNS
    try:
        ipaddress.ip_address(host)
        return True, ""
    except ValueError:
        pass
    # 异步 DNS 解析，避免阻塞事件循环
    try:
        loop = asyncio.get_event_loop()
        infos = await loop.getaddrinfo(
            host, parts.port or 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        return False, f"DNS 解析失败: {e}"
    except Exception as e:
        logger.debug("_is_safe_url_to_fetch 内部忽略的异常", exc_info=True)
        return False, f"DNS 解析异常: {e}"
    for _family, _stype, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        # getaddrinfo 的 sockaddr[0] 对 AF_INET/AF_INET6 恒为 IP 字符串（typeshed 宽化为 str | int）
        ok, reason = _check_ip_safe(cast(str, ip_str))
        if not ok:
            return False, reason
    return True, ""


# 用于在 JS 拼接表达式里识别"host 类"变量并用真实 host 替换。
# 顺序很重要：先长后短，避免 `location.host` 被先匹配成 `location`。
_JS_HOST_IDENTIFIERS: tuple[str, ...] = (
    "window.location.host",
    "window.location.origin",
    "window.location.hostname",
    "window.location.href",
    "location.host",
    "location.origin",
    "location.hostname",
    "location.href",
    "wlOrigin",
    "self.location.host",
    "self.location.origin",
)

# 这些变量携带的只是查询串 / 哈希 / 路径本身，不包含跳转目标信息——
# 拼接时直接丢弃，否则会把当前 URL 的 query 拼进新 URL，污染目标。
_JS_DROP_IDENTIFIERS: tuple[str, ...] = (
    "window.location.search",
    "window.location.hash",
    "window.location.pathname",
    "location.search",
    "location.hash",
    "location.pathname",
)


def _normalize_url_for_compare(url: str) -> str:
    """规范化 URL 用于"是否重定向到自身"的对比。

    - scheme / netloc 一律小写
    - 末尾斜杠去掉
    - query / fragment 丢弃
    """
    try:
        parts = urlsplit(url)
    except (ValueError, TypeError):
        return (url or "").lower()
    scheme = (parts.scheme or "").lower()
    netloc = (parts.netloc or "").lower()
    path = (parts.path or "").rstrip("/")
    return f"{scheme}://{netloc}{path}"


def _extract_js_redirect_targets(html: str, current_url: str) -> list[str]:
    """从 HTML 的 JavaScript 中提取所有"有用"的跳转目标地址。

    与旧的 naive 正则 ``window\\.location\\.href\\s*=\\s*['"]...['"]`` 相比，
    本函数覆盖更通用的网页跳转写法：

    * 支持多种 location 别名前缀：``window.`` / ``document.`` / ``top.`` /
      ``parent.`` / ``self.`` / ``frames.``，以及无前缀的裸 ``location``；
    * 支持 ``location.href = ...``、``location.replace(...)``、
      ``location.assign(...)`` 以及裸 ``location = '...'`` 写法；
    * 字符串拼接表达式：``window.location.href = 'https://' + host + '/index/' + search``
      会被拆成多个字符串字面量并重新拼起来，host 类变量用真实 host 替换，
      search / hash / pathname 类变量直接丢弃；
    * 仅捕获到裸 scheme（如 ``https://``）或目标规范化后等于当前 URL 时，
      视为"无可用目标"跳过，让调用方继续尝试 Meta Refresh 与根路径回退，
      而不是返回 ``失败：页面重定向到自身`` 这种误导性错误。

    返回去重后的候选绝对 URL 列表（按文档出现顺序）。同一 if/else 中的
    多个分支会被全部收集，调用方按顺序尝试，第一个能成功抓取的即返回。
    """
    if not html:
        return []

    base_parts = urlsplit(current_url)
    base_scheme = (base_parts.scheme or "https").lower() or "https"
    base_host = base_parts.netloc

    # 匹配 (prefix可选) location (.href/.replace/.assign 可选) (= 或 () 后接 EXPR) ;|)
    # `(?!\w)` 防止把 `locationBar`、`locationHost` 等普通变量名误识别。
    pattern = re.compile(
        r"""(?:window\.|document\.|top\.|parent\.|self\.|frames\.)?"""
        r"""location(?:\.(?:href|replace|assign))?(?!\w)"""
        r"""\s*(?:=|\()\s*"""
        r"""(?P<expr>(?:[^;'"()]+|'[^']*'|"[^"]*")+?)"""
        r"""\s*(?:\)|;)""",
        re.IGNORECASE,
    )

    candidates: list[str] = []
    seen: set[str] = set()

    for m in pattern.finditer(html):
        expr = m.group("expr").strip()
        if not expr:
            continue

        # 在 expr 中顺序消费字符串字面量与已知标识符，跳过运算符 / 空白。
        target_parts: list[str] = []
        pos = 0
        n = len(expr)
        while pos < n:
            ch = expr[pos]
            if ch in ("'", '"'):
                end = expr.find(ch, pos + 1)
                if end == -1:
                    break
                target_parts.append(expr[pos + 1:end])
                pos = end + 1
                continue
            rest = expr[pos:]
            consumed = False
            # 先匹配要丢弃的标识符（避免被 host 列表里的更短名字误吞）
            for ident in _JS_DROP_IDENTIFIERS:
                if rest.startswith(ident):
                    pos += len(ident)
                    consumed = True
                    break
            if consumed:
                continue
            for ident in _JS_HOST_IDENTIFIERS:
                if rest.startswith(ident):
                    target_parts.append(base_host)
                    pos += len(ident)
                    consumed = True
                    break
            if consumed:
                continue
            # 跳过运算符 / 空白 / 未知标识符字符
            pos += 1

        joined = "".join(target_parts).strip()
        if not joined:
            continue

        # 裸 scheme（如 'https://'）——没有任何路径信息，直接跳过。
        if re.fullmatch(r"https?://", joined, re.IGNORECASE):
            continue

        # 计算候选绝对 URL
        if joined.startswith(("//", "/")):
            candidate = urljoin(current_url, joined)
        elif re.match(r"^https?://", joined, re.IGNORECASE):
            # 若形如 'https:///index/'（拼接丢了 host），补回 host。
            try:
                jp = urlsplit(joined)
            except ValueError:
                continue
            if not jp.netloc and jp.path:
                candidate = f"{base_scheme}://{base_host}{jp.path}"
            else:
                candidate = joined
        else:
            candidate = urljoin(current_url, joined)

        # 候选必须是 http(s) 且带 host；否则跳过。
        try:
            cp = urlsplit(candidate)
        except ValueError:
            continue
        if cp.scheme.lower() not in ("http", "https") or not cp.netloc:
            continue
        # 路径为空或仅 "/" 视为"无可用目标"（重定向到根自己）。
        if cp.path in ("", "/"):
            continue
        # 规范化后等于当前 URL 视为重定向到自身——交给回退处理。
        if _normalize_url_for_compare(candidate) == _normalize_url_for_compare(current_url):
            continue

        # 去重：规范化后相同的 URL 视为同一候选。
        norm = _normalize_url_for_compare(candidate)
        if norm in seen:
            continue
        seen.add(norm)
        candidates.append(candidate)

    return candidates


def _extract_meta_refresh_targets(html: str, current_url: str) -> list[str]:
    """从 ``<meta http-equiv="refresh" content="N; url=...">`` 提取跳转目标列表。

    与旧实现相比：

    * 当目标规范化后等于当前 URL 时，**跳过该候选**而不是让上层报
      "重定向到自身"——这样根路径回退仍有机会被尝试；
    * 同时支持引号包裹的 content 值（标准写法）与无引号的 content 值
      （非标准但实际页面常见）；
    * 多个 meta refresh 标签都会被收集，调用方按顺序尝试。
    """
    if not html:
        return []
    # 1) 引号包裹的标准写法：<meta http-equiv="refresh" content="0; url=/path">
    quoted = re.compile(
        r"""<meta\s+http-equiv\s*=\s*["']refresh["']"""
        r"""\s+content\s*=\s*["']\s*\d+\s*;\s*url\s*=\s*(?P<url>[^"']+)["']""",
        re.IGNORECASE,
    )
    # 2) 无引号写法：<meta http-equiv=refresh content="0; url=/path"> 或
    #    <meta http-equiv='refresh' content=0;url=/path>
    unquoted = re.compile(
        r"""<meta\s+http-equiv\s*=\s*["']?refresh["']?"""
        r"""\s+content\s*=\s*["']?\s*\d+\s*;\s*url\s*=\s*(?P<url>[^\s"'>]+)""",
        re.IGNORECASE,
    )

    candidates: list[str] = []
    seen: set[str] = set()
    for rgx in (quoted, unquoted):
        for m in rgx.finditer(html):
            target = (m.group("url") or "").strip()
            if not target:
                continue
            candidate = urljoin(current_url, target)
            try:
                cp = urlsplit(candidate)
            except ValueError:
                continue
            if cp.scheme.lower() not in ("http", "https") or not cp.netloc:
                continue
            if cp.path in ("", "/"):
                continue
            if _normalize_url_for_compare(candidate) == _normalize_url_for_compare(current_url):
                continue
            norm = _normalize_url_for_compare(candidate)
            if norm in seen:
                continue
            seen.add(norm)
            candidates.append(candidate)
    return candidates


async def _try_root_url_fallback(
    url: str,
    redirect_depth: int,
    start_time: float,
) -> str | None:
    """在根路径抓取失败后尝试配置的同站点首页路径。"""
    candidates = root_fallback_urls(url)
    for fallback_url in candidates:
        if time.monotonic() - start_time > 30:
            logger.warning("[fetch_url] 首页回退超出总超时：%s", url)
            break
        logger.info("[fetch_url] 根路径回退：%s -> %s", url, fallback_url)
        result = await execute_fetch_url(
            fallback_url,
            redirect_depth=redirect_depth + 1,
            start_time=start_time,
        )
        if not result.startswith("失败："):
            # 同时缓存原始根路径，后续相同请求不再重复经历失败链路。
            set_fetch_cache(url, result)
            return result
        logger.info("[fetch_url] 首页回退失败：%s", fallback_url)
    return None


async def execute_fetch_url(url: str, redirect_depth: int = 0, start_time: float | None = None) -> str:
    # 先检查缓存（避免 SSRF 校验浪费），再做 SSRF 校验
    cached = get_fetch_cache(url)
    if cached is not None:
        logger.debug(f"Fetch cache hit for {url}")
        return cached
    # SSRF 防护：先校验 URL（含异步 DNS 解析）
    ok, reason = await _is_safe_url_to_fetch(url)
    if not ok:
        logger.warning(f"fetch_url 拒绝不安全 URL: {url} ({reason})")
        return f"失败：拒绝抓取不安全的 URL：{reason}"

    if start_time is None:
        # 使用 time.monotonic 而非 asyncio.get_event_loop().time()：
        # 后者在 Python 3.10+ 没有运行 loop 时会发出 DeprecationWarning，
        # 且与 time.monotonic 不是同一个时钟。
        start_time = time.monotonic()
    # 总超时 30 秒
    if time.monotonic() - start_time > 30:
        result = f"失败：抓取超时（总时间 >30s）：{url}"
        return result

    if redirect_depth > 3:
        result = f"失败：重定向层次过深 (>{3})，已放弃：{url}"
        return result

    original_url = url

    # ---- 重试循环：最多尝试2次 ----
    for attempt in range(2):
        try:
            # 先用 curl_cffi 获取 HTML
            html = await _fetch_html_with_curl(url)
            if not html:
                # curl 失败：trafilatura 自带下载器兜底（拿到 HTML 后仍走富 HTML 提取）
                html = await _download_html_with_trafilatura(url)
            if not html:
                # 第一次尝试失败，等待后重试
                if attempt == 0:
                    logger.warning(f"fetch_url attempt {attempt+1} failed for {url}, retrying...")
                    await asyncio.sleep(1)
                    continue
                else:
                    fallback_result = await _try_root_url_fallback(
                        url, redirect_depth, start_time,
                    )
                    if fallback_result is not None:
                        return fallback_result
                    result = f"失败：无法获取页面内容：{url}"
                    return result

            # 获取标题（用于失败提示与展示兜底）
            title = _get_title_from_html(html)

            # 转 Telegram Rich HTML（CPU 密集，放线程池避免阻塞事件循环）。
            # 内容 + 内嵌视频/播放器/音频/图片 都在这一步提取。
            payload = await asyncio.to_thread(_build_rich_fetch_payload, url, html)
            if payload:
                set_fetch_cache(url, payload)
                return payload

            # ---- 检测 JavaScript 重定向（含字符串拼接表达式）----
            # 单字面量正则匹配 `window.location.href = '...'` 在遇到
            # `'https://' + host + '/index/' + search` 这种拼接时会捕获到
            # `https://`，urljoin 再把它解析回原 URL，误判为"重定向到自身"
            # 并直接报错——绕过下方根路径回退。因此把拼接表达式里的字面量
            # 与已知 host / search 变量分别处理，且当目标不可用时返回空列表
            # 让流程继续往下走 Meta Refresh 与根路径回退。
            # 同一 if/else 中的多个分支会被全部收集，按文档顺序尝试——
            # 这样移动端 / 桌面端不同路径的页面也能命中一个能抓取的候选。
            for js_target in _extract_js_redirect_targets(html, url):
                if time.monotonic() - start_time > 30:
                    logger.warning("[fetch_url] JS 候选超出总超时：%s", url)
                    break
                logger.info(f"[fetch_url] 跟随 JS 跳转: {original_url} -> {js_target}")
                js_result = await execute_fetch_url(
                    js_target, redirect_depth + 1, start_time,
                )
                if not js_result.startswith("失败："):
                    set_fetch_cache(url, js_result)
                    return js_result
                logger.info(
                    "[fetch_url] JS 跳转目标抓取失败，尝试下一候选：%s -> %s",
                    url, js_target,
                )

            # ---- 检测 Meta Refresh 重定向 ----
            for meta_target in _extract_meta_refresh_targets(html, url):
                if time.monotonic() - start_time > 30:
                    logger.warning("[fetch_url] Meta 候选超出总超时：%s", url)
                    break
                logger.info(f"[fetch_url] 跟随 Meta Refresh: {original_url} -> {meta_target}")
                meta_result = await execute_fetch_url(
                    meta_target, redirect_depth + 1, start_time,
                )
                if not meta_result.startswith("失败："):
                    set_fetch_cache(url, meta_result)
                    return meta_result
                logger.info(
                    "[fetch_url] Meta Refresh 目标抓取失败，尝试根路径回退：%s -> %s",
                    url, meta_target,
                )

            # 未提取到有效正文 / JS 与 Meta 跳转均不可用：仅根路径可继续
            # 尝试配置的同站点首页路径（如 splash page 的 `/index/`）。
            fallback_result = await _try_root_url_fallback(
                url, redirect_depth, start_time,
            )
            if fallback_result is not None:
                return fallback_result
            result = f"失败：无法提取有效正文（标题：{title}）\n🔗 {url}"
            return result

        except asyncio.TimeoutError:
            if attempt == 0:
                logger.warning(f"fetch_url timeout (attempt {attempt+1}) for {url}, retrying...")
                await asyncio.sleep(1)
                continue
            else:
                result = f"失败：抓取超时，请稍后重试：{url}"
                return result
        except Exception as e:
            logger.error(f"fetch_url unexpected error (attempt {attempt+1}): {e}")
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            else:
                result = f"失败：抓取异常，请稍后重试：{url}"
                return result

    # 如果循环结束仍未返回（理论上不会）
    result = f"失败：多次尝试均失败：{url}"
    return result

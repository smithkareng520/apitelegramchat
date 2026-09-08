# =====================================================================
# tests/unit/test_fetch_url_helpers.py — fetch_url 抓取层辅助逻辑
# =====================================================================
# 被测关键路径：search/fetch_url.py 的编码检测/URL 清洗/重定向提取/
# SSRF 同步校验/响应体大小上限，以及 search/caches.py 的缓存键归一化。
# 网络请求全部用假对象替身，不发真实请求。
# =====================================================================
import asyncio

import pytest

import search.fetch_url as fu
from search.caches import (
    _normalize_fetch_cache_key,
    get_fetch_cache,
    set_fetch_cache,
)
from search.fetch_url import (
    _check_ip_safe,
    _detect_html_encoding,
    _extract_js_redirect_targets,
    _extract_meta_refresh_targets,
    _fetch_html_with_curl,
    _is_safe_url_to_fetch_sync,
    _normalize_encoding_name,
    _normalize_url_for_compare,
    _read_response_capped,
)


# ---------------------------------------------------------------------
# 编码检测（中文站点 GBK 兼容的关键路径）
# ---------------------------------------------------------------------
def test_detect_encoding_bom_has_highest_priority():
    assert _detect_html_encoding(b"\xef\xbb\xbf<html>", None) == "utf-8-sig"
    assert _detect_html_encoding(b"\xff\xfe<html>", "utf-8") == "utf-16-le"


def test_detect_encoding_gbk_meta_beats_latin1_fallback():
    raw = '<html><head><meta charset="gb2312"></head><body>中文</body></html>'.encode("gbk")
    # iso-8859-1 是 curl_cffi 的常见兜底声明，不应优先于 meta
    assert _detect_html_encoding(raw, "iso-8859-1") == "gbk"
    assert _detect_html_encoding(raw, None) == "gbk"


def test_detect_encoding_http_header_wins_when_explicit():
    raw = "<html></html>".encode("utf-8")
    assert _detect_html_encoding(raw, "utf-8") == "utf-8"


def test_normalize_encoding_name_aliases():
    assert _normalize_encoding_name("gb2312") == "gbk"
    assert _normalize_encoding_name("gb2312-80") == "gbk"
    assert _normalize_encoding_name("utf8") == "utf-8"
    assert _normalize_encoding_name("UTF-16LE") == "utf-16-le"


# ---------------------------------------------------------------------
# SSRF 同步校验
# ---------------------------------------------------------------------
def test_is_safe_url_sync_rejects_private_and_bad_schemes():
    ok, _ = _is_safe_url_to_fetch_sync("http://127.0.0.1/admin")
    assert not ok
    ok, _ = _is_safe_url_to_fetch_sync("http://[::1]/x")
    assert not ok
    ok, _ = _is_safe_url_to_fetch_sync("http://10.0.0.5/x")
    assert not ok
    ok, _ = _is_safe_url_to_fetch_sync("ftp://example.com/x")
    assert not ok
    ok, _ = _is_safe_url_to_fetch_sync("javascript:alert(1)")
    assert not ok


def test_is_safe_url_sync_allows_public_https():
    ok, reason = _is_safe_url_to_fetch_sync("https://example.com/page")
    assert ok and reason == ""


def test_check_ip_safe_ranges():
    assert not _check_ip_safe("192.168.1.1")[0]
    assert not _check_ip_safe("169.254.169.254")[0]
    assert _check_ip_safe("93.184.216.34")[0]


# ---------------------------------------------------------------------
# 重定向目标提取
# ---------------------------------------------------------------------
def test_extract_js_redirect_targets_concatenation():
    html = "<script>window.location.href = 'https://' + location.host + '/index/home.html';</script>"
    targets = _extract_js_redirect_targets(html, "http://example.com/foo/bar")
    # 字面量自带完整 scheme（https://），保留之；host 变量替换为真实 host
    assert targets == ["https://example.com/index/home.html"]


def test_extract_js_redirect_targets_skips_self_and_root():
    html = "<script>location.href = '/';</script>"
    assert _extract_js_redirect_targets(html, "http://example.com/a") == []
    html2 = "<script>location.replace('http://example.com/a');</script>"
    assert _extract_js_redirect_targets(html2, "http://example.com/a") == []


def test_extract_meta_refresh_targets_quoted_and_unquoted():
    quoted = '<meta http-equiv="refresh" content="0; url=/index/home.html">'
    unquoted = "<meta http-equiv=refresh content=0;url=/index/home.html>"
    for html in (quoted, unquoted):
        targets = _extract_meta_refresh_targets(html, "http://example.com/")
        assert targets == ["http://example.com/index/home.html"]


def test_normalize_url_for_compare():
    assert (_normalize_url_for_compare("HTTP://Example.com/a/")
            == _normalize_url_for_compare("http://example.com/a"))
    assert _normalize_url_for_compare("http://e.com/a?q=1") == _normalize_url_for_compare("http://e.com/a")


# ---------------------------------------------------------------------
# 响应体大小上限与流式读取
# ---------------------------------------------------------------------
class _FakeStreamResponse:
    """模拟 curl_cffi 流式响应：aiter_content 分块产出。"""

    def __init__(self, chunks, status_code=200, headers=None):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    async def aiter_content(self, chunk_size=65536):
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


def test_read_response_capped_truncates_oversized_body():
    chunk = b"x" * 65536
    resp = _FakeStreamResponse([chunk] * 200)   # 共 12.8MB > 8MB 上限
    raw = asyncio.run(_read_response_capped(resp, fu.CONTENT_MAX_BYTES))
    assert len(raw) == fu.CONTENT_MAX_BYTES
    assert resp.closed is False                 # 截断读取本身不负责关闭


def test_read_response_capped_passes_small_body_through():
    resp = _FakeStreamResponse([b"<html></html>"])
    raw = asyncio.run(_read_response_capped(resp, fu.CONTENT_MAX_BYTES))
    assert raw == b"<html></html>"


class _FakeSession:
    """模拟 AsyncSession：记录请求参数并返回预置响应。"""

    last_kwargs = None

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        _FakeSession.last_kwargs = kwargs
        return self._response


def test_fetch_html_with_curl_returns_status_tuple(monkeypatch):
    html = "<html><head><title>t</title></head></html>".encode("utf-8")
    resp = _FakeStreamResponse([html], status_code=200,
                               headers={"Content-Type": "text/html; charset=utf-8"})
    monkeypatch.setattr(fu, "AsyncSession", lambda: _FakeSession(resp))
    text, status = asyncio.run(_fetch_html_with_curl("https://example.com/"))
    assert status == 200 and "title" in text
    # 重定向仍由 libcurl 自动跟随：请求不再显式禁用（保持既有跳转行为）
    assert _FakeSession.last_kwargs.get("stream") is True


def test_fetch_html_with_curl_reports_404_without_body(monkeypatch):
    resp = _FakeStreamResponse([b"not found"], status_code=404)
    monkeypatch.setattr(fu, "AsyncSession", lambda: _FakeSession(resp))
    text, status = asyncio.run(_fetch_html_with_curl("https://example.com/gone"))
    assert text is None and status == 404


def test_fetch_html_with_curl_aborts_when_content_length_exceeds_cap(monkeypatch):
    resp = _FakeStreamResponse([b"x"], status_code=200,
                               headers={"Content-Length": str(fu.CONTENT_MAX_BYTES * 2)})
    monkeypatch.setattr(fu, "AsyncSession", lambda: _FakeSession(resp))
    text, status = asyncio.run(_fetch_html_with_curl("https://example.com/huge"))
    assert text is None and status == 200


def test_fetch_html_with_curl_network_error_returns_none_status(monkeypatch):
    class _BoomSession:
        async def __aenter__(self):
            raise ConnectionError("dns fail")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(fu, "AsyncSession", _BoomSession)
    text, status = asyncio.run(_fetch_html_with_curl("https://down.example.com/"))
    assert text is None and status is None


# ---------------------------------------------------------------------
# 缓存键归一化 / 失败不缓存
# ---------------------------------------------------------------------
def test_normalize_fetch_cache_key_drops_fragment_and_tracking_params():
    base = "https://e.com/a?x=1"
    key = _normalize_fetch_cache_key(base + "&utm_source=wechat&fbclid=abc#top")
    assert key == "https://e.com/a?x=1"
    assert _normalize_fetch_cache_key("https://e.com/a#frag") == "https://e.com/a"


def test_fetch_cache_rejects_failure_payloads():
    url = "https://e.com/fail-case"
    set_fetch_cache(url, "失败：无法获取页面内容")
    assert get_fetch_cache(url) is None
    set_fetch_cache(url, "<h3>ok</h3>")
    assert get_fetch_cache(url) == "<h3>ok</h3>"
    assert get_fetch_cache(url + "#other") == "<h3>ok</h3>"   # fragment 归一化命中

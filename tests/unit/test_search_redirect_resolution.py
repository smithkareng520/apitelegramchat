"""HTTP search-result redirect resolution: safety and URL normalization."""
import asyncio

import sys
import types

import search.fetch_url as fetch_url
import search.serper as serper


class FakeResponse:
    def __init__(self, status, url, headers=None):
        self.status_code = status
        self.url = url
        self.headers = headers or {}
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _install_fake_curl(monkeypatch, session):
    requests_module = types.ModuleType("curl_cffi.requests")
    requests_module.AsyncSession = lambda: session
    package_module = types.ModuleType("curl_cffi")
    package_module.requests = requests_module
    monkeypatch.setitem(sys.modules, "curl_cffi", package_module)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", requests_module)


def test_resolve_result_url_follows_relative_redirect_without_auto_follow(monkeypatch):
    first = FakeResponse(302, "https://search.example/go", {"Location": "/article"})
    second = FakeResponse(200, "https://site.example/article")
    session = FakeSession([first, second])
    _install_fake_curl(monkeypatch, session)

    async def safe(_url):
        return True, ""

    monkeypatch.setattr(fetch_url, "_is_safe_url_to_fetch", safe)
    result = asyncio.run(serper._resolve_result_url("https://search.example/go"))

    assert result == "https://site.example/article"
    assert [call[0] for call in session.calls] == [
        "https://search.example/go", "https://search.example/article"
    ]
    assert all(call[1].get("allow_redirects") is False for call in session.calls)
    assert first.closed and second.closed


def test_resolve_result_url_checks_redirect_target_before_request(monkeypatch):
    response = FakeResponse(302, "https://public.example/go", {"Location": "http://127.0.0.1/admin"})
    session = FakeSession([response])
    _install_fake_curl(monkeypatch, session)

    async def safe(url):
        return ("127.0.0.1" not in url), "unsafe" if "127.0.0.1" in url else ""

    monkeypatch.setattr(fetch_url, "_is_safe_url_to_fetch", safe)
    result = asyncio.run(serper._resolve_result_url("https://public.example/go"))

    assert result == "https://public.example/go"
    assert len(session.calls) == 1
    assert response.closed

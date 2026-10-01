"""流式响应健壮性回归：Gemini 长 SSE 行、Gemini 不因 total 超时被误杀、Anthropic 空闲超时可重试。"""
import asyncio
import json

import aiohttp
import pytest
from aiohttp import web

from ai._constants import STREAM_CLIENT_TIMEOUT, STREAM_READ_BUFSIZE
from ai.anthropic_bridge import _is_retryable_stream_error
from ai.gemini_bridge import _iter_gemini_stream_events
from ai.streaming import AIStreamTimeoutError


def _sse_app(payload: dict) -> web.Application:
    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
        return resp

    app = web.Application()
    app.router.add_get("/", handler)
    return app


@pytest.mark.asyncio
async def test_gemini_sse_line_over_128kib_is_parsed():
    payload = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "text_editor", "args": {"file_text": "x" * 300_000}}}]},
        "finishReason": "STOP"}]}
    runner = web.AppRunner(_sse_app(payload))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession(
            timeout=STREAM_CLIENT_TIMEOUT, read_bufsize=STREAM_READ_BUFSIZE
        ) as session:
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                events = [e async for e in _iter_gemini_stream_events(resp)]
    finally:
        await runner.cleanup()
    call = next(e for e in events if e["kind"] == "function_call")
    assert len(call["args"]["file_text"]) == 300_000


def test_gemini_stream_timeout_has_no_total_cap():
    assert STREAM_CLIENT_TIMEOUT.total is None
    assert STREAM_CLIENT_TIMEOUT.sock_read and STREAM_CLIENT_TIMEOUT.sock_read > 0


def test_anthropic_retries_application_idle_timeout():
    assert _is_retryable_stream_error(AIStreamTimeoutError("idle"))
    assert not _is_retryable_stream_error(asyncio.CancelledError())
    assert not _is_retryable_stream_error(ValueError("boom"))


def test_first_choice_reports_gateway_error():
    from types import SimpleNamespace
    from ai.errors import AIResponseParseError, first_choice

    assert first_choice(SimpleNamespace(choices=["a"])) == "a"
    with pytest.raises(AIResponseParseError, match="content_filter"):
        first_choice(SimpleNamespace(choices=None, error="content_filter"), label="x")
    with pytest.raises(AIResponseParseError):
        first_choice(SimpleNamespace(choices=[]))

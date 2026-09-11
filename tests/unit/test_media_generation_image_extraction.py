import base64
import asyncio

from ai.media_generation import (
    _extract_image_items,
    _response_items_to_bytes,
    _detect_valid_image,
    _sniff_payload_kind,
    _request_openai_images_task,
    _request_chat_modalities_image_task,
)
from core.images import ImageTask, ImageTaskResult


# 1x1 transparent PNG.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_extract_image_items_does_not_scan_arbitrary_urls():
    payload = {
        "data": [{"url": "https://cdn.example/image.png"}],
        "fallback": {"url": "https://fallback.example/not-an-image.png"},
        "metadata": {"output_url": "https://example.com/debug.png"},
    }
    items = _extract_image_items(payload, max_items=1)
    assert len(items) == 1
    assert items[0]["image_url"]["url"] == "https://cdn.example/image.png"


def test_extract_image_items_accepts_chat_message_images_but_not_text_urls():
    payload = {
        "choices": [{
            "message": {
                "content": "See https://example.com/fake.png",
                "images": [{"type": "image_url", "image_url": {"url": "https://cdn.example/real.png"}}],
            }
        }]
    }
    items = _extract_image_items(payload, max_items=1)
    assert len(items) == 1
    assert items[0]["image_url"]["url"] == "https://cdn.example/real.png"


def test_detect_valid_image_rejects_html():
    assert _detect_valid_image(b"<html><body>fallback</body></html>") is None
    assert _detect_valid_image(PNG_1X1) == ("image/png", "png")


def test_response_items_to_bytes_rejects_non_image_base64():
    html_b64 = base64.b64encode(b"<html>fallback</html>").decode("ascii")
    payload = {"data": [{"b64_json": html_b64}]}
    images, diagnostics = asyncio.run(_response_items_to_bytes(payload, max_images=1))
    assert images == []
    # 拒绝必须留下诊断：base64 内容非图片（HTML 错误页）
    assert len(diagnostics) == 1
    assert "base64" in diagnostics[0]
    assert "HTML" in diagnostics[0]


def test_response_items_to_bytes_accepts_one_real_image_and_respects_limit():
    image_b64 = base64.b64encode(PNG_1X1).decode("ascii")
    payload = {"data": [{"b64_json": image_b64}, {"b64_json": image_b64}]}
    images, diagnostics = asyncio.run(_response_items_to_bytes(payload, max_images=1))
    assert len(images) == 1
    assert images[0] == PNG_1X1
    assert diagnostics == []      # 全部成功时无诊断


# ---------------------------------------------------------------------------
# 回归测试（2026-09 ModelScope 生产事故）：中转商返回 HTTP 200 + 图片 URL，
# 但 URL 下载下来是防盗链错误页（7627 字节 HTML）。旧代码静默拒绝后上层
# 只会报“接口返回成功，但未找到可用图片数据”——语义完全失真。
# 诊断必须逐项记录真实原因并透传给用户报错。
# ---------------------------------------------------------------------------

class _FakeDownloadSession:
    """按 {status, headers, body} 脚本返回响应的最小 aiohttp 会话替身。"""

    def __init__(self, script):
        self._script = script

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        status, headers, body = self._script(url)
        return _FakeDownloadResponse(status, headers, body)


class _FakeDownloadResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers

        class _Content:
            def __init__(self, body):
                self._body = body
                self._consumed = False

            async def readany(self):
                # 模拟 aiohttp StreamReader.readany：数据一次性返回，
                # 之后返回空字节表示 EOF
                if self._consumed:
                    return b""
                self._consumed = True
                return self._body

        self.content = _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_response_items_to_bytes_diagnoses_non_image_url(monkeypatch):
    """声称 image/png 的 URL 下载到 HTML 错误页：拒绝 + 诊断带 host 与内容形态。"""
    import ai.media_generation as mg

    anti_leech = b"<!DOCTYPE html><html><body>Request rejected (anti-leech)</body></html>" * 40

    def script(url):
        return 200, {"Content-Type": "image/png"}, anti_leech

    monkeypatch.setattr(mg.aiohttp, "ClientSession", lambda **k: _FakeDownloadSession(script))

    payload = {"data": [{"url":
        "https://modelscope-studios.oss-cn-zhangjiakou.aliyuncs.com/aigc/text-to-image/abc.png"}]}
    images, diagnostics = asyncio.run(_response_items_to_bytes(payload, max_images=1))
    assert images == []
    assert len(diagnostics) == 1
    # host 定位 + 字节数 + 内容形态（不带带签名参数的完整 URL）
    assert "modelscope-studios.oss-cn-zhangjiakou.aliyuncs.com" in diagnostics[0]
    assert "字节" in diagnostics[0]
    assert "HTML" in diagnostics[0]


def test_response_items_to_bytes_diagnoses_download_failure(monkeypatch):
    """图片 URL 下载 HTTP 403：诊断说明下载失败与状态码。"""
    import ai.media_generation as mg

    def script(url):
        return 403, {"Content-Type": "text/plain"}, b"forbidden"

    monkeypatch.setattr(mg.aiohttp, "ClientSession", lambda **k: _FakeDownloadSession(script))

    payload = {"data": [{"url": "https://cdn.example.com/expired.png"}]}
    images, diagnostics = asyncio.run(_response_items_to_bytes(payload, max_images=1))
    assert images == []
    assert len(diagnostics) == 1
    assert "cdn.example.com" in diagnostics[0]
    assert "403" in diagnostics[0]


def test_response_items_to_bytes_accepts_real_image_url(monkeypatch):
    """URL 下载到真实 PNG：正常入库且无诊断。"""
    import ai.media_generation as mg

    def script(url):
        return 200, {"Content-Type": "image/png"}, PNG_1X1

    monkeypatch.setattr(mg.aiohttp, "ClientSession", lambda **k: _FakeDownloadSession(script))

    payload = {"data": [{"url": "https://cdn.example.com/ok.png"}]}
    images, diagnostics = asyncio.run(_response_items_to_bytes(payload, max_images=1))
    assert images == [PNG_1X1]
    assert diagnostics == []


def test_response_items_to_bytes_reads_chunked_image_fully(monkeypatch):
    """回归（2026-09 生产事故，85451 字节 PNG 被误拒）：

    resp.content.read(n) 语义是"最多读 n 字节"——分块传输时立即返回
    buffer 中已到达的部分数据，大图被读成半张（缺 IEND 尾部）→ PIL
    verify 判"损坏或截断"误拒（浏览器打开同一 URL 完全正常）。
    修复后必须循环 readany() 读到 EOF，分块到达也要拼出完整字节。
    """
    import io

    from PIL import Image

    import ai.media_generation as mg

    buf = io.BytesIO()
    Image.new("RGB", (400, 400), (120, 30, 200)).save(buf, "PNG")
    body = buf.getvalue()
    assert len(body) > 256      # 确保会被切成多块

    class _ChunkedContent:
        def __init__(self, data, size=64):
            self._chunks = [data[i:i + size] for i in range(0, len(data), size)]
            self._i = 0

        async def readany(self):
            if self._i >= len(self._chunks):
                return b""      # EOF
            chunk = self._chunks[self._i]
            self._i += 1
            return chunk

    class _Resp:
        status = 200
        headers = {"Content-Type": "image/png"}
        content = _ChunkedContent(body)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            return _Resp()

    monkeypatch.setattr(mg.aiohttp, "ClientSession", lambda **k: _Session())

    payload = {"data": [{"url": "https://cdn.example.com/chunked.png"}]}
    images, diagnostics = asyncio.run(_response_items_to_bytes(payload, max_images=1))
    assert diagnostics == []
    assert images == [body]     # 完整字节，一字不少


def test_sniff_payload_kind_reports_content_shape():
    """非图片字节流的内容嗅探：能一眼看出错误页/XML/JSON/截断等形态。"""
    f = _sniff_payload_kind
    assert "HTML" in f(b"<!DOCTYPE html><html><body>x</body></html>")
    assert "XML" in f(b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>')
    assert "JSON" in f(b'{"error": {"message": "quota exceeded"}}')
    assert "PDF" in f(b"%PDF-1.7 rest")
    assert "GZIP" in f(b"\x1f\x8b\x08\x00payload")
    assert "损坏" in f(b"\x89PNG\r\n\x1a\n" + b"truncated-garbage")
    assert "纯文本" in f(b"hello world, this is plain text response")
    assert "未知二进制" in f(b"\x00\x01\x02\xfe\xff\x07")
    assert "空内容" in f(b"")


def test_openai_images_task_carries_diagnostics(monkeypatch):
    """响应解析层的拒绝诊断必须透传到 ImageTaskResult（供上层报错）。"""

    async def fake_request(*args, **kwargs):
        return {"data": [{"url": "https://host.example/x.png"}]}, "/images/generations", "", 200, "req"

    async def fake_to_bytes(response_json, max_images=1):
        return [], ["图片 #1（host.example）：链接下载了 7627 字节，但内容不是有效图片（HTML 页面）"]

    monkeypatch.setattr("ai.media_generation._request_images_generations", fake_request)
    monkeypatch.setattr("ai.media_generation._response_items_to_bytes", fake_to_bytes)

    task = ImageTask.generate("test", model="google/gemini-3-pro-image-preview")
    result = asyncio.run(_request_openai_images_task(task))
    assert result.images == []
    assert len(result.diagnostics) == 1
    assert "host.example" in result.diagnostics[0]
    # 无诊断路径默认空列表（向后兼容旧构造）
    assert ImageTaskResult().diagnostics == []

def test_openai_images_task_resolves_registered_model_without_name_error(monkeypatch):
    async def fake_request(*args, **kwargs):
        return {"data": []}, "/images/generations", "", 200, "req-test"

    monkeypatch.setattr("ai.media_generation._request_images_generations", fake_request)
    task = ImageTask.generate("test", model="google/gemini-3-pro-image-preview")
    result = asyncio.run(_request_openai_images_task(task))
    assert result.images == []
    assert result.diagnostics == []   # 空响应是“无图片数据”，不是下载校验失败
    assert result.endpoint == "/v1/images/generations"


def test_openrouter_chat_image_task_resolves_registered_model_without_name_error(monkeypatch):
    class FakeCompletions:
        async def create(self, **kwargs):
            class FakeMessage:
                content = ""
                images = []

                def model_dump(self):
                    return {"content": "", "images": []}

            class FakeChoice:
                finish_reason = "stop"
                message = FakeMessage()

            class FakeResponse:
                choices = [FakeChoice()]
                usage = None

            return FakeResponse()

    class FakeClient:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr("api_client.api_client.get_client_for_model", lambda model_info: FakeClient())
    monkeypatch.setattr("ai.media_generation.get_sampling_params", lambda model_info: {})

    task = ImageTask.generate("test", model="google/gemini-3-pro-image-preview")
    result = asyncio.run(_request_chat_modalities_image_task(task))
    assert result.images == []
    assert result.endpoint == "/chat/completions"


# ---------------------------------------------------------------------------
# 回归测试（2026-09-08 生产事故）：/images/edits 失败后绝不能回退
# /images/generations —— 中转站会忽略非官方 image 字段，把编辑请求当
# 纯文生图执行，HTTP 200 "假成功"但产出一张与原图无关的新图。
# ---------------------------------------------------------------------------

def test_openai_compat_edit_never_falls_back_to_generations(monkeypatch):
    """编辑端点失败时绝不能伪装成文生图成功。"""
    from types import SimpleNamespace
    import ai.media_generation as mg

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    calls = []

    async def fake_image_urls_to_data_urls(session, image_urls):
        return [
            "data:image/png;base64,"
            + base64.b64encode(PNG_1X1).decode("ascii")
        ]

    async def fake_post(session, url, **kwargs):
        calls.append(url)
        if url.endswith("/images/edits"):
            return (
                None,
                "/images/edits",
                "page not found",
                404,
                "req-edit-404",
            )
        raise AssertionError(
            "edit failure must never call /images/generations"
        )

    monkeypatch.setattr(mg.aiohttp, "ClientSession", lambda **kwargs: FakeSession())
    monkeypatch.setattr(mg, "_resolve_provider_api_key", lambda env: "test-key")
    monkeypatch.setattr(mg, "_image_urls_to_data_urls", fake_image_urls_to_data_urls)
    monkeypatch.setattr(mg, "_post_images_with_retry", fake_post)
    monkeypatch.setattr(mg, "_data_url_to_bytes", lambda _: (PNG_1X1, "image/png"))

    model_info = SimpleNamespace(provider="xxtf", name="XXTF")

    result = asyncio.run(
        mg._request_openai_compat_image(
            model_info,
            prompt="remove pedestrians and keep everything else unchanged",
            image_urls=["https://example.test/original.png"],
            num_images=1,
            model="gpt-image-2",
            aspect_ratio="1:1",
        )
    )

    response, endpoint, detail, status, request_id = result

    assert response is None
    assert endpoint == "/images/edits"
    assert status == 404
    assert request_id == "req-edit-404"
    # 明确告诉模型/用户：不会降级成文生图。
    assert "不会回退" in detail
    # 只打过 /images/edits 一趟，绝不出现第二次 generations 请求。
    assert calls == ["https://xxtf.baby/v1/images/edits"]


def test_image_task_edit_requires_reference_image():
    """edit/variation 没有参考图必须直接失败，而不是偷偷变成文生图。"""
    try:
        ImageTask.edit("edit this", [], model="gpt-image-2")
    except ValueError as exc:
        assert "至少需要一张参考图" in str(exc)
    else:
        raise AssertionError(
            "ImageTask.edit must reject empty reference images"
        )


def test_image_urls_to_data_urls_skips_html_payload():
    """参考 URL 返回 HTML 错误页时必须跳过，绝不伪装成参考图送进请求。"""
    import ai.media_generation as mg

    class FakeResp:
        status = 200
        headers = {"Content-Type": "text/html"}

        async def read(self):
            return b"<html><body>error page</body></html>"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def get(self, url, **kwargs):
            return FakeResp()

    result = asyncio.run(
        mg._image_urls_to_data_urls(FakeSession(), ["https://example.test/broken.png"])
    )
    assert result == []

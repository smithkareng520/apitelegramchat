import base64
import asyncio

from ai.media_generation import (
    _extract_image_items,
    _response_items_to_bytes,
    _detect_valid_image,
    _request_openai_images_task,
    _request_chat_modalities_image_task,
)
from core.images import ImageTask


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
    assert asyncio.run(_response_items_to_bytes(payload, max_images=1)) == []


def test_response_items_to_bytes_accepts_one_real_image_and_respects_limit():
    image_b64 = base64.b64encode(PNG_1X1).decode("ascii")
    payload = {"data": [{"b64_json": image_b64}, {"b64_json": image_b64}]}
    result = asyncio.run(_response_items_to_bytes(payload, max_images=1))
    assert len(result) == 1
    assert result[0] == PNG_1X1

def test_validate_image_bytes_is_legacy_exported_from_ai_handlers():
    # Avoid importing the whole application in this focused unit test (it pulls
    # optional runtime dependencies such as tiktoken). Verify the compatibility
    # re-export directly from the module source.
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / "src" / "ai_handlers.py").read_text()
    assert "    _validate_image_bytes," in source


def test_openai_images_task_resolves_registered_model_without_name_error(monkeypatch):
    async def fake_request(*args, **kwargs):
        return {"data": []}, "/images/generations", "", 200, "req-test"

    monkeypatch.setattr("ai.media_generation._request_images_generations", fake_request)
    task = ImageTask.generate("test", model="google/gemini-3-pro-image-preview")
    result = asyncio.run(_request_openai_images_task(task))
    assert result.images == []
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

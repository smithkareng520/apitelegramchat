import base64
import asyncio

from ai.media_generation import _extract_image_items, _response_items_to_bytes, _detect_valid_image


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

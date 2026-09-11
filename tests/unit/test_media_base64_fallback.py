"""base64 内联通用兜底回归测试（媒体拉取 400 的第三次尝试）。

背景（2026-09-11 生产 [3a64f5cd] / [5332ea8f]）：agnes-3.0-flash 等网关
每次请求都要自行下载消息历史里的媒体 URL（R2 预签名地址）；R2 慢窗口
会连续覆盖首次请求与 1.5s 后的 URL 重放，双双 400 后整轮报废。Agnes
图像文档明确输入图像支持 Data URI Base64（chat 的 image_url 同形状，
内部 ImageBlock 在 R2 不可用时早已走 data:image/...;base64,... 形状），
故重放梯子升级为：

    尝试 0（URL）→ 失败 → 原样 URL 重放（1.5s，抖动自愈）
      → 再失败 → _inline_wire_images_as_data_urls 把 http(s) 图片全部
                 内联为 data URI 后重放（通用兜底，网关不再访问 R2）

覆盖：
- _sniff_image_mime：magic bytes 嗅探；
- _media_fetch_replay_mode：重放形态梯子（url → inline）；
- _inline_wire_images_as_data_urls：part 收集范围（image_url 独占；
  video_url/file/text 不碰；data: 天然跳过）、MIME 判定四级回退、
  下载失败/空字节的"部分兜底"语义、detail 字段保留、原地替换。

_agentic_loop_openai_compat 重放分支的闸门组合已由
test_media_fetch_retry.py 与本文件的形态判定共同覆盖。
"""
import asyncio
import base64

import pytest

from ai.agentic_loops import (
    _MEDIA_FETCH_MAX_REPLAYS,
    _inline_wire_images_as_data_urls,
    _media_fetch_replay_mode,
    _should_retry_media_fetch_400,
    _sniff_image_mime,
)


PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def _png_data_url(data: bytes = PNG_MAGIC, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _wire_user(parts: list) -> dict:
    return {"role": "user", "content": parts}


def _image_part(url: str, detail: str | None = "high") -> dict:
    inner: dict = {"url": url}
    if detail:
        inner["detail"] = detail
    return {"type": "image_url", "image_url": inner}


def _fetch_ok(data: bytes, content_type: str = ""):
    async def _fetch(url: str):
        return data, content_type
    return _fetch


def _fetch_fail(exc: Exception | None = None):
    async def _fetch(url: str):
        raise exc or RuntimeError("download boom")
    return _fetch


class TestSniffImageMime:
    def test_jpeg(self):
        assert _sniff_image_mime(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "image/jpeg"

    def test_png(self):
        assert _sniff_image_mime(PNG_MAGIC) == "image/png"

    @pytest.mark.parametrize("sig", [b"GIF87a", b"GIF89a"])
    def test_gif(self, sig):
        assert _sniff_image_mime(sig + b"\x00" * 8) == "image/gif"

    def test_webp(self):
        assert _sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"

    def test_bmp(self):
        assert _sniff_image_mime(b"BM\x00\x00") == "image/bmp"

    def test_unknown_returns_empty(self):
        assert _sniff_image_mime(b"\x00\x01\x02\x03") == ""
        assert _sniff_image_mime(b"") == ""


class TestReplayModeLadder:
    def test_first_replay_is_url(self):
        # 首次失败：原样 URL 重放（抖动自愈；请求体不变，幂等性最强）
        assert _media_fetch_replay_mode(0) == "url"

    def test_second_replay_is_inline(self):
        # 第二次失败：base64 内联通用兜底（[5332ea8f] 实证同一 R2 慢窗口
        # 会连续击落 URL 重放，原样重试无效）
        assert _media_fetch_replay_mode(1) == "inline"

    def test_ladder_total_attempts_unchanged(self):
        # 梯子不扩大重试总量：仍是 1 + 2 次请求
        assert _MEDIA_FETCH_MAX_REPLAYS == 2

    def test_gate_blocks_third_attempt(self):
        from tests.unit.test_media_fetch_retry import PRODUCTION_ERROR_TEXT
        exc = RuntimeError(PRODUCTION_ERROR_TEXT)
        # 第三次尝试（stream_attempt=2）无论什么形态都不再放行
        assert not _should_retry_media_fetch_400(exc, received_any=False, stream_attempt=2)


class TestInlineWireImages:
    def test_http_image_replaced_with_data_url(self):
        part = _image_part("https://r2.example/photo.jpg?X-Amz-Signature=abc")
        wire = [_wire_user([{"type": "text", "text": "看这张图"}, part])]
        inlined, failed = asyncio.run(_inline_wire_images_as_data_urls(
            wire, _fetch=_fetch_ok(PNG_MAGIC, "application/octet-stream")))
        assert (inlined, failed) == (1, 0)
        # magic sniff 优先于 Content-Type（octet-stream 未被采用）
        assert part["image_url"]["url"] == _png_data_url()
        # detail 字段原样保留
        assert part["image_url"]["detail"] == "high"

    def test_data_uri_parts_untouched_and_uncounted(self):
        data_url = _png_data_url()
        part = _image_part(data_url)
        wire = [_wire_user([part])]
        inlined, failed = asyncio.run(_inline_wire_images_as_data_urls(
            wire, _fetch=_fetch_ok(PNG_MAGIC)))
        assert (inlined, failed) == (0, 0)
        assert part["image_url"]["url"] == data_url

    def test_video_and_file_parts_not_inlined(self):
        video_part = {"type": "video_url", "video_url": {"url": "https://r2.example/v.mp4"}}
        file_part = {"type": "file", "file": {"filename": "a.pdf", "file_data": "https://r2.example/a.pdf"}}
        wire = [_wire_user([video_part, file_part])]
        inlined, failed = asyncio.run(_inline_wire_images_as_data_urls(
            wire, _fetch=_fetch_ok(PNG_MAGIC)))
        # 视频可达数十 MB，内联会让请求体爆炸——明确不碰，也不计数
        assert (inlined, failed) == (0, 0)
        assert video_part["video_url"]["url"] == "https://r2.example/v.mp4"
        assert file_part["file"]["file_data"] == "https://r2.example/a.pdf"

    def test_failed_download_keeps_url_partial_fallback(self):
        part_a = _image_part("https://r2.example/a.jpg")
        part_b = _image_part("https://r2.example/b.jpg")
        calls: list[str] = []

        async def _fetch(url: str):
            calls.append(url)
            if "a.jpg" in url:
                raise RuntimeError("boom")
            return JPEG_MAGIC, ""

        wire = [_wire_user([part_a, part_b])]
        inlined, failed = asyncio.run(_inline_wire_images_as_data_urls(wire, _fetch=_fetch))
        assert (inlined, failed) == (1, 1)
        # 失败者保持 URL 原样（部分兜底：能内联几张是几张）
        assert part_a["image_url"]["url"] == "https://r2.example/a.jpg"
        assert part_b["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_empty_bytes_keeps_url(self):
        part = _image_part("https://r2.example/x.jpg")
        wire = [_wire_user([part])]
        inlined, failed = asyncio.run(_inline_wire_images_as_data_urls(
            wire, _fetch=_fetch_ok(b"")))
        assert (inlined, failed) == (0, 1)
        assert part["image_url"]["url"] == "https://r2.example/x.jpg"

    def test_mime_fallback_content_type_then_ext_then_default(self):
        # magic 认不出 + Content-Type 非 image/* → URL 扩展名或 jpeg 兑底：
        # .bin 不在扩展名表 → jpeg 兑底；.PNG（大小写不敏感）→ image/png
        part_bin = _image_part("https://r2.example/a.bin")
        part_ext = _image_part("https://r2.example/photo.PNG?sig=1")
        part_default = _image_part("https://r2.example/noext")
        wire = [_wire_user([part_bin, part_ext, part_default])]

        async def _fetch(url: str):
            return b"\x00\x01\x02\x03", "application/octet-stream"

        asyncio.run(_inline_wire_images_as_data_urls(wire, _fetch=_fetch))
        assert part_bin["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert part_ext["image_url"]["url"].startswith("data:image/png;base64,")
        assert part_default["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_mime_from_image_content_type(self):
        part = _image_part("https://r2.example/a.bin")
        wire = [_wire_user([part])]

        async def _fetch(url: str):
            return b"\x00\x01\x02", "image/avif"

        asyncio.run(_inline_wire_images_as_data_urls(wire, _fetch=_fetch))
        assert part["image_url"]["url"].startswith("data:image/avif;base64,")

    def test_content_type_charset_parameter_stripped(self):
        part = _image_part("https://r2.example/a.bin")
        wire = [_wire_user([part])]
        asyncio.run(_inline_wire_images_as_data_urls(
            wire, _fetch=_fetch_ok(b"\x00\x01\x02", "image/png; charset=binary")))
        assert part["image_url"]["url"].startswith("data:image/png;base64,")

    def test_multiple_messages_and_string_content_safe(self):
        # content 为字符串的 message（system/tool）必须安全跳过
        part = _image_part("https://r2.example/p.jpg")
        wire = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": [{"type": "text", "text": "hi"}, part]},
            {"role": "tool", "content": "tool result"},
        ]
        inlined, failed = asyncio.run(_inline_wire_images_as_data_urls(
            wire, _fetch=_fetch_ok(PNG_MAGIC)))
        assert (inlined, failed) == (1, 0)
        assert part["image_url"]["url"].startswith("data:image/png;base64,")

    def test_no_images_returns_zero_zero(self):
        wire = [_wire_user([{"type": "text", "text": "纯文本"}])]
        assert asyncio.run(_inline_wire_images_as_data_urls(
            wire, _fetch=_fetch_ok(PNG_MAGIC))) == (0, 0)

    def test_inline_replay_bypasses_gateway_download_shape(self):
        # 端到端形状验证：内联后的 user 消息里不再有任何 http(s) 图片引用，
        # 网关侧无需再访问 R2（这正是"通用兜底"的意义）
        parts = [_image_part(f"https://r2.example/p{i}.jpg") for i in range(3)]
        wire = [_wire_user(parts)]
        asyncio.run(_inline_wire_images_as_data_urls(wire, _fetch=_fetch_ok(PNG_MAGIC)))
        for msg in wire:
            for part in (msg.get("content") or []):
                if isinstance(part, dict) and part.get("type") == "image_url":
                    assert part["image_url"]["url"].startswith("data:")

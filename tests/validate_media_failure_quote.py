"""Regression checks for quote-formatted media generation failures."""

import asyncio

from apitelegramchat.ai_handlers import _render_media_failure_quote
from apitelegramchat.tool_executors import _TOOL_TIMEOUT_MARKER, format_tool_result


async def main() -> None:
    cases = (
        (
            "generate_image_from_text",
            {},
            "❌ 图像生成失败 (HTTP 502): upstream unavailable",
            "🎨 图片生成失败",
            "图像生成失败 (HTTP 502): upstream unavailable",
        ),
        (
            "edit_image_with_reference",
            {},
            "⚠️ 图片生成成功，但下载全部失败。失败项: 图片 1",
            "🎨 图片编辑失败",
            "⚠️ 图片生成成功，但下载全部失败。失败项: 图片 1",
        ),
        (
            "generate_video",
            {},
            "❌ 视频生成失败：未获取到视频链接。",
            "🎬 视频生成失败",
            "视频生成失败：未获取到视频链接。",
        ),
    )

    for tool_name, arguments, result, expected_summary, expected_message in cases:
        summary, details = await format_tool_result(tool_name, arguments, result)
        assert summary == expected_summary
        assert details.startswith("<p><b>Result</b></p><blockquote>")
        assert details.endswith("</blockquote>")
        assert expected_message in details
        assert "&lt;br/&gt;" not in details

    summary, details = await format_tool_result(
        "generate_video", {}, _TOOL_TIMEOUT_MARKER
    )
    assert summary == "⏱️ Video generation timed out"
    assert details.startswith("<p><b>Result</b></p><blockquote>")

    native_details = _render_media_failure_quote(
        "⚠️ <b>ModelScope 图像接口 请求失败</b><br/>HTTP 状态：429<br/>详情：限流"
    )
    assert native_details == (
        "<p><b>Result</b></p><blockquote>"
        "⚠️ ModelScope 图像接口 请求失败<br/>HTTP 状态：429<br/>详情：限流"
        "</blockquote>"
    )

    print("媒体失败引用格式验证通过")


if __name__ == "__main__":
    asyncio.run(main())

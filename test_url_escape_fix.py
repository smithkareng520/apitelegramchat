from __future__ import annotations

import asyncio
import re

from apitelegramchat.rich_media import normalize_rich_media_html
from apitelegramchat.tool_executors import _format_image_generation_result, format_tool_result

URL = (
    "https://a94baf2f82dbdc55044f54eb19838b0c.r2.cloudflarestorage.com/"
    "dearella/generated/ac6716cb3aad4b48ad98ef6ed2867e81_0.png?"
    "X-Amz-Algorithm=AWS4-HMAC-SHA256&"
    "X-Amz-Credential=5e1dc48ef14f62c26496ff38f2155b55%2F20260814%2Fauto%2Fs3%2Faws4_request&"
    "X-Amz-Date=20260814T212316Z&"
    "X-Amz-Expires=3600&"
    "X-Amz-SignedHeaders=host&"
    "X-Amz-Signature=209b299df3ffbcbf9fa90875bd5721510f3f5ee938afefcf0906d3026362ef48"
)


def attr_values(markup: str, attr: str) -> list[str]:
    return re.findall(rf'\b{attr}="([^"]*)"', markup)


def assert_media_card(label: str, markup: str) -> None:
    srcs = attr_values(markup, "src")
    hrefs = attr_values(markup, "href")
    assert srcs, f"{label}: missing src"
    assert hrefs, f"{label}: missing href"
    assert all("&amp;" in src for src in srcs), f"{label}: src must retain entity escaping"
    assert all(href == URL for href in hrefs), f"{label}: href must equal raw presigned URL"
    assert all("&amp;" not in href for href in hrefs), f"{label}: href contains literal entity"
    print(f"PASS {label}: src escaped; href has {hrefs[0].count('&')} raw query separators")


async def main() -> None:
    image_result = f"✅ 已生成 1 张图片。\n图片链接：\n{URL}"
    _, image_html = _format_image_generation_result(
        image_result,
        operation_en="Generated",
        operation_zh="已生成",
        failure_summary="image failure",
        failure_fallback="image failure",
    )
    assert_media_card("image tool card", image_html)

    _, video_html = await format_tool_result(
        "generate_video",
        {"prompt": "test 5 秒"},
        f"✅ 已生成视频。\n视频链接：{URL}",
    )
    assert_media_card("video tool card", video_html)

    link_url = URL.replace("_0.png", "_0.download")
    markdown_html = normalize_rich_media_html(f"[下载文件]({link_url})")
    hrefs = attr_values(markdown_html, "href")
    assert hrefs == [link_url], "Markdown fallback did not preserve raw href"
    assert "&amp;" not in hrefs[0], "Markdown fallback contains literal entity"
    print("PASS markdown link fallback: href equals raw presigned URL")


asyncio.run(main())

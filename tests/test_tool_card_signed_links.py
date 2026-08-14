#!/usr/bin/env python3
"""仅覆盖工具卡片图片/视频链接的 URL 上下文。"""
from __future__ import annotations

import ast
import asyncio
import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / "src/apitelegramchat/utils.py"
EXECUTORS = ROOT / "src/apitelegramchat/tool_executors.py"
URL = (
    "https://example.r2.cloudflarestorage.com/bucket/generated/example.png"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=test%2F20260814%2Fauto%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260814T115123Z"
    "&X-Amz-Expires=3600"
    "&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=abcdef"
)


def load(path: Path, names: set[str], ns: dict, assignments: set[str] | None = None) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            body.append(node)
        elif assignments and isinstance(node, ast.Assign):
            targets = {item.id for item in node.targets if isinstance(item, ast.Name)}
            if targets & assignments:
                body.append(node)
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), ns)


def main() -> None:
    utils_ns = {"re": re}
    load(UTILS, {"_escape_media_src_urls"}, utils_ns, {"_VALID_HTML_ENTITIES", "_BARE_AMP_RE", "_RICH_URL_ATTR_RE"})

    image_ns = {"re": re, "escape_html": html.escape}
    load(EXECUTORS, {"_format_image_generation_result"}, image_ns)
    _summary, image_card = image_ns["_format_image_generation_result"](
        f"✅ 已生成 1 张图片。\n图片链接：\n{URL}",
        operation_en="Generated", operation_zh="已生成", failure_summary="failed", failure_fallback="failed",
    )
    image_sent = utils_ns["_escape_media_src_urls"](image_card)
    assert f'<img src="{html.escape(URL)}"/>' in image_sent
    assert f'<a href="{URL}">图片 1</a>' in image_sent
    assert f'<a href="{html.escape(URL)}">图片 1</a>' not in image_sent

    video_ns = {"re": re, "html": html, "escape_html": html.escape, "_TOOL_TIMEOUT_MARKER": "__timeout__"}
    load(EXECUTORS, {"format_tool_result"}, video_ns)
    _summary, video_card = asyncio.run(video_ns["format_tool_result"](
        "generate_video", {}, f"✅ 已生成视频。\n视频链接：{URL}"
    ))
    video_sent = utils_ns["_escape_media_src_urls"](video_card)
    assert f'<video src="{html.escape(URL)}"></video>' in video_sent
    assert f'<a href="{URL}">下载 / 查看视频</a>' in video_sent
    assert f'<a href="{html.escape(URL)}">下载 / 查看视频</a>' not in video_sent

    print("PASS: tool-card href uses raw URL; media src uses HTML-escaped URL")


if __name__ == "__main__":
    main()

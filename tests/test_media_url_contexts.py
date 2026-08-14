#!/usr/bin/env python3
"""媒体预签名 URL 在工具、原生模型与富文本三条链路中的回归测试。"""
from __future__ import annotations

import ast
import asyncio
import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / "src/apitelegramchat/utils.py"
EXECUTORS = ROOT / "src/apitelegramchat/tool_executors.py"
TOOL_LOOP = ROOT / "src/apitelegramchat/ai/tool_call_loop.py"
AGENTIC = ROOT / "src/apitelegramchat/ai/agentic_loops.py"
URL = (
    "https://example.r2.cloudflarestorage.com/bucket/generated/demo.png"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=key%2F20260814%2Fauto%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260814T115123Z"
    "&X-Amz-Expires=3600"
    "&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=abcdef"
)


def load_functions(path: Path, names: set[str], namespace: dict, assignments: set[str] | None = None) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            selected.append(node)
        elif assignments and isinstance(node, ast.Assign):
            targets = {item.id for item in node.targets if isinstance(item, ast.Name)}
            if targets & assignments:
                selected.append(node)
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)


def load_url_helpers() -> dict:
    ns = {"html": html, "re": re}
    load_functions(
        UTILS,
        {"raw_media_url", "media_url_html_attr", "_escape_media_src_urls"},
        ns,
        {"_VALID_HTML_ENTITIES", "_BARE_AMP_RE", "_RICH_URL_ATTR_RE"},
    )
    return ns


def test_url_context_helpers() -> None:
    ns = load_url_helpers()
    html_attr = ns["media_url_html_attr"](URL)
    assert "&amp;X-Amz-Credential=" in html_attr
    assert ns["raw_media_url"](html_attr) == URL
    assert ns["_escape_media_src_urls"](f'<img src="{html_attr}"/>') == f'<img src="{html_attr}"/>'


def test_tool_cards_do_not_publish_html_escaped_anchor() -> None:
    helpers = load_url_helpers()
    ns = {"re": re, "html": html, "escape_html": html.escape, "media_url_html_attr": helpers["media_url_html_attr"]}
    load_functions(EXECUTORS, {"_format_image_generation_result"}, ns)
    _summary, image_html = ns["_format_image_generation_result"](
        f"✅ 已生成 1 张图片。\n图片链接：\n{URL}",
        operation_en="Generated", operation_zh="已生成", failure_summary="failed", failure_fallback="failed",
    )
    assert '<img src="' in image_html
    assert "&amp;X-Amz-Credential=" in image_html
    assert '<a href="' not in image_html
    assert "图片 1" not in image_html

    ns = {
        "re": re,
        "html": html,
        "escape_html": html.escape,
        "media_url_html_attr": helpers["media_url_html_attr"],
        "_TOOL_TIMEOUT_MARKER": "__timeout__",
    }
    load_functions(EXECUTORS, {"format_tool_result"}, ns)
    _summary, video_html = asyncio.run(ns["format_tool_result"](
        "generate_video", {}, f"✅ 已生成视频。\n视频链接：{URL}"
    ))
    assert '<video src="' in video_html
    assert "&amp;X-Amz-Credential=" in video_html
    assert '<a href="' not in video_html


def test_tool_buttons_keep_raw_url() -> None:
    helpers = load_url_helpers()
    sent: list[dict] = []

    async def fake_send(_chat_id, _content, *, reply_markup=None, reassert_draft=False, **_kwargs):
        sent.append({"reply_markup": reply_markup, "reassert_draft": reassert_draft})
        return True

    ns = {
        "re": re,
        "raw_media_url": helpers["raw_media_url"],
        "send_rich_html_message": fake_send,
        "logger": type("Logger", (), {"exception": lambda *_args, **_kwargs: None})(),
        "_MEDIA_RESULT_TOOL_NAMES": {"generate_image_from_text", "edit_image_with_reference", "generate_video", "qr_code"},
    }
    load_functions(TOOL_LOOP, {"_extract_raw_media_urls", "_send_media_open_buttons"}, ns)
    escaped = html.escape(URL, quote=True)
    urls = ns["_extract_raw_media_urls"]("generate_video", f"✅ 已生成视频。\n视频链接：{escaped}")
    assert urls == [URL]
    asyncio.run(ns["_send_media_open_buttons"](1, "generate_video", f"✅ 已生成视频。\n视频链接：{escaped}"))
    sent_url = sent[0]["reply_markup"]["inline_keyboard"][0][0]["url"]
    assert sent_url == URL
    assert "&amp;" not in sent_url
    assert sent[0]["reassert_draft"] is True


def test_native_paths_use_both_contexts() -> None:
    source = AGENTIC.read_text(encoding="utf-8")
    ast.parse(source, filename=str(AGENTIC))
    assert "media_url_html_attr(u)" in source
    assert "_media_open_keyboard(uploaded_urls" in source
    assert '"url": raw_url' in source
    assert "原始媒体 URL（仅供后续模型请求使用）" in source


def main() -> None:
    test_url_context_helpers()
    test_tool_cards_do_not_publish_html_escaped_anchor()
    test_tool_buttons_keep_raw_url()
    test_native_paths_use_both_contexts()
    print("PASS: image/video tool URL contexts are isolated")


if __name__ == "__main__":
    main()

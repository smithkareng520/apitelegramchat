import ast
import html
import re
from pathlib import Path


def load_nodes(path: str, names: set[str], namespace: dict) -> dict:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names for target in node.targets
        ):
            selected.append(node)
    exec(compile(ast.Module(body=selected, type_ignores=[]), path, "exec"), namespace)
    return namespace


BASE_URL = (
    "https://example.r2.cloudflarestorage.com/dearella/generated/example.png?"
    "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=test%2F20260814&"
    "X-Amz-Signature=signature"
)
UPSTREAM_URL = BASE_URL.replace("&", "&amp;")
SAMPLE = f"已生成 1 张图片：\n图片 1 ({UPSTREAM_URL})"


if __name__ == "__main__":
    tool_ns = load_nodes(
        "src/apitelegramchat/tool_executors.py",
        {"_MEDIA_RESULT_URL_RE", "_extract_media_result_urls", "_format_image_generation_result"},
        {"html": html, "re": re, "escape_html": html.escape},
    )
    summary, detail = tool_ns["_format_image_generation_result"](
        SAMPLE,
        operation_en="Generated",
        operation_zh="已生成",
        failure_summary="failed",
        failure_fallback="failed",
    )
    assert summary == "🎨 Generated 1 image"
    assert BASE_URL not in detail
    assert UPSTREAM_URL in detail

    utils_ns = load_nodes(
        "src/apitelegramchat/utils.py",
        {"_RICH_URL_ATTR_RE", "_decode_rich_url_entities", "_sanitize_raw_href_url", "_escape_media_src_urls"},
        {"html": html, "re": re},
    )
    sent_html = utils_ns["_escape_media_src_urls"](detail)
    expected_src = BASE_URL.replace("&", "&amp;")
    assert f'<img src="{expected_src}"/>' in sent_html
    assert f'<a href="{BASE_URL}">图片 1</a>' in sent_html
    assert "amp;amp;" not in sent_html
    print("PASS: upstream &amp; URL becomes raw & in href and one-layer &amp; in src")

import ast
import html
import re
from pathlib import Path


SOURCE = Path("src/apitelegramchat/utils.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
NAMES = {"_VALID_HTML_ENTITIES", "_BARE_AMP_RE", "_RICH_URL_ATTR_RE", "_normalize_rich_url_attr", "_escape_media_src_urls"}
SELECTED = []
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES:
        SELECTED.append(node)
    elif isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id in NAMES for target in node.targets
    ):
        SELECTED.append(node)

NAMESPACE = {"html": html, "re": re}
exec(compile(ast.Module(body=SELECTED, type_ignores=[]), "utils_excerpt.py", "exec"), NAMESPACE)
escape_media_urls = NAMESPACE["_escape_media_src_urls"]

BASE_URL = "https://cdn.example.com/media.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc123"
EXPECTED_HTML_URL = BASE_URL.replace("&", "&amp;")


def check(input_url: str) -> None:
    rendered = escape_media_urls(f'<a href="{input_url}">下载</a>')
    expected = f'<a href="{EXPECTED_HTML_URL}">下载</a>'
    assert rendered == expected, (rendered, expected)
    assert "amp;amp;" not in rendered


if __name__ == "__main__":
    check(BASE_URL)
    check(EXPECTED_HTML_URL)
    check(EXPECTED_HTML_URL.replace("&amp;", "&amp;amp;"))
    print("PASS: media/download URL is encoded exactly once")

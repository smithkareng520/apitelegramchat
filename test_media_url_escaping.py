import ast
import html
import re
from pathlib import Path


SOURCE = Path("src/apitelegramchat/utils.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
NAMES = {
    "_VALID_HTML_ENTITIES",
    "_BARE_AMP_RE",
    "_RICH_URL_ATTR_RE",
    "_decode_rich_url_entities",
    "_sanitize_raw_href_url",
    "_escape_media_src_urls",
}
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
escape_urls = NAMESPACE["_escape_media_src_urls"]

BASE_URL = "https://cdn.example.com/media.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc123"
ENCODED_URL = BASE_URL.replace("&", "&amp;")
DOUBLE_ENCODED_URL = ENCODED_URL.replace("&amp;", "&amp;amp;")


def check(input_url: str) -> None:
    rendered = escape_urls(
        f'<figure><video src="{input_url}"></video>'
        f'<figcaption><a href="{input_url}">下载 / 查看视频</a></figcaption></figure>'
    )
    expected = (
        f'<figure><video src="{ENCODED_URL}"></video>'
        f'<figcaption><a href="{BASE_URL}">下载 / 查看视频</a></figcaption></figure>'
    )
    assert rendered == expected, (rendered, expected)
    assert "amp;amp;" not in rendered


if __name__ == "__main__":
    check(BASE_URL)
    check(ENCODED_URL)
    check(DOUBLE_ENCODED_URL)
    print("PASS: src is entity-escaped once; href keeps raw query ampersands")

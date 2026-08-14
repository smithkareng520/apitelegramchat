import ast
import html
import re
from pathlib import Path


SOURCE = Path("src/apitelegramchat/ai/tool_call_loop.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
NAMES = {"_MEDIA_RESULT_URL_RE", "_normalize_media_tool_result_urls"}
SELECTED = []
for node in TREE.body:
    if isinstance(node, ast.FunctionDef) and node.name in NAMES:
        SELECTED.append(node)
    elif isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id in NAMES for target in node.targets
    ):
        SELECTED.append(node)

NAMESPACE = {"html": html, "re": re}
exec(compile(ast.Module(body=SELECTED, type_ignores=[]), "tool_call_loop_excerpt.py", "exec"), NAMESPACE)
normalize = NAMESPACE["_normalize_media_tool_result_urls"]

BASE_URL = (
    "https://a94baf2f82dbdc55044f54eb19838b0c.r2.cloudflarestorage.com/"
    "dearella/generated/16afcb64c9cc4876ac9cc921ae1f0ea1_0.png?"
    "X-Amz-Algorithm=AWS4-HMAC-SHA256&"
    "X-Amz-Credential=5e1dc48ef14f62c26496ff38f2155b55%2F20260814%2Fauto%2Fs3%2Faws4_request&"
    "X-Amz-Date=20260814T221813Z&X-Amz-Expires=3600&"
    "X-Amz-SignedHeaders=host&X-Amz-Signature=3247d4200b252aa9777ab6e39ed498de4cfc6ccb72f1844db225b3ebd8d0cd03"
)
UPSTREAM_RESULT = f"已生成 1 张图片：\n图片 1 ({BASE_URL.replace('&', '&amp;')})"


if __name__ == "__main__":
    normalized = normalize(UPSTREAM_RESULT)
    assert BASE_URL in normalized
    assert "&amp;" not in normalized
    print("PASS: frontend-visible media tool result keeps raw query ampersands")

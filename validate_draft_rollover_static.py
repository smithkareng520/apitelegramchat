"""Isolated verification of rich-message text and structural limits.

The exact pure methods are extracted from the production class AST, avoiding a
full application import that starts unrelated external runtime components.
"""
import ast
import html
import re
from pathlib import Path

SOURCE = Path("src/apitelegramchat/ai_handlers.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
builder = next(
    node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "RichMessageBuilder"
)
method_names = {
    "_rich_message_text",
    "_rich_message_text_chars",
    "_rich_message_block_count",
    "_needs_draft_rollover",
    "_split_html_for_rich_messages",
}
methods = [
    node for node in builder.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in method_names
]
assert {node.name for node in methods} == method_names
minimal_class = ast.ClassDef(
    name="RichLimitHarness", bases=[], keywords=[], body=methods, decorator_list=[]
)
module = ast.fix_missing_locations(ast.Module(body=[minimal_class], type_ignores=[]))
namespace = {
    "html": html,
    "re": re,
    "escape_html": html.escape,
    "strip_html_tags": lambda content: re.sub(r"<[^>]*>", "", content),
}
exec(compile(module, "<exact_rich_limit_methods>", "exec"), namespace)

harness = namespace["RichLimitHarness"]()
harness.MAX_RICH_MESSAGE_TEXT_CHARS = 1_000
harness.RICH_DRAFT_ROLLOVER_TEXT_CHARS = 900
harness.MAX_RICH_MESSAGE_BLOCKS = 500
harness.RICH_DRAFT_ROLLOVER_BLOCKS = 440

# HTML markup and entity spellings do not inflate parsed rich-text length.
marked = "<p>" + ("&amp;<b>甲</b>" * 899) + "</p>"
assert harness._rich_message_text_chars(marked) == 1_798
assert harness._needs_draft_rollover(marked)

# A long rich message is split by post-entity visible text length, not source
# length. Each generated fallback paragraph is under the configured 1,000 limit.
source = "<p>" + ("& \"' Telegram 富消息需要安全分段。" * 240) + "</p>"
chunks = harness._split_html_for_rich_messages(source)
assert len(chunks) > 1
assert all(harness._rich_message_text_chars(chunk) <= harness.MAX_RICH_MESSAGE_TEXT_CHARS for chunk in chunks)
assert all(chunk.startswith("<p>") and chunk.endswith("</p>") for chunk in chunks)
assert any("&amp;" in chunk for chunk in chunks)

# Structural rollover fires before Telegram's documented 500-block ceiling.
block_heavy = "".join("<p>x</p>" for _ in range(440))
assert harness._rich_message_block_count(block_heavy) == 440
assert harness._needs_draft_rollover(block_heavy)

# InputRichMessage must choose exactly one representation. All rich send payloads
# now use html alone, rather than the previous content+html pair.
UTILS = Path("src/apitelegramchat/utils.py").read_text(encoding="utf-8")
assert UTILS.count('"rich_message": {"html": html_content}') == 3
assert '"content": html_content,\n            "html": html_content' not in UTILS
assert "async def rollover_if_needed" in SOURCE
assert "await self._persist_completed_segment(candidate)" in SOURCE

print(
    "isolated rich draft validation passed "
    f"segments={len(chunks)} max_visible_chars={max(harness._rich_message_text_chars(chunk) for chunk in chunks)}"
)

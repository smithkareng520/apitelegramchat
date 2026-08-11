import ast
from pathlib import Path


def test_tool_refresh_loop_force_includes_bash():
    source = Path("src/apitelegramchat/ai_handlers.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    text = source
    assert "has_bash_tool = any(fn_name in BASH_TOOLS" in text
    assert "force_tool_refresh = has_image_tool or has_bash_tool" in text
    assert "await builder.flush(force=force_tool_refresh)" in text

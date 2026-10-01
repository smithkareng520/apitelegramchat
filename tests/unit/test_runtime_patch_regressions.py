from pathlib import Path
import ast


ROOT = Path(__file__).resolve().parents[2]


def test_ai_handlers_imports_log_truncate_limit_from_config():
    tree = ast.parse((ROOT / "src" / "ai_handlers.py").read_text(encoding="utf-8"))
    imported = set()
    used = False
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "config":
            imported.update(alias.name for alias in node.names)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "LOG_TRUNCATE_LIMIT" and isinstance(node.ctx, ast.Load):
            used = True
            break
    assert used
    assert "LOG_TRUNCATE_LIMIT" in imported


def test_fetch_content_limit_has_safe_default():
    text = (ROOT / "src" / "search" / "fetch_url.py").read_text(encoding="utf-8")
    assert "CONTENT_MAX_BYTES" in text
    assert "except (TypeError, ValueError)" in text

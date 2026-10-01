"""Workspace/R2 boundary tests.

Ordinary tool edits are local-only. User-upload R2 caching remains owned by
file_handlers, and is therefore tested separately from the workspace tools.
"""
from pathlib import Path
import sys

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_text_editor_has_no_r2_dependency():
    source = (SRC / "search" / "text_editor.py").read_text(encoding="utf-8")
    assert "s3_utils" not in source
    assert "upload_bytes_to_r2" not in source


def test_file_upload_cache_has_its_own_explicit_r2_boundary():
    source = (SRC / "file_handlers.py").read_text(encoding="utf-8")
    assert "_upload_to_r2_after_download" in source
    assert 'return f"telegram/{file_id}"' in source

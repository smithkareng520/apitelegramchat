"""Regression guard for failed-turn draft preservation."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ai_error_path_delegates_failed_turn_lifecycle():
    source = (ROOT / "src" / "ai_handlers.py").read_text(encoding="utf-8")
    marker = "await turn_recovery.finalize_failed_turn("
    block_start = source.index(marker, source.index("async def get_ai_response"))
    block = source[block_start:source.index("if is_timer:", block_start)]
    assert "draft_builder=builder" in block
    assert "persist_salvaged_journal" not in block
    assert "finalize_interrupted_draft" not in block

"""Regression tests for packaged-skill auto-sync shutdown lifecycle."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys


SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import skills


async def _yield_once() -> None:
    await asyncio.sleep(0)


def test_auto_sync_watcher_keeps_stop_event_after_global_clear(monkeypatch, tmp_path, caplog):
    """Clearing the module handle must not make a live watcher dereference None."""
    caplog.set_level("WARNING", logger="skills")
    monkeypatch.setenv("APITELEGRAMCHAT_SKILLS_DIR", str(tmp_path / "packaged"))
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    packaged = tmp_path / "packaged" / "demo"
    packaged.mkdir(parents=True)
    (packaged / "SKILL.md").write_text("v1", encoding="utf-8")

    monkeypatch.setattr(skills, "_SYNC_INTERVAL_SECONDS", 3600.0)
    monkeypatch.setattr(
        skills,
        "sync_packaged_skills_for_existing_workspaces",
        lambda: {"workspaces": 0, "copied": 0, "updated": 0, "preserved": 0, "errors": []},
    )

    async def scenario() -> None:
        await skills.start_packaged_skill_auto_sync()
        task = skills._sync_watcher_task
        stop_event = skills._sync_watcher_stop
        assert task is not None
        assert stop_event is not None

        # Simulate the old shutdown ordering: the module-level handle is cleared
        # while the watcher still exists. The watcher must continue to use its
        # captured local Event rather than dereferencing the global.
        skills._sync_watcher_stop = None
        stop_event.set()
        await _yield_once()
        await task
        assert task.exception() is None
        assert "packaged skill auto-sync stopped unexpectedly" not in caplog.text

        skills._sync_watcher_task = None

    try:
        asyncio.run(scenario())
    finally:
        skills._sync_watcher_task = None
        skills._sync_watcher_stop = None

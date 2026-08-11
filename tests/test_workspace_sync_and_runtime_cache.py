from pathlib import Path


def test_runtime_cache_is_local_only():
    source = Path("src/apitelegramchat/workspace_utils.py").read_text(encoding="utf-8")
    assert '".runtime_cache"' in source
    assert "_LOCAL_ONLY_TOPLEVEL_DIRS" in source


def test_workspace_sync_is_coalesced():
    source = Path("src/apitelegramchat/workspace_utils.py").read_text(encoding="utf-8")
    assert "_workspace_sync_tasks" in source
    assert "schedule_workspace_sync" in source
    assert "await asyncio.sleep(0.75)" in source


def test_python_bytecode_disabled_in_bash_env():
    source = Path("src/apitelegramchat/sandbox.py").read_text(encoding="utf-8")
    assert '"PYTHONDONTWRITEBYTECODE": "1"' in source


def test_bash_does_not_spawn_raw_workspace_sync_task():
    source = Path("src/apitelegramchat/tool_executors.py").read_text(encoding="utf-8")
    assert "asyncio.create_task(_async_sync_workspace_to_r2" not in source
    assert "schedule_workspace_sync(self.chat_id)" in source

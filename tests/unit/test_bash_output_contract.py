import asyncio

from bash_session import _format_bash_envelope
from tool_result_format import format_tool_result
from ai.tool_summary import _tool_result_is_failure, _generate_tool_summary_done
from tool_ui_render import _parse_bash_envelope, _render_bash_result


def test_bash_result_contract_contains_only_cwd_and_output():
    result = _format_bash_envelope("/work", "line 1\nline 2")
    assert result == "Cwd: /work\nline 1\nline 2"
    assert "Exit code:" not in result
    assert "/work$" not in result
    assert "line 1" in result


def test_bash_nonzero_command_is_not_tool_failure():
    result = "Cwd: /work\nbash: missing-command: command not found"
    assert not _tool_result_is_failure("bash", {"command": "missing-command"}, result)
    assert _generate_tool_summary_done("bash", {"command": "missing-command"}, result) == "Ran a command"


def test_bash_tool_timeout_is_still_a_failure():
    assert _tool_result_is_failure("bash", {}, "Error: Command timed out after 2 seconds")


def test_bash_ui_uses_bash_highlighting_and_hides_metadata():
    details = _render_bash_result(
        "Cwd: /work\nbash: missing-command: command not found",
        {"command": "missing-command"},
    )
    assert details.count('class="language-bash"') == 2
    assert "Exit code:" not in details
    assert "Cwd: /work" not in details
    assert "missing-command" in details


def test_bash_parser_returns_cwd_and_output_only():
    assert _parse_bash_envelope("Cwd: /work\nhello") == ("/work", "hello")


def test_bash_format_tool_result_nonzero_stays_done():
    summary, details = asyncio.run(
        format_tool_result(
            "bash",
            {"command": "false", "description": "测试非零退出"},
            "Cwd: /work\ncommand failed\n",
        )
    )
    assert summary == "测试非零退出"
    assert 'class="language-bash"' in details
    assert "Exit code:" not in details


def test_bash_session_nonzero_command_returns_output_without_exit_code(tmp_path, monkeypatch):
    import sandbox
    import workspace_paths
    from bash_session import BashSession

    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("APITELEGRAMCHAT_SANDBOX_DISABLE_LANDLOCK", "1")
    monkeypatch.setattr(sandbox, "_apply_landlock", lambda path: True)
    workspace_paths.data_root.cache_clear()
    workspace_paths.workspaces_root.cache_clear()

    async def run():
        session = BashSession(991234)
        try:
            result = await session.execute("printf 'command-output\\n'; false", total_timeout=10, idle_timeout=2)
            assert result.startswith("Cwd: ")
            assert "command-output" in result
            assert "Exit code:" not in result
            assert "false" not in result
        finally:
            await session.close()

    asyncio.run(run())

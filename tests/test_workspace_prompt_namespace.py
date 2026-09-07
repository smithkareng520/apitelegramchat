from pathlib import Path
import sys


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_workspace_paths_are_isolated_by_user_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path))

    # Import after the env var is set so workspace_paths resolves the test root.
    import workspace_paths

    user_a = workspace_paths.workspace_workdir(12345, "10001")
    user_b = workspace_paths.workspace_workdir(12345, "20002")

    assert user_a != user_b
    assert user_a == tmp_path / "workspaces" / "10001"
    assert user_b == tmp_path / "workspaces" / "20002"


def test_workspace_prompt_code_accepts_explicit_namespace():
    source = (SRC / "ai_handlers.py").read_text(encoding="utf-8")
    assert "workspace_namespace_value: str | None = None" in source
    assert "workspace_workdir(chat_id, workspace_namespace_value)" in source
    assert "workspace_guide=_workspace_guide_html(chat_id, workspace_namespace_value)" in source

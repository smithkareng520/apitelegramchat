'''Unit tests for workspace prompt namespace.'''

from pathlib import Path
import sys


SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _fresh_workspace_paths(monkeypatch, tmp_path):
    """Import workspace_paths with private data/workspaces roots and cleared caches.

    data_root()/workspaces_root() 带 lru_cache，且迁移检查带进程内缓存；
    每个测试都清空三者，保证用例之间互不污染（与执行顺序无关）。
    """
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    # 工作空间根同样指到 tmp（生产默认 /home，不属于测试可写假设）。
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    import workspace_paths

    workspace_paths.data_root.cache_clear()
    workspace_paths.workspaces_root.cache_clear()
    return workspace_paths


def test_workspace_paths_are_isolated_by_user_namespace(monkeypatch, tmp_path):
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)

    user_a = wp.workspace_workdir(12345, "10001")
    user_b = wp.workspace_workdir(12345, "20002")

    assert user_a != user_b
    # 家目录 = workspace 根本身：home/<ns>/（生产默认 /home/<ns>），无中间层。
    assert user_a == tmp_path / "home" / "10001"
    assert user_b == tmp_path / "home" / "20002"


def test_workspaces_root_defaults_to_home(monkeypatch, tmp_path):
    """默认工作空间根是 /home：家目录即 /home/<ns>，pwd 不带 data_root 前缀。"""
    monkeypatch.delenv("APITELEGRAMCHAT_WORKSPACES_DIR", raising=False)
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    import workspace_paths

    workspace_paths.data_root.cache_clear()
    workspace_paths.workspaces_root.cache_clear()
    try:
        assert workspace_paths.workspaces_root() == Path("/home")
        # 覆盖 env 生效：家目录跟随工作空间根。
        monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "custom"))
        workspace_paths.workspaces_root.cache_clear()
        assert workspace_paths.workspaces_root() == tmp_path / "custom"
        assert workspace_paths.workspace_root(12345, "10001") == tmp_path / "custom" / "10001"
    finally:
        # 清缓存避免污染后续用例（env 由 monkeypatch 自动还原）。
        workspace_paths.workspaces_root.cache_clear()
        workspace_paths.data_root.cache_clear()


def test_agent_home_layout(monkeypatch, tmp_path):
    """家目录布局：workspace 根即家目录，upload/download/skills 与隐藏
    缓存层 .runtime/ 都直接挂在根下，没有 claude/ 之类的中间层。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)

    home = wp.workspace_workdir(12345, "10001")
    root = wp.workspace_root(12345, "10001")
    cache = wp.runtime_cache_root(12345, "10001")
    upload = wp.workspace_upload_root(12345, "10001")
    download = wp.workspace_download_root(12345, "10001")
    skills = wp.workspace_skills_root(12345, "10001")

    # 家目录就是 workspace 根本身（$HOME = cwd = Landlock 边界）。
    assert home == root
    assert wp.agent_home(12345, "10001") == root
    assert home == tmp_path / "home" / "10001"
    # 用户可见子目录都在家目录根下（bash 相对路径体验不变）。
    assert upload == home / "upload"
    assert download == home / "download"
    assert skills == home / "skills"
    # 缓存层：隐藏目录 + 家目录内部（Landlock 边界内天然可写）。
    assert cache == home / ".runtime"
    assert cache.name.startswith(".")
    assert cache.parent == home
    # 状态域仍在 data_root 下、与 workspace 隔离。
    assert wp.state_root() == tmp_path / "data" / "state"


def test_runtime_state_lives_inside_hidden_cache_layer(monkeypatch, tmp_path):
    """bash 工具链清单 runtime.json 归入家目录隐藏层 .runtime/，不落在根下。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)

    from bash_session import _runtime_state_path

    state_path = _runtime_state_path(12345, "10001")
    home = wp.workspace_workdir(12345, "10001")
    assert state_path == home / ".runtime" / "runtime.json"
    assert state_path.parent.name.startswith(".")


def test_bash_session_landlock_scope_is_agent_home(monkeypatch, tmp_path):
    """BashSession 的 Landlock 放行边界必须是家目录（workdir = workspace 根）。

    只做构造级验证（不 spawn 进程）：持久会话与 one-shot 隔离执行两条
    路径都以 ``str(self.workdir.absolute())`` 作为 preexec 参数，且
    workdir 与 workspace（workspace 根）重合——家目录即根，无中间层。
    """
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)
    home = wp.workspace_workdir(12345, "10001")
    root = wp.workspace_root(12345, "10001")

    from bash_session import BashSession

    session = BashSession(12345, "10001")
    assert session.workdir == home
    assert session.workspace == root
    # 家目录即 workspace 根：两者重合，不存在 claude/ 之类的中间层。
    assert session.workdir == session.workspace
    assert session.workdir == tmp_path / "home" / "10001"

    # 源码级锁定：preexec 参数来自 workdir（家目录 = workspace 根）。
    source = (SRC / "bash_session.py").read_text(encoding="utf-8")
    assert "_preexec_sandbox,\n            str(self.workdir.absolute())," in source
    one_shot = source.split("async def _execute_heredoc_isolated", 1)[1]
    assert "workspace = self.workdir" in one_shot


def test_sandbox_env_home_points_to_agent_home(monkeypatch, tmp_path):
    """沙箱 $HOME / $WORKSPACE 指向 workspace 根；TMPDIR 等缓存全部在隐藏层内。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)
    home = wp.workspace_workdir(12345, "10001")

    from sandbox import build_sandbox_env

    env = build_sandbox_env(home, 12345, "10001")
    assert env["HOME"] == str(home)
    assert env["WORKSPACE"] == str(home)
    assert env["WORKDIR"] == str(home)
    assert env["PWD"] == str(home)
    tmpdir = Path(env["TMPDIR"])
    assert tmpdir == home / ".runtime" / "tmp"
    assert Path(env["PIP_CACHE_DIR"]).parent == home / ".runtime"
    assert Path(env["XDG_CACHE_HOME"]).parent == home / ".runtime"
    # .runtime 在家目录内 → Landlock 边界（= 家目录 = workspace 根）内缓存可写。
    assert home in tmpdir.parents


def test_workspace_prompt_code_accepts_explicit_namespace():
    source = (SRC / "ai_handlers.py").read_text(encoding="utf-8")
    assert "workspace_namespace_value: str | None = None" in source
    assert "workspace_workdir(chat_id, workspace_namespace_value)" in source
    assert "workspace_guide=_workspace_guide_html(chat_id, workspace_namespace_value)" in source

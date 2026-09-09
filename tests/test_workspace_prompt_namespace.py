from pathlib import Path
import sys


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _fresh_workspace_paths(monkeypatch, tmp_path):
    """Import workspace_paths with a private data root and cleared caches.

    data_root() 带 lru_cache，且 v2.3 的迁移检查带进程内缓存；每个测试
    都清空两者，保证用例之间互不污染（与执行顺序无关）。
    """
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    import workspace_paths

    workspace_paths.data_root.cache_clear()
    workspace_paths._home_migrated.clear()
    return workspace_paths


def test_workspace_paths_are_isolated_by_user_namespace(monkeypatch, tmp_path):
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)

    user_a = wp.workspace_workdir(12345, "10001")
    user_b = wp.workspace_workdir(12345, "20002")

    assert user_a != user_b
    # v2.3 布局：workdir = agent 家目录（容器根下的 claude/）。
    assert user_a == tmp_path / "data" / "workspaces" / "10001" / "claude"
    assert user_b == tmp_path / "data" / "workspaces" / "20002" / "claude"


def test_agent_home_layout(monkeypatch, tmp_path):
    """家目录布局：upload/download/skills 与隐藏缓存层 .runtime/ 都挂在家目录下。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)

    home = wp.workspace_workdir(12345, "10001")
    root = wp.workspace_root(12345, "10001")
    cache = wp.runtime_cache_root(12345, "10001")
    upload = wp.workspace_upload_root(12345, "10001")
    download = wp.workspace_download_root(12345, "10001")
    skills = wp.workspace_skills_root(12345, "10001")

    # 家目录是容器根下唯一的可见层。
    assert home.parent == root
    assert home.name == "claude"
    # 用户可见子目录都在家目录下（bash 相对路径体验不变）。
    assert upload == home / "upload"
    assert download == home / "download"
    assert skills == home / "skills"
    # 缓存层：隐藏目录 + 家目录内部（保证 Landlock 收紧后仍可写）。
    assert cache == home / ".runtime"
    assert cache.name.startswith(".")
    assert cache.parent == home
    # 状态域仍在 data_root 下、与 workspace 隔离。
    assert wp.state_root() == tmp_path / "data" / "state"


def test_legacy_workspace_layout_is_migrated(monkeypatch, tmp_path):
    """v2.2 旧布局（容器根平铺）首次访问时原子迁移进 claude/ 家目录。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)
    root = wp.workspace_root(12345, "10001")

    # 构造旧布局：容器根下平铺 download/upload/skills/runtime/runtime.json。
    (root / "download").mkdir()
    (root / "download" / "brief.pdf").write_text("pdf-bytes", encoding="utf-8")
    (root / "upload").mkdir()
    (root / "upload" / "out.txt").write_text("hello", encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills" / "demo").mkdir()
    (root / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (root / "runtime").mkdir()
    (root / "runtime" / "pip").mkdir()
    (root / "runtime.json").write_text('{"schema": 1}', encoding="utf-8")

    # 首次触碰家目录 → 迁移发生。
    home = wp.workspace_workdir(12345, "10001")

    assert home == root / "claude"
    assert (home / "download" / "brief.pdf").read_text(encoding="utf-8") == "pdf-bytes"
    assert (home / "upload" / "out.txt").is_file()
    assert (home / "skills" / "demo" / "SKILL.md").is_file()
    # runtime/ → .runtime/（隐藏缓存层），runtime.json 随迁。
    assert (home / ".runtime" / "pip").is_dir()
    assert (home / ".runtime" / "runtime.json").read_text(encoding="utf-8") == '{"schema": 1}'
    # 容器根下不再有旧条目。
    for legacy in ("download", "upload", "skills", "runtime", "runtime.json"):
        assert not (root / legacy).exists()


def test_migration_is_idempotent_and_never_overwrites(monkeypatch, tmp_path):
    """迁移只移动不合并：目标已存在时保留双方，重复调用无副作用。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)
    root = wp.workspace_root(12345, "10001")

    # 先建立新布局（家目录 + upload/ 已有新文件）。
    home = wp.workspace_workdir(12345, "10001")
    upload_root = wp.workspace_upload_root(12345, "10001")
    (upload_root / "new.txt").write_text("new", encoding="utf-8")

    # 模拟滚动升级窗口期：旧进程又往容器根的旧位置写了文件。
    (root / "upload").mkdir()
    (root / "upload" / "legacy.txt").write_text("legacy", encoding="utf-8")

    # 强制再次触发迁移路径（模拟重启后的另一个进程）。
    wp._home_migrated.clear()
    wp.agent_home(12345, "10001")

    # 新文件未被旧目录覆盖；旧目录因目标已存在而原地保留（对沙箱不可见）。
    assert (home / "upload" / "new.txt").read_text(encoding="utf-8") == "new"
    assert (root / "upload" / "legacy.txt").read_text(encoding="utf-8") == "legacy"


def test_runtime_state_lives_inside_hidden_cache_layer(monkeypatch, tmp_path):
    """bash 工具链清单 runtime.json 归入 <home>/.runtime/，不再落在容器根。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)

    from bash_session import _runtime_state_path

    state_path = _runtime_state_path(12345, "10001")
    home = wp.workspace_workdir(12345, "10001")
    assert state_path == home / ".runtime" / "runtime.json"
    assert state_path.parent.name.startswith(".")


def test_bash_session_landlock_scope_is_agent_home(monkeypatch, tmp_path):
    """BashSession 的 Landlock 放行边界必须是家目录（workdir），而非容器根。

    只做构造级验证（不 spawn 进程）：持久会话与 one-shot 隔离执行两条
    路径都以 ``str(self.workdir.absolute())`` 作为 preexec 参数。
    """
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)
    home = wp.workspace_workdir(12345, "10001")
    root = wp.workspace_root(12345, "10001")

    from bash_session import BashSession

    session = BashSession(12345, "10001")
    assert session.workdir == home
    assert session.workspace == root
    assert session.workdir != session.workspace
    assert session.workspace in session.workdir.parents

    # 源码级锁定：preexec 参数来自 workdir（家目录），防止回归到容器根。
    source = (SRC / "bash_session.py").read_text(encoding="utf-8")
    assert "_preexec_sandbox,\n            str(self.workdir.absolute())," in source
    one_shot = source.split("async def _execute_heredoc_isolated", 1)[1]
    assert "workspace = self.workdir" in one_shot
    assert "workspace = self.workspace" not in one_shot


def test_sandbox_env_home_points_to_agent_home(monkeypatch, tmp_path):
    """沙箱 $HOME / $WORKSPACE 指向家目录；TMPDIR 等缓存全部在隐藏层内。"""
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
    # .runtime 在家目录内 → Landlock 收紧到家目录后缓存仍可写。
    assert home in tmpdir.parents


def test_workspace_prompt_code_accepts_explicit_namespace():
    source = (SRC / "ai_handlers.py").read_text(encoding="utf-8")
    assert "workspace_namespace_value: str | None = None" in source
    assert "workspace_workdir(chat_id, workspace_namespace_value)" in source
    assert "workspace_guide=_workspace_guide_html(chat_id, workspace_namespace_value)" in source

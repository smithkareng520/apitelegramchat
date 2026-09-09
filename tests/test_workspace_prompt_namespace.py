from pathlib import Path
import sys


SRC = Path(__file__).resolve().parents[1] / "src"
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
    workspace_paths._home_migrated.clear()
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
    workspace_paths._home_migrated.clear()
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
        workspace_paths._home_migrated.clear()


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


def test_legacy_data_root_workspaces_are_folded_into_home(monkeypatch, tmp_path):
    """升级迁移：旧位置 data/workspaces/<ns> 首次访问时整目录并入新家。

    工作空间根从 data_root/workspaces 挪到 /home 后，旧位置的用户文件
    （skills/download/上传等）必须在首次访问时自动跟过来，不能孤儿化。
    """
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)

    legacy = tmp_path / "data" / "workspaces" / "10001"
    (legacy / "skills" / "old-skill").mkdir(parents=True)
    (legacy / "skills" / "old-skill" / "SKILL.md").write_text("old", encoding="utf-8")
    (legacy / "download").mkdir()
    (legacy / "download" / "brief.pdf").write_text("pdf-bytes", encoding="utf-8")
    (legacy / ".skills_initialized").write_text("initialized\n", encoding="utf-8")

    home = wp.workspace_workdir(12345, "10001")

    # 旧位置内容整体进入新家（home/<ns>），旧目录已删除。
    assert home == tmp_path / "home" / "10001"
    assert (home / "skills" / "old-skill" / "SKILL.md").read_text(encoding="utf-8") == "old"
    assert (home / "download" / "brief.pdf").read_text(encoding="utf-8") == "pdf-bytes"
    assert (home / ".skills_initialized").is_file()
    assert not legacy.exists()

    # 幂等可重入：重复触发迁移路径无副作用。
    wp._home_migrated.clear()
    wp.agent_home(12345, "10001")
    assert (home / "skills" / "old-skill" / "SKILL.md").read_text(encoding="utf-8") == "old"


def test_legacy_location_migration_never_overwrites_new_home(monkeypatch, tmp_path):
    """绝不覆盖：新家已存在同名条目时保留双方，旧目录残留原地保留。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)

    home = wp.workspace_workdir(12345, "10001")
    (home / "download").mkdir(exist_ok=True)
    (home / "download" / "new.txt").write_text("new", encoding="utf-8")

    legacy = tmp_path / "data" / "workspaces" / "10001"
    (legacy / "download").mkdir(parents=True)
    (legacy / "download" / "new.txt").write_text("stale", encoding="utf-8")
    (legacy / "upload").mkdir()
    (legacy / "upload" / "out.txt").write_text("old", encoding="utf-8")

    wp._home_migrated.clear()
    wp.agent_home(12345, "10001")

    # 新家内容原样保留；旧 upload/（新家没有的条目）照常并入。
    assert (home / "download" / "new.txt").read_text(encoding="utf-8") == "new"
    assert (home / "upload" / "out.txt").read_text(encoding="utf-8") == "old"
    # 同名条目未覆盖 → 旧 download/ 残留原地。
    assert (legacy / "download" / "new.txt").read_text(encoding="utf-8") == "stale"


def test_legacy_workspace_layout_is_migrated(monkeypatch, tmp_path):
    """v2.2 旧布局（runtime/ 与 runtime.json 平铺在根下）首次访问时迁移：
    runtime/ → .runtime/，runtime.json → .runtime/runtime.json；
    download/upload/skills 本就归属根，原地不动。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)
    root = wp.workspace_root(12345, "10001")

    # 构造旧布局：根下平铺 download/upload/skills/runtime/runtime.json。
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

    assert home == root
    # 用户文件层原地保留，绝无挪动。
    assert (home / "download" / "brief.pdf").read_text(encoding="utf-8") == "pdf-bytes"
    assert (home / "upload" / "out.txt").is_file()
    assert (home / "skills" / "demo" / "SKILL.md").is_file()
    # runtime/ → .runtime/（隐藏缓存层），runtime.json 随迁。
    assert (home / ".runtime" / "pip").is_dir()
    assert (home / ".runtime" / "runtime.json").read_text(encoding="utf-8") == '{"schema": 1}'
    # 根下不再有旧条目（runtime 目录与 runtime.json）。
    assert not (root / "runtime").exists()
    assert not (root / "runtime.json").exists()


def test_interim_claude_layout_is_folded_back(monkeypatch, tmp_path):
    """v2.3.0 过渡草案（根下 claude/ 家目录，未正式发布）首次访问时折叠
    回根：claude/ 下的条目逐个回到根，空的 claude/ 目录被删除。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)
    root = wp.workspace_root(12345, "10001")

    # 构造过渡布局：家目录整体位于根下 claude/。
    interim = root / "claude"
    (interim / "download").mkdir(parents=True)
    (interim / "download" / "brief.pdf").write_text("pdf-bytes", encoding="utf-8")
    (interim / "upload").mkdir()
    (interim / "upload" / "out.txt").write_text("hello", encoding="utf-8")
    (interim / "skills").mkdir()
    (interim / "skills" / "demo").mkdir()
    (interim / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (interim / ".runtime").mkdir()
    (interim / ".runtime" / "pip").mkdir()
    (interim / ".runtime" / "runtime.json").write_text('{"schema": 1}', encoding="utf-8")
    (interim / ".skills_initialized").write_text("initialized\n", encoding="utf-8")

    # 首次触碰家目录 → 折叠回根。
    home = wp.workspace_workdir(12345, "10001")

    assert home == root
    assert (home / "download" / "brief.pdf").read_text(encoding="utf-8") == "pdf-bytes"
    assert (home / "upload" / "out.txt").is_file()
    assert (home / "skills" / "demo" / "SKILL.md").is_file()
    assert (home / ".runtime" / "pip").is_dir()
    assert (home / ".runtime" / "runtime.json").read_text(encoding="utf-8") == '{"schema": 1}'
    assert (home / ".skills_initialized").is_file()
    # claude/ 中间层已消失。
    assert not (root / "claude").exists()


def test_migration_is_idempotent_and_never_overwrites(monkeypatch, tmp_path):
    """迁移只移动不合并、绝不覆盖：目标已存在时保留双方，重复调用无副作用。"""
    wp = _fresh_workspace_paths(monkeypatch, tmp_path)
    root = wp.workspace_root(12345, "10001")

    # 先建立新布局（.runtime/ 已存在且有新缓存）。
    home = wp.workspace_workdir(12345, "10001")
    runtime_dir = wp.runtime_cache_root(12345, "10001")
    (runtime_dir / "pip").mkdir(exist_ok=True)
    (runtime_dir / "pip" / "new.whl").write_text("new", encoding="utf-8")

    # 模拟滚动升级窗口期：旧进程又往根下的旧位置写了数据。
    (root / "runtime").mkdir()
    (root / "runtime" / "pip").mkdir()
    (root / "runtime" / "pip" / "legacy.whl").write_text("legacy", encoding="utf-8")

    # 强制再次触发迁移路径（模拟重启后的另一个进程）。
    wp._home_migrated.clear()
    wp.agent_home(12345, "10001")

    # 新缓存未被旧目录覆盖；旧 runtime/ 因目标已存在而原地保留。
    assert (home / ".runtime" / "pip" / "new.whl").read_text(encoding="utf-8") == "new"
    assert (root / "runtime" / "pip" / "legacy.whl").read_text(encoding="utf-8") == "legacy"

    # runtime.json 是可再生缓存：目标已存在时旧缓存直接丢弃（不覆盖）。
    (runtime_dir / "runtime.json").write_text('{"schema": 1, "fresh": true}', encoding="utf-8")
    (root / "runtime.json").write_text('{"schema": 1, "stale": true}', encoding="utf-8")
    wp._home_migrated.clear()
    wp.agent_home(12345, "10001")

    assert (home / ".runtime" / "runtime.json").read_text(encoding="utf-8") == '{"schema": 1, "fresh": true}'
    assert not (root / "runtime.json").exists()


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

"""用户 skills/ 目录 R2 持久化（恢复 + 备份）的回归测试。

覆盖 src/workspace_utils.py 中 `_ensure_workspace_initialized` 的完整链路：
打包 bootstrap → R2 恢复 → R2 增量备份。R2 用内存字典伪造（monkeypatch
workspace_utils 内已绑定的 s3_utils 符号），打包技能根目录经
APITELEGRAMCHAT_SKILLS_DIR 指向 tmp 目录，走真实的
sync_all_skill_assets_to_workspace 实现。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys


SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import workspace_paths as wp
import workspace_utils as wu


NS = "10001"
CHAT_ID = 12345


class _FakeR2:
    """最小 R2 桩：dict 存对象，接口签名与 s3_utils 一致。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def keys_under(self, prefix: str) -> list[str]:
        p = prefix.strip("/") + "/"
        return sorted(k for k in self.objects if k.startswith(p))

    async def list_r2_objects(self, prefix: str) -> list[str]:
        return self.keys_under(prefix)

    async def download_from_r2(self, key: str):
        data = self.objects.get(key)
        return bytes(data) if data is not None else None

    async def upload_bytes_to_r2(self, data: bytes, key: str, content_type: str = ""):
        self.objects[key] = bytes(data)
        return key

    async def delete_r2_object(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None


def _fresh_env(monkeypatch, tmp_path) -> _FakeR2:
    """隔离 data_root / workspaces_root / 进程内缓存 / init 锁注册表。"""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(data_dir))
    # 工作空间根同样隔离（生产默认 /home）。
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    # 打包技能根：一个最小技能包，走真实 bootstrap 实现。
    packaged = tmp_path / "packaged_skills"
    (packaged / "demo").mkdir(parents=True, exist_ok=True)
    (packaged / "demo" / "SKILL.md").write_text(
        "---\nname: demo\npriority: 1\n---\nbody\n", encoding="utf-8"
    )
    monkeypatch.setenv("APITELEGRAMCHAT_SKILLS_DIR", str(packaged))

    wp.data_root.cache_clear()
    wp.workspaces_root.cache_clear()
    wp._home_migrated.clear()
    wu._workspace_initialized.clear()
    wu._workspace_init_lock_registry._locks.clear()
    wu._workspace_file_locks._locks.clear()

    fake = _FakeR2()
    monkeypatch.setattr(wu, "is_r2_configured", lambda: True)
    monkeypatch.setattr(wu, "list_r2_objects", fake.list_r2_objects)
    monkeypatch.setattr(wu, "download_from_r2", fake.download_from_r2)
    monkeypatch.setattr(wu, "upload_bytes_to_r2", fake.upload_bytes_to_r2)
    monkeypatch.setattr(wu, "delete_r2_object", fake.delete_r2_object)
    return fake


def _skills_dir() -> Path:
    return wp.workspace_skills_root(CHAT_ID, NS)


def _wipe_disk_but_keep_env(monkeypatch, tmp_path) -> None:
    """模拟"服务重启 + 磁盘被清空"：清空数据目录与工作空间根，复位缓存。"""
    import shutil

    shutil.rmtree(tmp_path / "data", ignore_errors=True)
    shutil.rmtree(tmp_path / "home", ignore_errors=True)
    wp.data_root.cache_clear()
    wp.workspaces_root.cache_clear()
    wp._home_migrated.clear()
    wu._workspace_initialized.clear()
    wu._workspace_init_lock_registry._locks.clear()
    wu._workspace_file_locks._locks.clear()


def test_skill_relpath_guard():
    assert wu._is_safe_skill_relpath("demo/SKILL.md")
    assert wu._is_safe_skill_relpath("demo/scripts/a.py")
    assert not wu._is_safe_skill_relpath("../escape")
    assert not wu._is_safe_skill_relpath("/abs")
    assert not wu._is_safe_skill_relpath("")
    # 保留名：清单本体永远不作为技能文件参与恢复/备份。
    assert not wu._is_safe_skill_relpath(".manifest.json")


def test_first_init_backs_up_skills_and_manifest(monkeypatch, tmp_path):
    fake = _fresh_env(monkeypatch, tmp_path)

    asyncio.run(wu.init_workspace(CHAT_ID, NS))

    prefix = f"skills/{NS}"
    local = _skills_dir()
    assert (local / "demo" / "SKILL.md").is_file()
    # 打包技能与清单都已进 R2；manifest 哈希与本地内容一致。
    assert f"{prefix}/demo/SKILL.md" in fake.objects
    manifest = json.loads(fake.objects[f"{prefix}/.manifest.json"])
    import hashlib

    expect = hashlib.sha256((local / "demo" / "SKILL.md").read_bytes()).hexdigest()
    assert manifest["files"]["demo/SKILL.md"] == expect
    # 无变化时重复初始化：零上传（清单命中，增量比对全跳过）。
    before = dict(fake.objects)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    assert fake.objects == before


def test_restore_after_restart_with_wiped_disk(monkeypatch, tmp_path):
    fake = _fresh_env(monkeypatch, tmp_path)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))

    # 用户自建一个新技能并让备份通道同步上去。
    custom = _skills_dir() / "my-skill" / "SKILL.md"
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.write_text("---\nname: my-skill\n---\ncustom\n", encoding="utf-8")
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    assert f"skills/{NS}/my-skill/SKILL.md" in fake.objects

    # 重启 + 磁盘清空 → 重新初始化：自建技能必须从 R2 回来。
    _wipe_disk_but_keep_env(monkeypatch, tmp_path)
    # 直接拼路径检查（workspace_skills_root() 访问即建目录，不能用它断言）。
    assert not (tmp_path / "home" / NS / "skills").exists()
    asyncio.run(wu.init_workspace(CHAT_ID, NS))

    restored = (_skills_dir() / "my-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert restored == "---\nname: my-skill\n---\ncustom\n"
    # 打包技能也照常重新补齐。
    assert (_skills_dir() / "demo" / "SKILL.md").is_file()
    assert (_skills_dir().parent / ".skills_initialized").is_file()


def test_restore_never_overwrites_local_content(monkeypatch, tmp_path):
    fake = _fresh_env(monkeypatch, tmp_path)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))

    # 用户改了包内技能文件并备份。
    skill_md = _skills_dir() / "demo" / "SKILL.md"
    skill_md.write_text("---\nname: demo\n---\nuser edit\n", encoding="utf-8")
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    assert b"user edit" in fake.objects[f"skills/{NS}/demo/SKILL.md"]

    # 磁盘清空后重启：R2 恢复的是用户编辑版，而不是 pristine 打包版。
    _wipe_disk_but_keep_env(monkeypatch, tmp_path)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    assert "user edit" in (_skills_dir() / "demo" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    # 本地已有文件时 R2 永不覆盖：改本地内容，重新初始化后保持本地版。
    (_skills_dir() / "demo" / "SKILL.md").write_text(
        "---\nname: demo\n---\nnewer local\n", encoding="utf-8"
    )
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    assert "newer local" in (_skills_dir() / "demo" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_backup_propagates_local_deletion(monkeypatch, tmp_path):
    fake = _fresh_env(monkeypatch, tmp_path)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))

    custom = _skills_dir() / "my-skill" / "SKILL.md"
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.write_text("---\nname: my-skill\n---\ncustom\n", encoding="utf-8")
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    assert f"skills/{NS}/my-skill/SKILL.md" in fake.objects

    # 标记已存在（不再触发 bootstrap/恢复），本地删除后备份通道同步删除。
    custom.unlink()
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    assert f"skills/{NS}/my-skill/SKILL.md" not in fake.objects
    manifest = json.loads(fake.objects[f"skills/{NS}/.manifest.json"])
    assert "my-skill/SKILL.md" not in manifest["files"]
    assert "demo/SKILL.md" in manifest["files"]


def test_r2_unconfigured_keeps_pure_local_behavior(monkeypatch, tmp_path):
    _fresh_env(monkeypatch, tmp_path)
    monkeypatch.setattr(wu, "is_r2_configured", lambda: False)

    asyncio.run(wu.init_workspace(CHAT_ID, NS))

    # 打包技能照常落地；无任何 R2 交互（桩里没有对象被写入）。
    assert (_skills_dir() / "demo" / "SKILL.md").is_file()


def test_packaged_skill_upgrade_does_not_clobber_user_edit(monkeypatch, tmp_path):
    from skills import sync_all_skill_assets_to_workspace

    packaged = tmp_path / "packaged"
    (packaged / "demo").mkdir(parents=True)
    source = packaged / "demo" / "SKILL.md"
    source.write_text("v1", encoding="utf-8")
    monkeypatch.setenv("APITELEGRAMCHAT_SKILLS_DIR", str(packaged))

    home = tmp_path / "home"
    first = sync_all_skill_assets_to_workspace(home)
    assert first["copied"] == 1
    assert (home / "skills/demo/SKILL.md").read_text() == "v1"

    source.write_text("v2", encoding="utf-8")
    second = sync_all_skill_assets_to_workspace(home)
    assert second["updated"] == 1
    assert (home / "skills/demo/SKILL.md").read_text() == "v2"

    source.write_text("v3", encoding="utf-8")
    (home / "skills/demo/SKILL.md").write_text("user edit", encoding="utf-8")
    third = sync_all_skill_assets_to_workspace(home)
    assert third["preserved_user_edits"] == 1
    assert (home / "skills/demo/SKILL.md").read_text() == "user edit"


def test_packaged_manifest_not_backed_up_to_r2(monkeypatch, tmp_path):
    fake = _fresh_env(monkeypatch, tmp_path)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    assert f"skills/{NS}/.packaged-manifest.json" not in fake.objects

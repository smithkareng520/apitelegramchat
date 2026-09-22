"""skills/ R2 压缩快照同步回归测试。"""
from __future__ import annotations

import asyncio
import io
import tarfile
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
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def download_from_r2(self, key: str):
        data = self.objects.get(key)
        return bytes(data) if data is not None else None

    async def upload_bytes_to_r2(self, data: bytes, key: str, content_type: str = ""):
        self.objects[key] = bytes(data)
        return key

    async def list_r2_objects(self, prefix: str):
        p = prefix.rstrip("/") + "/"
        return sorted(k for k in self.objects if k.startswith(p))

    async def delete_r2_object(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None


def _fresh_env(monkeypatch, tmp_path) -> _FakeR2:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    packaged = tmp_path / "packaged_skills"
    (packaged / "demo").mkdir(parents=True)
    (packaged / "demo" / "SKILL.md").write_text("v1", encoding="utf-8")
    monkeypatch.setenv("APITELEGRAMCHAT_SKILLS_DIR", str(packaged))

    wp.data_root.cache_clear()
    wp.workspaces_root.cache_clear()
    wp._home_migrated.clear()
    wu._workspace_initialized.clear()
    wu._workspace_init_lock_registry._locks.clear()
    wu._workspace_file_locks._locks.clear()

    fake = _FakeR2()
    monkeypatch.setattr(wu, "is_r2_configured", lambda: True)
    monkeypatch.setattr(wu, "download_from_r2", fake.download_from_r2)
    monkeypatch.setattr(wu, "upload_bytes_to_r2", fake.upload_bytes_to_r2)
    monkeypatch.setattr(wu, "list_r2_objects", fake.list_r2_objects)
    monkeypatch.setattr(wu, "delete_r2_object", fake.delete_r2_object)
    return fake


def _skills_dir() -> Path:
    return wp.workspace_skills_root(CHAT_ID, NS)


def _wipe_disk(tmp_path) -> None:
    import shutil
    shutil.rmtree(tmp_path / "data", ignore_errors=True)
    shutil.rmtree(tmp_path / "home", ignore_errors=True)
    wp.data_root.cache_clear()
    wp.workspaces_root.cache_clear()
    wp._home_migrated.clear()
    wu._workspace_initialized.clear()
    wu._workspace_init_lock_registry._locks.clear()
    wu._workspace_file_locks._locks.clear()


def test_first_init_bootstraps_and_uploads_only_archive(monkeypatch, tmp_path):
    fake = _fresh_env(monkeypatch, tmp_path)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))

    local = _skills_dir()
    assert (local / "demo" / "SKILL.md").read_text() == "v1"
    assert list(fake.objects) == [f"skills/{NS}/skills.tar.gz"]
    assert fake.objects[f"skills/{NS}/skills.tar.gz"].startswith(b"\x1f\x8b")

    with tarfile.open(fileobj=io.BytesIO(fake.objects[f"skills/{NS}/skills.tar.gz"]), mode="r:gz") as tf:
        assert tf.getnames() == [".packaged-manifest.json", "demo/SKILL.md"]


def test_snapshot_roundtrip_after_restart(monkeypatch, tmp_path):
    fake = _fresh_env(monkeypatch, tmp_path)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))

    custom = _skills_dir() / "my-skill" / "SKILL.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("custom", encoding="utf-8")
    asyncio.run(wu._backup_user_skills_to_r2(_skills_dir().parent, NS))

    _wipe_disk(tmp_path)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))

    assert (_skills_dir() / "demo" / "SKILL.md").read_text() == "v1"
    assert (_skills_dir() / "my-skill" / "SKILL.md").read_text() == "custom"
    assert list(fake.objects) == [f"skills/{NS}/skills.tar.gz"]


def test_snapshot_replaces_deleted_files(monkeypatch, tmp_path):
    fake = _fresh_env(monkeypatch, tmp_path)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    custom = _skills_dir() / "my-skill" / "SKILL.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("custom", encoding="utf-8")
    asyncio.run(wu._backup_user_skills_to_r2(_skills_dir().parent, NS))

    custom.unlink()
    custom.parent.rmdir()
    asyncio.run(wu._backup_user_skills_to_r2(_skills_dir().parent, NS))

    _wipe_disk(tmp_path)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    assert not (_skills_dir() / "my-skill").exists()
    assert list(fake.objects) == [f"skills/{NS}/skills.tar.gz"]


def test_tree_fingerprint_does_not_read_content_or_hash(monkeypatch, tmp_path):
    _fresh_env(monkeypatch, tmp_path)
    skills = _skills_dir()
    (skills / "demo").mkdir(parents=True, exist_ok=True)
    path = skills / "demo" / "SKILL.md"
    path.write_text("content", encoding="utf-8")

    fingerprint = wu._skills_tree_fingerprint(skills)
    assert fingerprint[0][0] == "demo/SKILL.md"
    assert fingerprint[0][1] == len(b"content")
    assert fingerprint[0][2] == path.stat().st_mtime_ns


def test_unsafe_archive_path_is_rejected(monkeypatch, tmp_path):
    _fresh_env(monkeypatch, tmp_path)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        data = b"escape"
        info = tarfile.TarInfo("../escape")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    try:
        wu._extract_skills_archive(buffer.getvalue(), _skills_dir())
    except ValueError as exc:
        assert "unsafe skills archive path" in str(exc)
    else:
        raise AssertionError("unsafe archive path was accepted")


def test_r2_unconfigured_keeps_local_behavior(monkeypatch, tmp_path):
    fake = _fresh_env(monkeypatch, tmp_path)
    monkeypatch.setattr(wu, "is_r2_configured", lambda: False)
    asyncio.run(wu.init_workspace(CHAT_ID, NS))
    assert (_skills_dir() / "demo" / "SKILL.md").is_file()
    assert fake.objects == {}

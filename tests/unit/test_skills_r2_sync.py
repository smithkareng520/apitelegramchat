"""Skills bundle R2 persistence and workspace bootstrap tests."""
from __future__ import annotations

import asyncio
import io
import json
import sys
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import workspace_paths as wp
import workspace_utils as wu
import skills

NS = "10001"
CHAT_ID = 12345


class _FakeR2:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.uploads = 0

    async def get_r2_object_metadata(self, key: str):
        if key not in self.objects:
            return None
        return self.metadata.get(key, {})

    async def download_from_r2(self, key: str):
        data = self.objects.get(key)
        return bytes(data) if data is not None else None

    async def upload_bytes_to_r2(self, data: bytes, key: str, content_type: str = "", metadata=None):
        self.objects[key] = bytes(data)
        self.metadata[key] = {str(k).lower(): str(v) for k, v in (metadata or {}).items()}
        self.uploads += 1
        return key


def _fresh_env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    home_dir = tmp_path / "home"
    packaged = tmp_path / "packaged_skills"
    (packaged / "demo" / "scripts").mkdir(parents=True, exist_ok=True)
    (packaged / "demo" / "SKILL.md").write_text(
        "---\nname: demo\npriority: 1\n---\nbody\n", encoding="utf-8"
    )
    (packaged / "demo" / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(home_dir))
    monkeypatch.setenv("APITELEGRAMCHAT_SKILLS_DIR", str(packaged))
    monkeypatch.setenv("APITELEGRAMCHAT_SKILLS_BUNDLE_KEY", "skills/skills.bundle.zip")

    wp.data_root.cache_clear()
    wp.workspaces_root.cache_clear()
    wp._home_migrated.clear()
    wu._workspace_initialized.clear()
    wu._workspace_init_lock_registry._locks.clear()
    wu._workspace_file_locks._locks.clear()
    skills._bundle_source_root = None
    skills._bundle_source_hash = ""
    skills._bundle_lock = asyncio.Lock()

    fake = _FakeR2()
    import s3_utils
    monkeypatch.setattr(s3_utils, "is_r2_configured", lambda: True)
    monkeypatch.setattr(s3_utils, "get_r2_object_metadata", fake.get_r2_object_metadata)
    monkeypatch.setattr(s3_utils, "download_from_r2", fake.download_from_r2)
    monkeypatch.setattr(s3_utils, "upload_bytes_to_r2", fake.upload_bytes_to_r2)
    return fake, packaged, home_dir


def test_deploy_builds_one_zip_in_r2(monkeypatch, tmp_path):
    fake, _, _ = _fresh_env(monkeypatch, tmp_path)

    asyncio.run(skills._ensure_packaged_skill_bundle())

    assert list(fake.objects) == ["skills/skills.bundle.zip"]
    bundle = fake.objects["skills/skills.bundle.zip"]
    assert zipfile.is_zipfile(io.BytesIO(bundle))
    with zipfile.ZipFile(io.BytesIO(bundle)) as zf:
        manifest = json.loads(zf.read(".bundle-manifest.json"))
        assert manifest["format"] == 1
        assert "demo/SKILL.md" in manifest["files"]
        assert "demo/scripts/run.py" in manifest["files"]
    assert fake.metadata["skills/skills.bundle.zip"]["skills-sha256"] == manifest["source_sha256"]


def test_unchanged_deploy_does_not_upload_again(monkeypatch, tmp_path):
    fake, _, _ = _fresh_env(monkeypatch, tmp_path)
    asyncio.run(skills._ensure_packaged_skill_bundle())
    assert fake.uploads == 1

    # Simulate a fresh process: remote metadata is enough to avoid a new upload.
    skills._bundle_source_root = None
    skills._bundle_source_hash = ""
    asyncio.run(skills._ensure_packaged_skill_bundle())
    assert fake.uploads == 1


def test_skill_change_rebuilds_and_replaces_same_r2_object(monkeypatch, tmp_path):
    fake, packaged, _ = _fresh_env(monkeypatch, tmp_path)
    asyncio.run(skills._ensure_packaged_skill_bundle())
    old = fake.objects["skills/skills.bundle.zip"]

    (packaged / "demo" / "SKILL.md").write_text(
        "---\nname: demo\npriority: 1\n---\nchanged\n", encoding="utf-8"
    )
    skills._bundle_source_root = None
    skills._bundle_source_hash = ""
    asyncio.run(skills._ensure_packaged_skill_bundle())

    assert fake.uploads == 2
    assert fake.objects["skills/skills.bundle.zip"] != old
    assert list(fake.objects) == ["skills/skills.bundle.zip"]


def test_workspace_unpacks_bundle_once_and_preserves_user_edit(monkeypatch, tmp_path):
    fake, packaged, home_dir = _fresh_env(monkeypatch, tmp_path)
    asyncio.run(skills._ensure_packaged_skill_bundle())

    home = home_dir / NS
    first = skills.sync_all_skill_assets_to_workspace(home)
    assert first["copied"] == 2
    assert (home / "skills/demo/SKILL.md").read_text(encoding="utf-8").endswith("body\n")

    (home / "skills/demo/SKILL.md").write_text("user edit\n", encoding="utf-8")
    (packaged / "demo" / "SKILL.md").write_text(
        "---\nname: demo\n---\nserver update\n", encoding="utf-8"
    )
    skills._bundle_source_root = None
    skills._bundle_source_hash = ""
    asyncio.run(skills._ensure_packaged_skill_bundle())
    second = skills.sync_all_skill_assets_to_workspace(home)
    assert second["preserved_user_edits"] == 1
    assert (home / "skills/demo/SKILL.md").read_text(encoding="utf-8") == "user edit\n"
    assert fake.uploads == 2

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="skill_persist_"))
os.environ["APITELEGRAMCHAT_DATA_DIR"] = str(ROOT / "data")
os.environ["APITELEGRAMCHAT_SKILLS_DIR"] = str(ROOT / "packaged")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import skills
import workspace_utils
from workspace_paths import workspace_root


class FakeR2:
    def __init__(self):
        self.objects = {}

    async def list(self, prefix):
        return [key for key in self.objects if key.startswith(prefix + "/")]

    async def download(self, key):
        return self.objects.get(key)

    async def upload(self, data, key, content_type="application/octet-stream"):
        self.objects[key] = bytes(data)
        return "fake://" + key


async def main():
    packaged = ROOT / "packaged" / "builtin"
    packaged.mkdir(parents=True)
    (packaged / "SKILL.md").write_text("---\nname: builtin\n---\n", encoding="utf-8")
    fake = FakeR2()
    workspace_utils.list_r2_keys = fake.list
    workspace_utils.download_from_r2 = fake.download
    workspace_utils.upload_bytes_to_r2 = fake.upload
    skills.sync_all_skill_assets_to_workspace = lambda home: {"errors": [], "copied": 1}

    await workspace_utils._ensure_workspace_initialized(123, "user-a")
    home = workspace_root(123, "user-a")
    local_skill = home / "skills" / "custom" / "SKILL.md"
    local_skill.parent.mkdir(parents=True, exist_ok=True)
    local_skill.write_text("---\nname: custom\n---\n", encoding="utf-8")
    workspace_utils._workspace_initialized.clear()
    (home / ".skills_initialized").unlink()
    await workspace_utils._ensure_workspace_initialized(123, "user-a")
    key = "skills/user-a/custom/SKILL.md"
    assert fake.objects[key].startswith(b"---")

    # Simulate an ephemeral restart: remove the local skills and marker, then restore.
    import shutil
    shutil.rmtree(home / "skills")
    (home / ".skills_initialized").unlink()
    workspace_utils._workspace_initialized.clear()
    await workspace_utils._ensure_workspace_initialized(123, "user-a")
    assert (home / "skills" / "custom" / "SKILL.md").is_file()
    assert (home / ".skills_initialized").is_file()
    print("skill persistence: PASS")


asyncio.run(main())

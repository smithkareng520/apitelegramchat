import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="skill_discovery_"))
os.environ["APITELEGRAMCHAT_DATA_DIR"] = str(ROOT / "data")
os.environ["APITELEGRAMCHAT_SKILLS_DIR"] = str(ROOT / "packaged")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skills import load_skill_records, read_skill
from workspace_paths import workspace_skills_root


def write_skill(root: Path, skill_id: str, description: str, body: str) -> None:
    path = root / skill_id
    path.mkdir(parents=True, exist_ok=True)
    path.joinpath("SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


packaged = ROOT / "packaged"
write_skill(packaged, "builtin", "packaged builtin", "packaged builtin body")
write_skill(packaged, "shared", "packaged shared", "packaged shared body")

user_a = workspace_skills_root(1, "user-a")
write_skill(user_a, "custom-a", "user A custom", "user A body")
write_skill(user_a, "shared", "user A override", "user A override body")
user_b = workspace_skills_root(1, "user-b")
write_skill(user_b, "custom-b", "user B custom", "user B body")

records_a = load_skill_records(1, "user-a")
records_b = load_skill_records(1, "user-b")
ids_a = {record.skill_id for record in records_a}
ids_b = {record.skill_id for record in records_b}
assert {"builtin", "shared", "custom-a"}.issubset(ids_a)
assert {"builtin", "shared", "custom-b"}.issubset(ids_b)
assert "custom-b" not in ids_a
assert "custom-a" not in ids_b
assert next(record for record in records_a if record.skill_id == "shared").description == "user A override"
assert next(record for record in records_b if record.skill_id == "shared").description == "packaged shared"
assert read_skill("shared", 1, "user-a")["body"] == "user A override body"
assert read_skill("shared", 1, "user-b")["body"] == "packaged shared body"
print("skill discovery namespace: PASS")

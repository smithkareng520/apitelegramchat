"""Runtime skill catalog must come from the user's workspace skills directory."""
from __future__ import annotations

from pathlib import Path
import sys


SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_runtime_catalog_uses_workspace_skills_not_project_skills(monkeypatch, tmp_path):
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APITELEGRAMCHAT_SKILLS_DIR", str(tmp_path / "packaged"))

    import workspace_paths as wp
    wp.data_root.cache_clear()
    wp.workspaces_root.cache_clear()

    project = tmp_path / "packaged" / "project-only"
    project.mkdir(parents=True)
    (project / "SKILL.md").write_text(
        "---\nname: Project Only\ndescription: should not appear\n---\n",
        encoding="utf-8",
    )

    workspace_skills = wp.workspace_skills_root(12345, "10001")
    user = workspace_skills / "user-skill"
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text(
        "---\nname: User Skill\ndescription: workspace skill\n---\n",
        encoding="utf-8",
    )

    import skills

    catalog = skills.skill_catalog_brief(12345, "10001")
    assert "User Skill - workspace skill" in catalog
    assert "Project Only" not in catalog
    assert str(workspace_skills) in skills.get_skill_catalog(12345, "10001")["roots"]


def test_agent_run_refreshes_skill_catalog_in_place(monkeypatch):
    from core.messages import Message
    from skills_runtime import refresh_skill_catalog as _refresh_skill_catalog

    old = """
<p><b>当前可用技能列表：</b></p>
Old Skill - old description
<h3>角色提示</h3>
keep this stable
""".strip()
    prefix = """<p>固定工具说明</p>
<p><b>当前可用技能列表：</b></p>"""
    suffix = """
<h3>角色提示</h3>
keep this stable"""
    msg = Message.system(
        old,
        skill_catalog_refresh=True,
        skill_catalog_prefix=prefix,
        skill_catalog_suffix=suffix,
    )

    monkeypatch.setattr(
        "skills.skill_catalog_brief",
        lambda chat_id, namespace: "New Skill - changed during this agent run",
    )

    class Builder:
        chat_id = 12345

    messages = [msg]
    _refresh_skill_catalog(messages, Builder(), "10001")

    assert "Old Skill" not in msg.blocks[0].text
    assert "New Skill - changed during this agent run" in msg.blocks[0].text
    assert "keep this stable" in msg.blocks[0].text


def test_agent_run_refresh_reads_disk_without_r2_sync(monkeypatch, tmp_path):
    """A skill edit is visible on the next model call without waiting for R2."""
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("skills_r2.is_r2_configured", lambda: False)

    import workspace_paths as wp
    wp.data_root.cache_clear()
    wp.workspaces_root.cache_clear()

    from core.messages import Message
    from skills_runtime import refresh_skill_catalog

    skills_dir = wp.workspace_skills_root(12345, "10001")
    skill = skills_dir / "live-skill"
    skill.mkdir(parents=True)
    skill_file = skill / "SKILL.md"
    skill_file.write_text(
        "---\nname: Live Skill\ndescription: v1\n---\n", encoding="utf-8"
    )

    catalog = "Live Skill - v1"
    msg = Message.system(
        "<p><b>当前可用技能列表：</b></p>" + catalog,
        skill_catalog_refresh=True,
        skill_catalog_prefix="<p><b>当前可用技能列表：</b></p>",
        skill_catalog_suffix="",
    )

    class Builder:
        chat_id = 12345

    refresh_skill_catalog([msg], Builder(), "10001")
    assert "Live Skill - v1" in msg.blocks[0].text

    skill_file.write_text(
        "---\nname: Live Skill\ndescription: v2\n---\n", encoding="utf-8"
    )
    refresh_skill_catalog([msg], Builder(), "10001")
    assert "Live Skill - v2" in msg.blocks[0].text
    assert "Live Skill - v1" not in msg.blocks[0].text
